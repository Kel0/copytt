# copytt

Hyperliquid copytrader that mirrors a leader wallet's perp positions, scaled to your account equity. Trades the leader's actions live via Hyperliquid's WebSocket `userFills` feed; a 60s REST poll runs as a drift safety net.

Trades that fall below Hyperliquid's $10 minimum notional are paper-tracked into a "shadow portfolio" so you can see how the dust-filtered tail would have performed. A small Flask UI visualises that locally.

## Files

- `copytrader.py` — bot. WS-driven reconcile loop, leverage cap, dust filter.
- `shadow.py` — SQLite-backed paper tracker for dust-filtered trades.
- `webapp.py` — local Flask dashboard (chart + tables) on port `47821`.
- `Dockerfile` / `entrypoint.sh` — runs both processes in one container.

## Configure

Copy `.env.example` to `.env` and fill in:

```
HL_API_WALLET_PRIVATE_KEY=0x...   # generated at app.hyperliquid.xyz → API
HL_ACCOUNT_ADDRESS=0x...          # your main wallet (where USDC sits)
LEADER_ADDRESS=0x023a3d058020fb76cca98f01b3c48c8938a22355
POLL_SECONDS=60
MAX_LEVERAGE=10
DRY_RUN=true                      # flip to false to actually trade
```

API wallets sign orders for your main account but cannot withdraw funds.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python copytrader.py    # terminal 1
python webapp.py        # terminal 2 → http://127.0.0.1:47821
```

## Run in Docker

```bash
docker build -t copytt .
docker run -d --name copytt \
  --env-file .env \
  -p 47821:47821 \
  -v copytt-data:/data \
  --restart unless-stopped \
  copytt
```

Open http://localhost:47821. The shadow database persists in the `copytt-data` volume across restarts.

## Caveats

- $100–$300 accounts will dust-filter most leader positions; meaningful mirroring starts around $1k.
- Shadow PnL ignores fees and slippage — it's a price-action counterfactual, not a backtest of real execution.
- Copytrading public wallets is generally unprofitable after fees and entry slippage. Treat this as instrumentation, not alpha.
