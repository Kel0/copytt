"""
Hyperliquid copytrader — event-driven.

Mirrors a leader wallet's perp positions, scaled so each position takes the
same fraction of YOUR equity as it does of the leader's.

Architecture:
  - WebSocket subscribes to the leader's `userFills` feed. Every fill triggers
    an immediate reconciliation (low latency, this is the hot path).
  - A background poller hits `clearinghouseState` every POLL_SECONDS as a
    safety net to catch drift, missed events, or WS disconnects.
  - Reconciliation is idempotent: it always recomputes target sizes from the
    leader's CURRENT positions and diffs against ours, so duplicate triggers
    don't double-fill.

Leader (default): 0x023a3d058020fb76cca98f01b3c48c8938a22355

Setup:
    pip install -r requirements.txt
    cp .env.example .env   # fill in keys
    python copytrader.py
"""

from __future__ import annotations

import os
import ssl
import time
import math
import logging
import threading

# macOS Python often ships without root certs; force certifi's bundle so
# WebSocket TLS handshakes don't fail with CERTIFICATE_VERIFY_FAILED.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ssl._create_default_https_context = lambda: ssl.create_default_context(
        cafile=certifi.where()
    )
except ImportError:
    pass
from dataclasses import dataclass
from typing import Dict, Any

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from shadow import ShadowTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("copytrader")


# ---------- leader / own state ----------

def leader_target_weights(info: Info, leader: str) -> Dict[str, float]:
    """{coin: signed_notional / leader_equity}. + long, - short."""
    state = info.user_state(leader)
    equity = float(state["marginSummary"]["accountValue"])
    if equity <= 0:
        return {}
    weights: Dict[str, float] = {}
    for ap in state.get("assetPositions", []):
        pos = ap["position"]
        szi = float(pos["szi"])
        if szi == 0:
            continue
        notional = abs(float(pos["positionValue"]))
        weights[pos["coin"]] = (notional if szi > 0 else -notional) / equity
    return weights


def my_state(info: Info, address: str):
    """
    Returns (equity, held_perp_positions).
    Equity = perps account value + spot USDC. Works for both unified accounts
    (where USDC lives on the spot side and is auto-used as cross-collateral)
    and old segregated accounts.
    """
    state = info.user_state(address)
    perps_equity = float(state["marginSummary"]["accountValue"])

    spot_usdc = 0.0
    try:
        spot = info.spot_user_state(address)
        for bal in spot.get("balances", []):
            if bal.get("coin") == "USDC":
                spot_usdc = float(bal.get("total", 0))
                break
    except Exception as e:
        log.warning("spot_user_state failed: %s", e)

    equity = perps_equity + spot_usdc

    held: Dict[str, float] = {}
    for ap in state.get("assetPositions", []):
        pos = ap["position"]
        szi = float(pos["szi"])
        if szi != 0:
            held[pos["coin"]] = szi
    return equity, held


# ---------- math ----------

def round_size(size: float, sz_decimals: int) -> float:
    q = 10 ** sz_decimals
    return math.floor(abs(size) * q) / q * (1 if size >= 0 else -1)


def compute_targets(
    weights: Dict[str, float],
    my_equity: float,
    mids: Dict[str, float],
    sz_decimals: Dict[str, int],
    max_leverage: float,
) -> Dict[str, float]:
    gross = sum(abs(w) for w in weights.values())
    scale = 1.0
    if gross > max_leverage:
        scale = max_leverage / gross
        log.warning("leader gross %.2fx > cap %.2fx, scaling by %.3f",
                    gross, max_leverage, scale)
    targets: Dict[str, float] = {}
    for coin, w in weights.items():
        mid = mids.get(coin)
        if not mid or mid <= 0:
            log.warning("no mid for %s, skipping", coin)
            continue
        raw = (w * scale * my_equity) / mid
        targets[coin] = round_size(raw, sz_decimals.get(coin, 4))
    return targets


# ---------- execution ----------

def reconcile_once(ctx: "Ctx", reason: str) -> None:
    """Idempotent: pull fresh state, diff, send orders for the gap."""
    with ctx.lock:
        try:
            weights = leader_target_weights(ctx.info, ctx.leader)
            mids = {k: float(v) for k, v in ctx.info.all_mids().items()}
            my_equity, held = my_state(ctx.info, ctx.account_address)
            if my_equity <= 0:
                log.warning("own equity is 0 — deposit funds first")
                return

            targets = compute_targets(
                weights, my_equity, mids, ctx.sz_decimals, ctx.max_leverage
            )

            log.info("[%s] equity=$%.2f  weights=%s", reason, my_equity,
                     {k: round(v, 3) for k, v in weights.items()})

            coins = set(targets) | set(held)
            for coin in sorted(coins):
                target = targets.get(coin, 0.0)
                current = held.get(coin, 0.0)
                decimals = ctx.sz_decimals.get(coin, 4)
                delta = round_size(target - current, decimals)
                if abs(delta) < 10 ** (-decimals):
                    continue

                # Skip dust orders below Hyperliquid's $10 min notional, but
                # paper-track them so we can see what we missed.
                mid = mids.get(coin, 0.0)
                if mid * abs(delta) < 10:
                    log.info("skip dust %s delta=%s (~$%.2f) → shadow",
                             coin, delta, mid * abs(delta))
                    ctx.shadow.record_dust(coin, delta, mid)
                    continue

                is_buy = delta > 0
                size = abs(delta)
                log.info("%s %s %s  (have=%s want=%s)",
                         "BUY" if is_buy else "SELL", size, coin, current, target)
                if ctx.dry_run:
                    continue
                try:
                    res = ctx.exchange.market_open(coin, is_buy, size, None, 0.01)
                    log.info("order result: %s", res)
                except Exception as e:
                    log.exception("order failed for %s: %s", coin, e)

            # Mark-to-market the shadow portfolio every reconcile.
            shadow_total = ctx.shadow.snapshot(mids)
            log.info("shadow PnL: $%+.4f (realized=$%+.4f)",
                     shadow_total, ctx.shadow.realized)
        except Exception as e:
            log.exception("reconcile error: %s", e)


# ---------- runtime ----------

@dataclass
class Ctx:
    info: Info
    exchange: Exchange
    leader: str
    account_address: str
    sz_decimals: Dict[str, int]
    max_leverage: float
    dry_run: bool
    lock: threading.Lock
    shadow: ShadowTracker


def on_leader_fills(ctx: Ctx, msg: Dict[str, Any]) -> None:
    data = msg.get("data", {})
    fills = data.get("fills", []) or []
    if not fills:
        return
    for f in fills:
        log.info("LEADER FILL: %s %s %s @ %s",
                 f.get("side"), f.get("sz"), f.get("coin"), f.get("px"))
    reconcile_once(ctx, reason="leader_fill")


def safety_poller(ctx: Ctx, interval: int) -> None:
    while True:
        time.sleep(interval)
        reconcile_once(ctx, reason="poll")


def main() -> None:
    load_dotenv()

    pk = os.environ["HL_API_WALLET_PRIVATE_KEY"].strip().strip('"').strip("'")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if len(pk) != 66 or any(c not in "0123456789abcdefABCDEF" for c in pk[2:]):
        raise SystemExit(
            "HL_API_WALLET_PRIVATE_KEY is not a valid 32-byte hex key. "
            "Generate one at app.hyperliquid.xyz → API → Generate."
        )
    account_address = os.environ["HL_ACCOUNT_ADDRESS"].strip().strip('"').strip("'")
    leader = os.environ.get(
        "LEADER_ADDRESS", "0x023a3d058020fb76cca98f01b3c48c8938a22355"
    )
    poll = int(os.environ.get("POLL_SECONDS", "60"))
    max_leverage = float(os.environ.get("MAX_LEVERAGE", "10"))
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"

    wallet = Account.from_key(pk)
    info_ws = Info(constants.MAINNET_API_URL)  # WS enabled
    exchange = Exchange(
        wallet, constants.MAINNET_API_URL, account_address=account_address
    )

    meta = info_ws.meta()
    sz_decimals = {a["name"]: a["szDecimals"] for a in meta["universe"]}

    ctx = Ctx(
        info=info_ws,
        exchange=exchange,
        leader=leader,
        account_address=account_address,
        sz_decimals=sz_decimals,
        max_leverage=max_leverage,
        dry_run=dry_run,
        lock=threading.Lock(),
        shadow=ShadowTracker(),
    )

    log.info("leader=%s account=%s dry_run=%s max_lev=%.1fx poll=%ds",
             leader, account_address, dry_run, max_leverage, poll)

    # Initial sync so we don't wait for a fill to take positions.
    reconcile_once(ctx, reason="startup")

    # Subscribe to the leader's fills — this is the hot path.
    info_ws.subscribe(
        {"type": "userFills", "user": leader},
        lambda msg: on_leader_fills(ctx, msg),
    )

    # Background drift poller.
    t = threading.Thread(target=safety_poller, args=(ctx, poll), daemon=True)
    t.start()

    # Park main thread.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
