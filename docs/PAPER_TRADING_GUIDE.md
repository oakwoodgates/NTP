# Paper Trading Guide

How to start paper trading, verify the pipeline, and iterate with different strategies.

## Prerequisites

- Phase 2 code implemented and tests passing (`pytest tests/unit/`)
- Docker Desktop running
- Python venv activated with `pip install -e ".[dev]"`

## 1. Configure `.env`

```bash
cp .env.example .env
```

The defaults work out of the box for paper trading. Optionally set up Telegram alerts:

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token
2. Message your new bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`
3. Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`

You do **not** need `HL_PRIVATE_KEY` or `HL_WALLET_ADDRESS` for paper trading.

## 2. Start Infrastructure

```bash
docker compose up -d
docker compose ps          # verify postgres, redis, grafana are healthy
alembic upgrade head       # create tables (first time only)
```

## 3. Start Paper Trading

```bash
python scripts/run_sandbox.py
```

Default config: EMACross(10,20) on BTC-USD-PERP, 1-hour bars, 0.001 BTC per trade.

**What happens:**
- Registers a `strategy_runs` row in PostgreSQL with a unique `run_id`
- Connects to live Hyperliquid market data (real prices, simulated execution)
- EMACross generates trades based on EMA crossovers
- Every fill → PersistenceActor writes to `order_fills` + AlertActor sends Telegram
- Every position close → writes to `positions` + Telegram
- Every 60s → account balance snapshot to `account_snapshots`
- Ctrl+C → graceful shutdown, `strategy_runs.stopped_at` updated

**First trade timing:** EMACross needs 20 bars (slow EMA period) before generating signals. With 1-hour bars and historical bar request on start, the first signal may come within a few hours.

## 4. Verify the Pipeline

### Check PostgreSQL

```bash
docker exec -it ntp-postgres-1 psql -U nautilus -d nautilus_platform
```

```sql
-- Active run
SELECT id, strategy_id, instrument_id, run_mode, started_at FROM strategy_runs;

-- Fills (after first trade)
SELECT ts, order_side, last_qty, last_px FROM order_fills ORDER BY ts DESC LIMIT 5;

-- Account snapshots (every 60s from start)
SELECT ts, balance_total FROM account_snapshots ORDER BY ts DESC LIMIT 5;

-- Positions (only after a round-trip trade completes)
SELECT * FROM positions ORDER BY ts_closed DESC LIMIT 5;
```

### Check Grafana

Open [http://localhost:3000](http://localhost:3000) (login: `admin` / `changeme` or your `GRAFANA_PASSWORD`).

The "Trading Overview" dashboard has:
- Account balance and cumulative PnL charts
- Drawdown % chart
- Win rate, total PnL, trade count stats
- Fill and position tables
- Template dropdowns to filter by strategy and run

### Check Telegram (if configured)

You should receive messages for fills, position closes (WIN/LOSS with PnL), and drawdown alerts (>10% from peak).

## 5. Switch Strategies

Change these in `.env`:

```bash
# Strategy: EMACross | SMACross | EMACrossATR | MACDRSI
STRATEGY=EMACross

# Instrument: BTC | ETH | SOL
INSTRUMENT_ID=BTC-USD-PERP.HYPERLIQUID

# Adjust trade size to keep ~$50-100 notional
TRADE_SIZE=0.01

# Timeframe
BAR_INTERVAL=15-MINUTE-LAST-EXTERNAL
```

Then restart: Ctrl+C the running node, `python scripts/run_sandbox.py`.

### Available strategies

| Strategy | Description | Key params (edit in `_build_strategy()`) |
|----------|-------------|------------------------------------------|
| EMACross | Simple EMA crossover | fast=10, slow=20 |
| SMACross | Simple SMA crossover | fast=10, slow=20 |
| EMACrossATR | EMA cross + ATR bracket TP/SL | fast=20, slow=50, atr=14, sl=1.5x, tp=3.0x |
| MACDRSI | MACD + RSI confluence | macd 12/26/9, rsi=14 |

### Available instruments

| Instrument | TRADE_SIZE for ~$50-100 |
|------------|------------------------|
| `BTC-USD-PERP.HYPERLIQUID` | `0.001` |
| `ETH-USD-PERP.HYPERLIQUID` | `0.01` |
| `SOL-USD-PERP.HYPERLIQUID` | `0.1` |

### Available timeframes

`1-MINUTE-LAST-EXTERNAL`, `5-MINUTE-LAST-EXTERNAL`, `15-MINUTE-LAST-EXTERNAL`, `1-HOUR-LAST-EXTERNAL`, `4-HOUR-LAST-EXTERNAL`, `1-DAY-LAST-EXTERNAL`

## 6. Daily Monitoring

1. **Grafana** — check balance curve, fill frequency, PnL distribution
2. **Telegram** — review fill notifications, watch for drawdown alerts
3. **PostgreSQL** — query for anomalies
4. **Terminal** — check for errors in TradingNode stdout
5. **Compare to backtest** — if paper results lag backtest by >30-40%, investigate

## 7. When to Go Live

1. Paper trade for **2+ weeks** with stable behavior
2. Verify fill prices match expected levels
3. Confirm no missed fills in persistence pipeline
4. Verify drawdown stays within acceptable bounds
5. Compare paper vs backtest results (expect 30-40% haircut)
6. Then: set `HL_TESTNET=false`, add `HL_PRIVATE_KEY` in `.env`, run `python scripts/run_live.py`

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No fills after 20+ hours | Check NT logs for subscription errors. Set `log_level="DEBUG"` temporarily in the runner. |
| `asyncpg.InvalidCatalogNameError` | Run `alembic upgrade head`. |
| `ConnectionRefusedError` on postgres | `docker compose up -d` |
| Telegram not sending | Verify token/chat_id. Test: `curl https://api.telegram.org/bot<TOKEN>/getMe` |
| `ModuleNotFoundError` | `pip install -e ".[dev]"` in activated venv |
| Redis connection error | `docker compose ps` — check Redis is running on port 6379 |
| Grafana panels empty | Data needs time to accumulate. Check datasource in Grafana → Settings → Data sources. |

## Files Reference

| File | Purpose |
|------|---------|
| `scripts/run_sandbox.py` | Paper trading runner |
| `scripts/run_live.py` | Live trading runner (after paper validation) |
| `src/actors/persistence.py` | Writes fills/positions/snapshots to PostgreSQL |
| `src/actors/alert.py` | Telegram notifications |
| `src/config/settings.py` | All settings from `.env` |
| `.env` | Your local configuration (gitignored) |
| `grafana/dashboards/trading.json` | Grafana dashboard definition |
