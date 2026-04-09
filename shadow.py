"""
Shadow tracker for dust-filtered trades.

Maintains a paper portfolio of trades the live bot SKIPPED because they were
below the $10 min notional. Lets you answer: 'what would those small mirrors
have done if I'd taken them — net of fees?'

State:
  - shadow_fills:     every dust delta we recorded, with mid + fee at the time
  - shadow_snapshots: periodic mark-to-market (realized + unrealized − fees)
  - in-memory positions, realized PnL, and cumulative fees: rebuilt on startup

Fee model: every fill costs `notional * SHADOW_FEE_RATE`. Default 0.00045 =
4.5bps, the Hyperliquid taker fee at the base tier. Override via env var if
you have a discount tier or want to model maker fees. Fees are tracked
separately from realized so you can see trading PnL and friction independently.

Slippage is still NOT modeled (fills happen at the mid). Funding payments are
not modeled either. So shadow PnL ≈ price-action PnL minus fees only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Dict, Tuple

DB_PATH = os.environ.get(
    "SHADOW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow.db"),
)

# Hyperliquid base taker fee = 4.5 bps. Override with SHADOW_FEE_RATE.
FEE_RATE = float(os.environ.get("SHADOW_FEE_RATE", "0.00045"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_fills (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    coin     TEXT    NOT NULL,
    side     TEXT    NOT NULL,
    size     REAL    NOT NULL,
    price    REAL    NOT NULL,
    notional REAL    NOT NULL,
    fee      REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON shadow_fills(ts);

CREATE TABLE IF NOT EXISTS shadow_snapshots (
    ts             INTEGER PRIMARY KEY,
    realized_pnl   REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    total_pnl      REAL NOT NULL,
    fees_paid      REAL NOT NULL DEFAULT 0,
    open_positions TEXT NOT NULL
);
"""


def _ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing table if missing (cheap migration)."""
    cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


class ShadowTracker:
    def __init__(self, db_path: str = DB_PATH, fee_rate: float = FEE_RATE) -> None:
        self.db_path = db_path
        self.fee_rate = fee_rate
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        # Migrate older databases that predate the fee columns.
        _ensure_column(self.db, "shadow_fills", "fee", "fee REAL NOT NULL DEFAULT 0")
        _ensure_column(self.db, "shadow_snapshots", "fees_paid",
                       "fees_paid REAL NOT NULL DEFAULT 0")
        self.db.commit()
        self.lock = threading.Lock()

        # In-memory state, rebuilt from history.
        # positions[coin] = (signed_size, avg_entry_price)
        self.positions: Dict[str, Tuple[float, float]] = {}
        self.realized: float = 0.0
        self.fees: float = 0.0
        self._rebuild_from_db()

    # ---------- core accounting ----------

    def _apply(self, coin: str, signed_delta: float, price: float) -> None:
        cur_size, cur_entry = self.positions.get(coin, (0.0, 0.0))
        new_size = cur_size + signed_delta

        # Same direction (or opening from flat) → update weighted avg entry.
        if cur_size == 0 or (cur_size > 0) == (signed_delta > 0):
            if new_size == 0:
                self.positions.pop(coin, None)
                return
            new_entry = (
                abs(cur_size) * cur_entry + abs(signed_delta) * price
            ) / abs(new_size)
            self.positions[coin] = (new_size, new_entry)
            return

        # Opposite direction → realize PnL on the closed portion.
        closed = min(abs(cur_size), abs(signed_delta))
        sign = 1 if cur_size > 0 else -1
        self.realized += (price - cur_entry) * closed * sign

        if abs(signed_delta) < abs(cur_size):
            # Partial close, entry unchanged.
            self.positions[coin] = (new_size, cur_entry)
        elif abs(signed_delta) == abs(cur_size):
            # Fully closed.
            self.positions.pop(coin, None)
        else:
            # Flipped through zero — leftover opens new position at fill price.
            leftover = abs(signed_delta) - abs(cur_size)
            self.positions[coin] = (
                leftover * (1 if signed_delta > 0 else -1),
                price,
            )

    def _rebuild_from_db(self) -> None:
        self.positions.clear()
        self.realized = 0.0
        self.fees = 0.0
        for coin, side, size, price, fee in self.db.execute(
            "SELECT coin, side, size, price, fee FROM shadow_fills ORDER BY id ASC"
        ):
            signed = size if side == "buy" else -size
            self._apply(coin, signed, price)
            self.fees += fee

    # ---------- public API ----------

    def record_dust(self, coin: str, signed_delta: float, price: float) -> None:
        if signed_delta == 0 or price <= 0:
            return
        side = "buy" if signed_delta > 0 else "sell"
        size = abs(signed_delta)
        notional = size * price
        fee = notional * self.fee_rate
        with self.lock:
            self.db.execute(
                "INSERT INTO shadow_fills "
                "(ts, coin, side, size, price, notional, fee) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), coin, side, size, price, notional, fee),
            )
            self.db.commit()
            self._apply(coin, signed_delta, price)
            self.fees += fee

    def snapshot(self, mids: Dict[str, float]) -> float:
        with self.lock:
            unrealized = 0.0
            for coin, (size, entry) in self.positions.items():
                mid = mids.get(coin)
                if mid:
                    unrealized += (mid - entry) * size
            total = self.realized + unrealized - self.fees
            payload = {
                k: {"size": v[0], "entry": v[1]}
                for k, v in self.positions.items()
            }
            self.db.execute(
                "INSERT OR REPLACE INTO shadow_snapshots "
                "(ts, realized_pnl, unrealized_pnl, total_pnl, fees_paid, open_positions) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), self.realized, unrealized, total,
                 self.fees, json.dumps(payload)),
            )
            self.db.commit()
            return total
