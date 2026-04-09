"""
Local visualizer for the shadow (dust-trade) portfolio.

Run alongside the bot:
    python webapp.py
Then open http://127.0.0.1:5000

Reads the same shadow.db that copytrader.py writes to.
"""

from __future__ import annotations

import json
import os
import sqlite3

from flask import Flask, jsonify, render_template_string

DB_PATH = os.environ.get(
    "SHADOW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow.db"),
)

app = Flask(__name__)


def q(sql: str, args: tuple = ()):
    db = sqlite3.connect(DB_PATH)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Shadow Portfolio — Dust Trades</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0b0f14; --fg: #e6edf3; --muted: #8b949e;
    --card: #161b22; --border: #30363d; --green: #3fb950; --red: #f85149;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
    font-family: -apple-system, system-ui, "Segoe UI", sans-serif; }
  h1 { margin: 0 0 4px; font-size: 22px; }
  .sub { color: var(--muted); margin-bottom: 24px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px;
    margin-bottom: 24px; }
  @media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } }
  .card { background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.05em; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .panel { background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 24px; }
  .panel h2 { margin: 0 0 12px; font-size: 14px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.05em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px;
    font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  td.num { text-align: right; }
  .empty { color: var(--muted); padding: 12px 0; font-size: 13px; }
  #chart-wrap { height: 320px; }
  .refresh { float: right; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
  <h1>Shadow Portfolio <span class="sub">— dust-filtered trades, paper-mirrored</span></h1>
  <div class="sub">Auto-refresh every 10s · reads shadow.db</div>

  <div class="grid">
    <div class="card"><div class="label">Total PnL (net of fees)</div><div class="value" id="total">—</div></div>
    <div class="card"><div class="label">Realized</div><div class="value" id="realized">—</div></div>
    <div class="card"><div class="label">Unrealized</div><div class="value" id="unrealized">—</div></div>
    <div class="card"><div class="label">Fees Paid</div><div class="value" id="fees">—</div></div>
    <div class="card"><div class="label">Shadow Fills</div><div class="value" id="fillcount">—</div></div>
  </div>

  <div class="panel">
    <h2>PnL over time</h2>
    <div id="chart-wrap"><canvas id="chart"></canvas></div>
  </div>

  <div class="panel">
    <h2>Open shadow positions</h2>
    <table id="pos-table">
      <thead><tr><th>Coin</th><th class="num">Size</th><th class="num">Avg Entry</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="panel">
    <h2>Recent shadow fills</h2>
    <table id="fills-table">
      <thead><tr><th>Time</th><th>Coin</th><th>Side</th><th class="num">Size</th><th class="num">Price</th><th class="num">Notional</th><th class="num">Fee</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
const fmt = n => (n >= 0 ? "+$" : "-$") + Math.abs(n).toFixed(4);
const cls = n => n >= 0 ? "pos" : "neg";

let chart;
function initChart(snaps) {
  const ctx = document.getElementById("chart").getContext("2d");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "Net PnL (after fees)",
          data: snaps.map(s => ({x: s.ts, y: s.total})),
          borderColor: "#58a6ff", backgroundColor: "rgba(88,166,255,0.1)",
          borderWidth: 2, fill: true, tension: 0.2, pointRadius: 0,
        },
        {
          label: "Gross PnL (pre-fees)",
          data: snaps.map(s => ({x: s.ts, y: s.realized + s.unrealized})),
          borderColor: "#3fb950", borderWidth: 1.5, borderDash: [4, 4],
          fill: false, tension: 0.2, pointRadius: 0,
        },
        {
          label: "Cumulative fees",
          data: snaps.map(s => ({x: s.ts, y: -s.fees})),
          borderColor: "#f85149", borderWidth: 1.5, borderDash: [2, 4],
          fill: false, tension: 0.2, pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { type: "time", ticks: { color: "#8b949e" }, grid: { color: "#21262d" } },
        y: { ticks: { color: "#8b949e", callback: v => "$" + v.toFixed(2) },
             grid: { color: "#21262d" } },
      },
      plugins: { legend: { labels: { color: "#8b949e" } } },
    },
  });
}

async function refresh() {
  const [snaps, fills, positions] = await Promise.all([
    fetch("/api/snapshots").then(r => r.json()),
    fetch("/api/fills").then(r => r.json()),
    fetch("/api/positions").then(r => r.json()),
  ]);

  const last = snaps[snaps.length - 1] || {realized: 0, unrealized: 0, total: 0, fees: 0};
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.textContent = fmt(v);
    el.className = "value " + cls(v);
  };
  set("total", last.total);
  set("realized", last.realized);
  set("unrealized", last.unrealized);
  // Fees always shown as a cost (negative).
  const feesEl = document.getElementById("fees");
  feesEl.textContent = "-$" + (last.fees || 0).toFixed(4);
  feesEl.className = "value neg";
  document.getElementById("fillcount").textContent = fills.length;

  initChart(snaps);

  const posBody = document.querySelector("#pos-table tbody");
  posBody.innerHTML = "";
  const entries = Object.entries(positions);
  if (entries.length === 0) {
    posBody.innerHTML = '<tr><td colspan="3" class="empty">no open shadow positions</td></tr>';
  } else {
    for (const [coin, p] of entries) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${coin}</td><td class="num ${cls(p.size)}">${p.size.toFixed(6)}</td><td class="num">$${p.entry.toFixed(4)}</td>`;
      posBody.appendChild(tr);
    }
  }

  const fillBody = document.querySelector("#fills-table tbody");
  fillBody.innerHTML = "";
  if (fills.length === 0) {
    fillBody.innerHTML = '<tr><td colspan="6" class="empty">no shadow fills yet</td></tr>';
  } else {
    for (const f of fills) {
      const t = new Date(f.ts).toLocaleString();
      const sideClass = f.side === "buy" ? "pos" : "neg";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${t}</td><td>${f.coin}</td><td class="${sideClass}">${f.side}</td>` +
                     `<td class="num">${f.size.toFixed(6)}</td><td class="num">$${f.price.toFixed(4)}</td>` +
                     `<td class="num">$${f.notional.toFixed(2)}</td>` +
                     `<td class="num neg">-$${(f.fee || 0).toFixed(4)}</td>`;
      fillBody.appendChild(tr);
    }
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/api/snapshots")
def snapshots():
    rows = q(
        "SELECT ts, realized_pnl, unrealized_pnl, total_pnl, fees_paid "
        "FROM shadow_snapshots ORDER BY ts ASC"
    )
    return jsonify([
        {"ts": r[0] * 1000, "realized": r[1], "unrealized": r[2],
         "total": r[3], "fees": r[4]}
        for r in rows
    ])


@app.get("/api/fills")
def fills():
    rows = q(
        "SELECT ts, coin, side, size, price, notional, fee "
        "FROM shadow_fills ORDER BY id DESC LIMIT 200"
    )
    return jsonify([
        {"ts": r[0] * 1000, "coin": r[1], "side": r[2],
         "size": r[3], "price": r[4], "notional": r[5], "fee": r[6]}
        for r in rows
    ])


@app.get("/api/positions")
def positions():
    rows = q(
        "SELECT open_positions FROM shadow_snapshots ORDER BY ts DESC LIMIT 1"
    )
    return jsonify(json.loads(rows[0][0]) if rows else {})


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"shadow.db not found at {DB_PATH} — start copytrader.py first.")
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "47821"))
    app.run(host=host, port=port, debug=False)
