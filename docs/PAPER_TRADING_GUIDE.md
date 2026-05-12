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

Default config: MACross-EMA(10,40) on BTC-USD-PERP, 4-hour bars, $2000 USD notional per trade (matches the canonical backtest in `notebooks/backtest/ma_cross.ipynb`). All hyperparameters flow from `.env` — see [`CONFIG.md`](CONFIG.md) for the per-system field map.

**What happens:**
- Registers a `strategy_runs` row in PostgreSQL with a unique `run_id`
- Connects to live Hyperliquid market data (real prices, simulated execution)
- MACross generates trades based on moving average crossovers (EMA by default)
- Every fill → PersistenceActor writes to `order_fills` + AlertActor sends Telegram
- Every position close → writes to `positions` + Telegram
- Every 60s → account balance snapshot to `account_snapshots`
- Ctrl+C → graceful shutdown, `strategy_runs.stopped_at` updated

**First trade timing:** MACross needs `slow_period` bars before generating signals. With default EMA(10,20) on 1-hour bars and historical bar request on start, the first signal may come within a few hours.

## 3b. Start Paper Trading (Docker)

Instead of running natively, you can run the trading node as a Docker container.
This is recommended for multi-day/week runs — the container restarts automatically
on crashes and survives terminal disconnects.

```bash
# Build the trader image (first time, or after code changes)
docker compose build trader

# Run migrations (first time only — before starting the trader)
docker compose run --rm trader alembic upgrade head

# Start everything (infra + trader)
docker compose up -d

# Tail logs (replaces the terminal session)
docker compose logs -f trader --tail 200
```

**Managing the container:**

| Action | Command |
|--------|---------|
| Stop trading (graceful) | `docker compose stop trader` |
| Start trading | `docker compose start trader` |
| Restart after `.env` change | `docker compose restart trader` |
| Restart after code change | `docker compose build trader && docker compose up -d trader` |
| Run migrations (trader running) | `docker compose exec trader alembic upgrade head` |
| Run migrations (trader stopped) | `docker compose run --rm trader alembic upgrade head` |
| Check status | `docker compose ps` |

The container uses `restart: unless-stopped` — it restarts automatically on
crashes and host reboots, but stays stopped after `docker compose stop`.

Each restart creates a new `run_id` in `strategy_runs`. Filter by run in Grafana.

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

-- Per-bar signal-gate stream (one row per bar after indicator warmup)
SELECT ts, signal, fast_value, slow_value, acted, bootstrap
FROM signal_events ORDER BY ts DESC LIMIT 10;
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

All hyperparameters flow from `.env`. Edit, restart `python scripts/run_sandbox.py` (or `docker compose restart trader`). No code edits required.

```bash
# Strategy + instrument
STRATEGY=MACross                          # MACross | …Cross | MACrossLongOnly | …CrossLongOnly | MACrossATR | MACDRSI
INSTRUMENT_ID=BTC-USD-PERP.HYPERLIQUID    # BTC | ETH | SOL
BAR_INTERVAL=4h                           # friendly form — converted to NT bar-type at the boundary
TRADE_NOTIONAL=2000

# MA crossover family (MACross, *Cross, MACrossLongOnly, *CrossLongOnly)
MA_FAST=10
MA_SLOW=40
MA_TYPE=EMA                               # EMA | SMA | HMA | DEMA | AMA | VIDYA
                                          # (family-specific aliases like EMACross pin MA_TYPE by name)

# MACrossATR
MACROSS_ATR_PERIOD=14
MACROSS_ATR_SL_MULT=1.5
MACROSS_ATR_TP_MULT=3.0

# MACDRSI
MACDRSI_MACD_FAST=12
MACDRSI_MACD_SLOW=26
MACDRSI_MACD_SIGNAL=9
MACDRSI_RSI_PERIOD=14
```

### Available strategies

**MA crossover variants** (all use unified `ma_cross.py` via `MovingAverageFactory`):

| Strategy | MA Type | Description |
|----------|---------|-------------|
| MACross | from `MA_TYPE` | MA crossover; family chosen by `MA_TYPE` env var |
| EMACross | EMA | Exponential MA (pinned regardless of `MA_TYPE`) |
| SMACross | SMA | Simple MA (pinned) |
| HMACross | HMA | Hull MA — less lag (pinned) |
| DEMACross | DEMA | Double Exponential MA — smoother (pinned) |
| AMACross | AMA | Kaufman Adaptive MA — volatility-adaptive (pinned) |
| VIDYACross | VIDYA | Variable Index Dynamic Average — CMO-adaptive (pinned) |

All MA crossover strategies share `MA_FAST`, `MA_SLOW`. Family-specific aliases override `MA_TYPE`.

**MA crossover long-only variants** (`ma_cross_long_only.py` — never shorts):

| Strategy | MA Type |
|----------|---------|
| MACrossLongOnly | from `MA_TYPE` |
| {EMA,SMA,HMA,DEMA,AMA,VIDYA}CrossLongOnly | pinned by alias |

**Other strategies:**

| Strategy | Key params (in `.env`) |
|----------|------------------------|
| MACrossATR | `MA_FAST`, `MA_SLOW`, `MACROSS_ATR_PERIOD`, `MACROSS_ATR_SL_MULT`, `MACROSS_ATR_TP_MULT` |
| MACDRSI | `MACDRSI_MACD_FAST`, `MACDRSI_MACD_SLOW`, `MACDRSI_MACD_SIGNAL`, `MACDRSI_RSI_PERIOD` |

### Available instruments

| Instrument | Example |
|------------|---------|
| `BTC-USD-PERP.HYPERLIQUID` | TRADE_NOTIONAL=100 → ~$100 per trade |
| `ETH-USD-PERP.HYPERLIQUID` | TRADE_NOTIONAL=100 → ~$100 per trade |
| `SOL-USD-PERP.HYPERLIQUID` | TRADE_NOTIONAL=100 → ~$100 per trade |

### Available timeframes

`1m`, `5m`, `15m`, `1h`, `4h`, `1d` (friendly form — converted to NT bar-type at the boundary).

## 5b. Phase 2.5 Revalidation — Stage A (sandbox) → Stage B (HL testnet)

Phase 2.5 of the roadmap ([`ROADMAP.md`](ROADMAP.md)) confirms the
cross-gated MACross + protective-stop + liquidation-simulator stack
behaves the same in paper as in backtest. Two stages:

### Stage A — Sandbox revalidation (~1 week)

Smoke-tests the new wiring (signal_events, AccountAliveMonitor) before
committing to a 2-week testnet run.

```bash
alembic upgrade head                      # picks up 002_signal_events
docker compose up -d                      # or python scripts/run_sandbox.py natively
```

**Verify after ~16 hours (4 bars at 4h):**

```sql
-- Per-bar gate stream is flowing
SELECT COUNT(*) FROM signal_events WHERE run_id = '<id>';   -- should be > 0

-- Acted vs observed split
SELECT acted, COUNT(*) FROM signal_events WHERE run_id = '<id>' GROUP BY acted;
```

```bash
# AccountAliveMonitor is subscribed
docker compose logs trader | grep "AccountAliveMonitor started"

# Per-bar log line from MACross (one per bar after warmup)
docker compose logs trader | grep "cross_gate:"
```

**Stage A pass-through:** any of `{signal_events empty, no AccountAliveMonitor log, blocked-callback warnings}` → fix and re-do. Do NOT proceed to Stage B with broken plumbing.

### Stage B — HL testnet (~2 weeks)

The real revalidation. Uses `scripts/run_live.py` against the actual HL
testnet exchange (real orders, fake money).

```bash
# .env settings:
#   HL_PRIVATE_KEY=<testnet key>
#   HL_TESTNET=true
#   LIVE_CONFIRM=yes
docker compose build trader
docker compose up -d trader
```

Pass criteria (gates Phase 2.6):

- [ ] ≥20 closed positions in `positions` table for the run.
- [ ] Every fill in `order_fills` matches an HL testnet UI fill (spot-check 5).
- [ ] `docker compose logs trader | grep -c "blocked-callback"` == 0.
- [ ] At least one drawdown alert OR documented reason none fired.
- [ ] No unhandled exceptions in `docker compose logs trader`.

### Signal alignment analysis (the roadmap's open question)

After Stage B, in `notebooks/review_live_run.ipynb`, join `signal_events`
against a fresh backtest re-run over the same window. Useful queries:

```sql
-- The full per-bar gate stream for the testnet run
SELECT ts, signal, fast_value, slow_value, acted, bootstrap
FROM signal_events
WHERE run_id = '<testnet run_id>'
ORDER BY ts;

-- Just the acted bars (one per entry/flip)
SELECT ts, signal, fast_value, slow_value
FROM signal_events
WHERE run_id = '<testnet run_id>' AND acted = TRUE
ORDER BY ts;

-- Per-bar lag (paper ts_received vs bar ts_event) — currently same column,
-- but if we add ts_received later this is where it'd surface
SELECT date_trunc('hour', ts) AS bar_hour, COUNT(*)
FROM signal_events WHERE run_id = '<testnet run_id>'
GROUP BY bar_hour ORDER BY bar_hour;
```

## 6. Daily Monitoring

1. **Grafana** — check balance curve, fill frequency, PnL distribution
2. **Telegram** — review fill notifications, watch for drawdown alerts
3. **PostgreSQL** — query for anomalies
4. **Terminal** — check for errors in TradingNode stdout
5. **Compare to backtest** — if paper results lag backtest by >30-40%, investigate

## 7. NautilusTrader upgrades

This project currently pins NautilusTrader `1.226.0` in `pyproject.toml`. When
upgrading NautilusTrader in future:

- Re-run the canonical EMA Cross backtest notebook to sanity-check P&L,
  drawdown, and trade counts (matching-engine changes can legitimately move
  these a bit between releases).
- Re-run `scripts/run_sandbox.py` and verify that fills, positions, and account
  snapshots still write cleanly into PostgreSQL and that Telegram alerts fire
  on fills/position changes.
- Skim the upstream `RELEASES.md` for any Hyperliquid changes (this project
  trades only on Hyperliquid for now) and adjust comments/docs if behavior
  changes (e.g. builder fee handling).

## 8. When to Go Live

1. Paper trade for **2+ weeks** with stable behavior
2. Verify fill prices match expected levels
3. Confirm no missed fills in persistence pipeline
4. Verify drawdown stays within acceptable bounds
5. Compare paper vs backtest results (expect 30-40% haircut)
6. Then: set `HL_TESTNET=false` and add `HL_PRIVATE_KEY` in `.env`.
   - **Native:** `python scripts/run_live.py` (interactive confirmation prompt)
   - **Docker:** Also set `TRADING_SCRIPT=scripts/run_live.py` and `LIVE_CONFIRM=yes` in `.env`, then `docker compose restart trader`

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
| Trader container restart loop | Check `docker compose logs trader --tail 50`. Common cause: migrations not run — run `docker compose run --rm trader alembic upgrade head`. |
| `docker compose stop` but `stopped_at` is NULL | SIGTERM handler issue — verify `scripts/run_sandbox.py` has the `signal.signal(signal.SIGTERM, ...)` handler at the top of `main()`. |
| Live trading restart loop in Docker | `input()` requires a TTY. Set `LIVE_CONFIRM=yes` in `.env` for containerized live trading. |

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
