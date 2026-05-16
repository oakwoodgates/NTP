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

Default config: MACross-EMA(`MA_FAST=10`, `MA_SLOW=100`) on `BTC-USD-PERP.HYPERLIQUID`, 4-hour bars, $2000 USD notional per trade, 5% protective stop. All hyperparameters flow from `.env` — see [`CONFIG.md`](CONFIG.md) for the per-system field map.

**What happens:**
- Registers a `strategy_runs` row in PostgreSQL with a unique `run_id` + `trader_id`
- Connects to live Hyperliquid market data (real prices, simulated execution via `SandboxExecutionClient`)
- MACross generates trades on moving-average crosses (EMA by default), gated to act only on fresh transitions (see [`STRATEGY_ENTRY_RULES.md`](STRATEGY_ENTRY_RULES.md))
- Every fill → PersistenceActor writes to `order_fills` + AlertActor sends Telegram
- Every position close → writes to `positions` + Telegram WIN/LOSS message
- Every bar (after warmup) → `signal_events` row with the per-bar gate state
- Every 60s → account balance snapshot to `account_snapshots`
- 5% protective stop fires reduce-only when an open position moves 5% against entry
- Ctrl+C → graceful shutdown, `strategy_runs.stopped_at` updated

**First trade timing:** MACross needs `MA_SLOW` bars of warmup before producing signals. At the default `MA_SLOW=100` on 4-hour bars, that's **100 × 4h = ~17 days** of warmup before the first cross can fire. NT backfills historical bars on start, so warmup completes within minutes of process start — but the first ACTED signal then requires a fresh cross transition, which on slow MAs at 4h is roughly once every 1-3 weeks under typical conditions.

**Skip the wait with `BOOTSTRAP_ON_DEPLOY=true`:** the strategy treats the first observed signal direction after warmup as a synthetic cross and fires immediately. Useful for **live deploys mid-trend** where you want to catch the current move. **Leave `false` for Phase 2.5/2.6 verification** — a synthetic deploy-time entry muddies the backtest-vs-paper signal-alignment analysis (Tool 1). See [`STRATEGY_ENTRY_RULES.md`](STRATEGY_ENTRY_RULES.md) for the rationale.

## 3b. Start Paper Trading (Docker)

Instead of running natively, you can run the trading node as a Docker container.
This is recommended for multi-day/week runs — the container restarts automatically
on crashes and survives terminal disconnects.

**Trader services are profile-gated.** `docker compose up -d` on its own
now starts only infrastructure (postgres, redis, grafana). To start a
trader, pick a profile:

| Profile | What it starts | When to use |
|---|---|---|
| `single` | Legacy single-instrument `trader` service driven by `.env` | Default Phase 2 single-strategy paper trading |
| `eth` / `btc` / `sol` | One per-instrument trader with config baked into compose | Phase 2.5/2.6 verification — launch instruments one at a time |
| `multi` | All three per-instrument traders at once | Phase 2.5/2.6 full multi-instrument verification |

### Single-instrument (legacy) deploy

```bash
# Build the trader image (first time, or after code changes)
docker compose build trader

# Run migrations (first time only — before starting the trader)
docker compose --profile single run --rm trader alembic upgrade head

# Start infra + single-instrument trader
docker compose --profile single up -d

# Tail logs
docker compose logs -f trader --tail 200
```

### Multi-instrument deploy

Three trader containers, one per instrument, each reading its strategy
config from a **gitignored per-service env file**: `.env.eth`,
`.env.btc`, `.env.sol`. Distinct `TRADER_ID` per container keeps PG
`strategy_runs` rows + NT MessageBus topics from colliding.

**Why per-service env files (not values in compose):** strategy picks
(MA windows, stops, bar intervals, instrument choice) are findings, not
infrastructure. They stay on the deploy host. Templates with empty
values are committed at `.env.eth.example` / `.env.btc.example` /
`.env.sol.example` to show the schema.

**First-time setup on the deploy host:**

```bash
# Copy the templates and fill in your picks for each instrument
cp .env.eth.example .env.eth        # then edit with your ETH config
cp .env.btc.example .env.btc        # then edit with your BTC config
cp .env.sol.example .env.sol        # then edit with your SOL config
```

If you don't create a per-service env file, `docker compose up` fails
loudly when that profile is requested — better than silently using
`.env` defaults and trading the wrong instrument.

**Build + migrate + launch:**

```bash
# Build once (all trader services share the image)
docker compose build trader

# Run migrations (one-shot via the single profile)
docker compose --profile single run --rm trader alembic upgrade head

# Launch one instrument at a time (e.g. ETH first)
docker compose --profile eth up -d trader-eth
docker compose logs -f trader-eth --tail 200

# Add more instruments as you go
docker compose --profile btc up -d trader-btc
docker compose --profile sol up -d trader-sol

# OR launch all three at once (requires all 3 .env.{asset} files to exist)
docker compose --profile multi up -d
docker compose logs -f trader-eth trader-btc trader-sol --tail 200
```

**Managing per-instrument containers:**

| Action | Command |
|--------|---------|
| Stop one instrument (graceful) | `docker compose stop trader-eth` |
| Stop all multi-instrument | `docker compose --profile multi stop` |
| Restart after `.env` change | `docker compose restart trader-eth` |
| Restart after code change | `docker compose build trader && docker compose --profile eth up -d trader-eth` |
| Check status | `docker compose ps` |

Each container has `restart: unless-stopped` — restarts automatically on
crashes and host reboots, stays stopped after `docker compose stop`.

Each restart creates a new `run_id` in `strategy_runs`. Filter by
`(trader_id, strategy_id)` in Grafana to keep the three streams separate.

**Important:** Multi-instrument deploys do NOT include the legacy
single-instrument `trader` service. If you previously had `trader`
running and switch to multi-instrument, `docker compose stop trader`
first or the legacy service keeps running on the old `.env` config.

## 4. Verify the Pipeline

### Check PostgreSQL

```bash
docker exec -it ntp-postgres-1 psql -U nautilus -d nautilus_platform
```

```sql
-- Active runs (with trader_id for multi-instrument filtering)
SELECT id, trader_id, strategy_id, instrument_id, run_mode, started_at, stopped_at
FROM strategy_runs ORDER BY started_at DESC LIMIT 10;

-- Fills (after first trade)
SELECT ts, strategy_id, order_side, last_qty, last_px
FROM order_fills ORDER BY ts DESC LIMIT 5;

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

If running on a remote droplet, prefer an SSH tunnel over exposing port 3000:

```bash
ssh -L 3000:localhost:3000 root@<droplet-ip>
# then open http://localhost:3000 in your browser
```

The "Trading Overview" dashboard has six template variables (the first five cascade in order):

| Variable | Filters |
|---|---|
| `trader_id` | One per running container (e.g. `nt-trader-eth`) |
| `strategy_id` | Per-instance strategy ID |
| `instrument_id` | `ETH-USD-PERP.HYPERLIQUID` etc. |
| `run_mode` | `sandbox` \| `live` |
| `run_id` | UUID of the strategy run; refreshes when an upstream variable changes |
| `bar_interval_seconds` | Used by the *Bars Since Last Bar Open* stat. Pick the interval matching the trader you're monitoring (15m / 1h / 4h / 1d). Does not affect any other panel. |

**Panel rows (top to bottom):**

1. **Operational Health** (always visible) — four stats that turn yellow/red when the trader is not keeping up:
   - *Bars Since Last Bar Open* — `(NOW() - MAX(signal_events.ts)) / bar_interval_seconds`. `signal_events.ts` stores the bar's OPEN timestamp (NT's canonical bar key — see `src/strategies/ma_cross.py`), so this gauge oscillates between 1.0 (right after a bar fires) and 2.0 (right before the next bar arrives) during healthy operation. Yellow > 1.5 means the next bar is overdue; red > 2.0 means at least one bar was missed (real wedge). Make sure the *Bar Interval* picker matches the trader.
   - *Time Since Last Fill* — seconds since the last `order_fills` row. High during ranging markets is normal; investigate after > 1 day of no fills.
   - *Last Account Snapshot Age* — `PersistenceActor` writes one every 60s. Yellow > 2 min, red > 5 min indicates executor lag or wedge.
   - *Active Runs* — count of `strategy_runs` with `stopped_at IS NULL` matching the picker filters. Should equal your expected container count.
2. **Signal Gate (Phase 2.5)** (collapsed by default — click the row title to expand):
   - *Signal Stream* — stepwise +1/-1/0 chart of the cross-gate state.
   - *MA Divergence* — relative gap `(fast - slow) / slow`; zero-crossings cause flips.
   - *Bars Since Last Flip*, *Acted / Emitted (24h)*, *Bootstrap Bars* — gate-health stats.
   - *Recent Signal Events* — last 50 rows with `acted=true` highlighted yellow.
3. Balance / Cumulative PnL / Drawdown timeseries panels.
4. **Position analytics**:
   - *Realized PnL Distribution* — histogram of `realized_pnl` per closed position. Tight cluster near zero = chop / cross-flip churn; fat right tail = trend-rider.
   - *Position Duration Distribution* — histogram in minutes (`duration_ns`). For cross-only strategies expect clusters around N × bar_interval.
5. Stats row: Win Rate, Total PnL, Total Trades, **Profit Factor**, Positions Closed, Avg Win / Avg Loss, Max Drawdown. PF reads `n/a` until at least one winner and one loser have closed.
6. **Commission & Slippage** row (collapsed by default — most useful on HL testnet where commissions are non-zero):
   - *Total Commission*, *Commission % of Gross PnL* (yellow > 15%, red > 25%), *TAKER Ratio*.
   - *Cumulative Commission Paid* timeseries.
7. Recent Fills, Recent Positions tables.
8. *Active Runs* (`stopped_at IS NULL`, includes `hours_running`) and *Completed Runs* (`stopped_at IS NOT NULL`, includes `duration_hours`) — side-by-side tables.

**Annotations** are rendered as vertical lines on every timeseries panel:
- *Run starts* (blue) — `started_at` markers for matching runs.
- *Run stops* (red) — `stopped_at` markers.
- *Acted signals* (yellow) — disabled by default. Enable from the annotation control in the top bar when investigating cross-gate alignment.

### Check Telegram (if configured)

You should receive messages for fills, position closes (WIN/LOSS with PnL), and drawdown alerts (>10% from peak).

## 5. Switch Strategies

All hyperparameters flow from `.env`. Edit, then restart your trader:

| Deploy mode | Restart command |
|---|---|
| Native | Ctrl+C the running process, then `python scripts/run_sandbox.py` |
| Single-instrument Docker | `docker compose --profile single restart trader` |
| Per-instrument Docker | `docker compose restart trader-eth` (or `trader-btc` / `trader-sol`) |

No code edits required.

```bash
# Strategy + instrument
STRATEGY=MACross                          # MACross | EMACross | SMACross | HMACross | DEMACross | AMACross | VIDYACross
                                          # | MACrossLongOnly | <prefix>CrossLongOnly | MACrossATR | MACDRSI
INSTRUMENT_ID=BTC-USD-PERP.HYPERLIQUID    # BTC | ETH | SOL
BAR_INTERVAL=4h                           # friendly form — converted to NT bar-type at the boundary
TRADE_NOTIONAL=2000

# Risk management
STOP_PCT=0.05                             # protective-stop fraction (5%). Applies to MACross only.
                                          # Set to a blank string to disable the stop entirely.

# MA crossover family (MACross, *Cross, MACrossLongOnly, *CrossLongOnly)
MA_FAST=10
MA_SLOW=100
MA_TYPE=EMA                               # EMA | SMA | HMA | DEMA | AMA | VIDYA
                                          # (family-specific aliases like EMACross pin MA_TYPE by name)
BOOTSTRAP_ON_DEPLOY=false                 # `true` = act on first observed signal as a synthetic cross
                                          # (use for live mid-trend deploys; keep `false` for verification)
```

Other strategies (MACrossATR, MACDRSI, etc.) have their own env vars — see `src/config/settings.py` for the field names and defaults. Add them to `.env` only when switching to one of those strategies.

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

All MA crossover strategies share `MA_FAST`, `MA_SLOW`, and (for MACross only) `STOP_PCT`. Family-specific aliases override `MA_TYPE`.

The `STOP_PCT` protective stop applies only to `MACross` / `EMACross` / `SMACross` / `HMACross` / `DEMACross` / `AMACross` / `VIDYACross`. The long-only variants and `MACrossATR` / `MACDRSI` do not use the `ProtectiveStopAware` mixin — their stops are baked into their strategy logic differently (long-only has none; ATR uses an ATR-multiple bracket).

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

Smoke-tests the new wiring (signal_events, AccountAliveMonitor, the
protective-stop mixin) before committing to a 2-week testnet run.

```bash
# Migrations (one-shot via single profile)
docker compose --profile single run --rm trader alembic upgrade head

# Pick ONE deploy mode:

# (A) Single-instrument BTC 4h × MACross (legacy / live target shape)
docker compose --profile single up -d

# (B) Per-instrument 15m × tailored MACross config — needs .env.{asset} files
docker compose --profile eth up -d trader-eth
# (and/or trader-btc, trader-sol)

# OR run natively for tighter feedback during initial debugging:
python scripts/run_sandbox.py
```

**Verify after ~4 bars of runtime:**

- 4h bars → ~16 hours wall-clock
- 15m bars → ~1 hour wall-clock
- 1h bars → ~4 hours wall-clock

```sql
-- Per-bar gate stream is flowing (substitute your run_id from strategy_runs)
SELECT COUNT(*) FROM signal_events WHERE run_id = '<run_id>';   -- should be > 0

-- Acted vs observed split
SELECT acted, COUNT(*) FROM signal_events WHERE run_id = '<run_id>' GROUP BY acted;

-- Cross-container check (multi-instrument): every trader gets its own run_id
SELECT trader_id, COUNT(*) AS bars
FROM signal_events s JOIN strategy_runs r ON s.run_id = r.id
WHERE r.started_at > NOW() - INTERVAL '1 day'
GROUP BY trader_id;
```

```bash
# AccountAliveMonitor is subscribed (substitute trader-eth/btc/sol or trader)
docker compose logs trader-eth | grep "AccountAliveMonitor started"

# Per-bar log line from MACross (one per bar after warmup)
docker compose logs trader-eth | grep "cross_gate:"

# Sanity: no blocked-callback warnings
docker compose logs trader-eth | grep -i "blocked"
```

**Stage A pass-through:** any of `{signal_events empty, no AccountAliveMonitor log, blocked-callback warnings, no per-bar `cross_gate:` log}` → fix and re-do. Do NOT proceed to Stage B with broken plumbing.

### Stage B — HL testnet (~2 weeks)

The real revalidation. Uses `scripts/run_live.py` against the actual HL
testnet exchange (real orders, fake money).

```bash
# .env settings to switch into live mode:
#   TRADING_SCRIPT=scripts/run_live.py
#   HL_PRIVATE_KEY=<testnet key>
#   HL_TESTNET=true
#   LIVE_CONFIRM=yes

docker compose build trader

# Pick ONE — same profile choice as Stage A, just with live-mode env vars:
docker compose --profile single up -d                       # single-instrument
docker compose --profile eth up -d trader-eth               # per-instrument ETH
# docker compose --profile multi up -d                      # all three at once
```

Pass criteria (gates Phase 2.6):

- [ ] **Closed positions** — at least enough to spot-check the persistence + alert pipeline. Original plan target was ≥20; multi-instrument deploys may hit this combined across containers, single-slow-MA deploys may not. Document the actual count + your justification.
- [ ] Every fill in `order_fills` matches an HL testnet UI fill (spot-check 5 random rows).
- [ ] `docker compose logs <trader-service> | grep -c "blocked-callback"` == 0 for each running trader container.
- [ ] At least one drawdown alert OR documented reason none fired (e.g. peak drawdown stayed under the 10% threshold).
- [ ] No unhandled exceptions in `docker compose logs <trader-service>`.
- [ ] At least one protective-stop fill present (`order_side` matching a closing direction within 5% of entry price) — verifies the `STOP_PCT` wiring fired.

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
6. Then in `.env`: set `HL_TESTNET=false`, add `HL_PRIVATE_KEY` + `HL_WALLET_ADDRESS`, set `TRADING_SCRIPT=scripts/run_live.py`.
   - **Native:** `python scripts/run_live.py` (interactive confirmation prompt)
   - **Docker:** Also set `LIVE_CONFIRM=yes` in `.env` (bypasses the interactive prompt — `input()` requires a TTY which containers don't have), then start using the same profile flag you've been using:
     - Single-instrument: `docker compose --profile single restart trader` (or `up -d` if not running)
     - Per-instrument: `docker compose restart trader-eth` (or btc/sol)

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
| Trader container restart loop | Check `docker compose logs <service> --tail 50` (e.g. `trader`, `trader-eth`). Common cause: migrations not run — run `docker compose --profile single run --rm trader alembic upgrade head`. |
| `docker compose stop` but `stopped_at` is NULL | SIGTERM handler issue — verify the runner has the `signal.signal(signal.SIGTERM, ...)` handler at the top of `main()`. |
| Live trading restart loop in Docker | `input()` requires a TTY. Set `LIVE_CONFIRM=yes` in `.env` for containerized live trading. |
| `env file .env.eth not found` | You're starting a per-instrument profile but haven't copied the template. Run `cp .env.eth.example .env.eth` (or `.env.btc` / `.env.sol`) and fill in the values. |
| `docker compose up -d` doesn't start any trader | Expected. Trader services are profile-gated since the multi-instrument PR. Use `--profile single` (legacy) or `--profile eth`/`btc`/`sol`/`multi`. |
| Protective stop never fires | After PR #29, `STOP_PCT` in `.env` (or `.env.{asset}` for per-instrument) is read by the runner and threaded into `MACrossConfig.stop_pct`. If you see no protective-stop fills, verify `STOP_PCT` is set non-blank in the right file. Blank or unset → mixin disabled by design. |

## Files Reference

| File | Purpose |
|------|---------|
| `scripts/run_sandbox.py` | Paper trading runner (sandbox exec — simulated fills against real HL data) |
| `scripts/run_live.py` | Live / testnet trading runner (real exec — testnet or mainnet via `HL_TESTNET`) |
| `src/actors/persistence.py` | Writes fills, positions, account snapshots, and signal events to PostgreSQL |
| `src/actors/alert.py` | Telegram notifications |
| `src/actors/account_alive.py` | `AccountAliveMonitor` actor — halts trading when equity drops below alive floor |
| `src/config/settings.py` | All settings from `.env` |
| `src/core/protective_stop_mixin.py` | `ProtectiveStopAware` strategy mixin — armed via `STOP_PCT` |
| `docker-compose.yml` | Infra + trader services (profile-gated `single` / `eth` / `btc` / `sol` / `multi`) |
| `.env` | Your local base config (gitignored) |
| `.env.example` | Template for base config (committed) |
| `.env.{eth,btc,sol}` | Per-instrument overrides for multi-instrument deploys (gitignored) |
| `.env.{eth,btc,sol}.example` | Templates showing the schema of per-instrument files (committed) |
| `grafana/dashboards/trading.json` | Grafana dashboard definition |
