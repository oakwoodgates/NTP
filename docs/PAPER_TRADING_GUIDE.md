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

**REQUIRED — set strong values before bringing infra up:**

| Field | Why | How to generate |
|---|---|---|
| `POSTGRES_PASSWORD` | asyncpg auth | `openssl rand -base64 32` |
| `REDIS_PASSWORD` | Redis container is launched with `--requirepass` (PR #45). Trader fails at `node.build()` with `NOAUTH` if missing. | `openssl rand -base64 32` |
| `GRAFANA_PASSWORD` | Seeded into Grafana's SQLite on first container start (rotate via `grafana-cli admin reset-admin-password` after init). | `openssl rand -base64 32` |

Optional but recommended — Telegram alerts:

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

Factory-default config (`src/config/settings.py`): MACross-EMA(`MA_FAST=10`, `MA_SLOW=100`) on `BTC-USD-PERP.HYPERLIQUID`, 4-hour bars, $1000 starting capital, $2000 USD notional per trade, 20× leverage, 5% protective stop. All hyperparameters flow from `.env` — see [`CONFIG.md`](CONFIG.md) for the per-system field map.

> **Deployed configs may differ.** What's actually running on the DO box (instrument, bar interval, MA windows) lives in `.env` and per-instrument `.env.{asset}` files on the deployment host — gitignored, per the findings-stay-local rule. The factory default above is what `python scripts/run_sandbox.py` does on a freshly-cloned dev machine with no `.env` overrides.

**What happens:**
- Registers a `strategy_runs` row in PostgreSQL with a unique `run_id` + `trader_id`
- Connects to live Hyperliquid market data (real prices, simulated execution via `PatchedSandboxExecutionClient` — our subclass of NT's `SandboxExecutionClient` that installs `BestPriceFillModel` to work around an NT 1.227.0 partial-fill race; see [`SANDBOX_PARTIAL_FILL_AUDIT.md`](SANDBOX_PARTIAL_FILL_AUDIT.md))
- MACross generates trades on moving-average crosses (EMA by default), gated to act only on fresh transitions (see [`STRATEGY_ENTRY_RULES.md`](STRATEGY_ENTRY_RULES.md))
- Every fill → PersistenceActor writes to `order_fills` + AlertActor sends Telegram
- Every position close → writes to `positions` + Telegram WIN/LOSS message
- Every bar (after warmup) → `signal_events` row with the per-bar gate state
- Every 60s → account balance snapshot to `account_snapshots`
- 5% protective stop fires reduce-only when an open position moves 5% against entry
- Ctrl+C → graceful shutdown, `strategy_runs.stopped_at` updated

**First trade timing:** MACross needs `MA_SLOW` bars of warmup before producing signals. At the default `MA_SLOW=100` on 4-hour bars, that's **100 × 4h = ~17 days** of warmup before the first cross can fire. NT backfills historical bars on start, so warmup completes within minutes of process start — but the first ACTED signal then requires a fresh cross transition, which on slow MAs at 4h is roughly once every 1-3 weeks under typical conditions.

**Skip the wait with `BOOTSTRAP_ON_DEPLOY=true`:** the strategy treats the first observed signal direction after warmup as a synthetic cross and fires immediately. Useful for **live deploys mid-trend** where you want to catch the current move. **Leave `false` for Phase 2.5/2.6 verification** — a synthetic deploy-time entry muddies the backtest-vs-paper signal-alignment analysis (Tool 1). See [`STRATEGY_ENTRY_RULES.md`](STRATEGY_ENTRY_RULES.md) for the rationale.

**Restarts do NOT re-arm bootstrap.** PR #42's `on_save`/`on_load` persists `cross_gate_bootstrap_pending` into NT's Redis cache. After the first bootstrap fires on the first run, the flag is flipped to `False` and saved. Every subsequent restart loads `False` from Redis, so `docker compose restart`/code deploys never synthesize a second deploy-time entry. A fresh `BOOTSTRAP_ON_DEPLOY=true` window only triggers when the Redis state is wiped (clean-restart workflow — see `## Wipe Database` in [`DEPLOY.md`](DEPLOY.md)).

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

**Restart semantics** — every process start inserts a fresh row.
A redeploy that takes 5 minutes shows up as two rows, not one. The new
row's `parent_run_id` column points at the previous row for the same
`(trader_id, instrument_id, strategy_id, run_mode)` tuple so you can
walk the chain forward or backward — see
[Cross-restart queries](#cross-restart-queries) below. First-ever run
for a tuple has `parent_run_id IS NULL`; a sandbox → live transition
also breaks the chain (`run_mode` is part of the lookup key).

**Important:** Multi-instrument deploys do NOT include the legacy
single-instrument `trader` service. If you previously had `trader`
running and switch to multi-instrument, `docker compose stop trader`
first or the legacy service keeps running on the old `.env` config.

### Deploy lifecycle — graceful stop is position-neutral

`docker compose stop <trader>` (and any restart that goes through it —
`restart`, `up -d --force-recreate` after a code change, host shutdown)
sends SIGTERM, the runner's signal handler raises `KeyboardInterrupt`,
the `finally` block calls `node.stop()` → `strategy.on_stop()`.

In paper/live mode this **does NOT flatten open positions**. The
runners explicitly construct strategy configs with
`close_positions_on_stop=False`, so `on_stop()` only cancels working
orders and unsubscribes from market data. The position itself stays
open in NT's Redis cache; PR #42's `on_save`/`on_load` persists the
strategy's cross-gate + mixin state alongside it. On the next start,
both the position and the strategy state are rehydrated — the strategy
resumes exactly where it left off.

**Why this default:** without it, every code deploy generates a
synthetic `shutdown_flatten` exit followed by a synthetic re-entry on
the next bar, polluting the trade history and breaking the
"backtest-vs-paper" comparison (Phase 2.6 Tool 1).
`shutdown_flatten`-tagged closes are easy to filter — but easier to
just not create them.

**To deliberately flatten before a code change** (e.g., shipping a
risky strategy logic change you don't want to ride through), close
positions from the live ExecClient first, then stop:

```bash
# (TODO: add scripts/panic_flatten.py — until then, close manually
# via the venue UI or a one-shot script that submits reduce-only
# market orders for each open position.)
docker compose stop trader-eth
```

**Backtests are unaffected** — `MACrossConfig.close_positions_on_stop`
still defaults to `True` everywhere else (notebooks, batch runner,
sweep helpers). The override is runner-only.

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

#### Cross-restart queries

A restart cycle (e.g. redeploying the trader) inserts a new
`strategy_runs` row rather than reusing the previous one. The
`parent_run_id` column links each row to the one that immediately
preceded it for the same
`(trader_id, instrument_id, strategy_id, run_mode)` tuple, so all the
activity across an outage is still walkable in a single query.

```sql
-- All run_ids in this trader's chain, newest first (give it any one
-- run_id from the chain — the CTE walks both directions).
WITH RECURSIVE chain AS (
    SELECT id, parent_run_id, started_at, stopped_at
    FROM strategy_runs WHERE id = '<any run_id in the chain>'
    UNION ALL
    SELECT r.id, r.parent_run_id, r.started_at, r.stopped_at
    FROM strategy_runs r JOIN chain c ON r.parent_run_id = c.id OR r.id = c.parent_run_id
)
SELECT * FROM chain ORDER BY started_at DESC;

-- All fills for an entire trader/instrument over its whole history
-- (walk back to the chain root, then join).
WITH RECURSIVE chain AS (
    SELECT id, parent_run_id FROM strategy_runs
    WHERE trader_id = 'nt-trader-eth'
      AND instrument_id = 'ETH-USD-PERP.HYPERLIQUID'
      AND strategy_id  = 'MACross-EMA-10-100'
      AND run_mode     = 'sandbox'
      AND stopped_at IS NULL                 -- start from the active row
    UNION ALL
    SELECT r.id, r.parent_run_id
    FROM strategy_runs r JOIN chain c ON r.id = c.parent_run_id
)
SELECT f.* FROM order_fills f JOIN chain c ON f.run_id = c.id
ORDER BY f.ts DESC;
```

Single-run queries (the common case) don't need the CTE — just filter
`WHERE run_id = '<one uuid>'` against `order_fills`, `positions`,
`signal_events`, `account_snapshots` as before.

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
3. Balance / PnL / Drawdown / Position-PnL panels (unchanged from prior version).
4. Stats row: Win Rate, Total PnL, Total Trades, Positions Closed, Avg Win / Avg Loss, Max Drawdown.
5. Recent Fills, Recent Positions tables.
6. *All Runs (matching filters)* table — active runs (no `stopped_at`) highlighted green.

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

The dedicated tool: **`notebooks/paper_vs_backtest.ipynb`** (Phase 2.6
Tool 1). Two-layer comparison:

1. **Layer 1 — signal stream (primary).** Loads `signal_events` for the
   paper run, re-runs the same strategy in `BacktestEngine` over the
   matched window, joins per-bar gate decisions. Headline metric is
   **direction match rate** — what fraction of bars did paper and
   backtest agree on `signal=+1` vs `-1`?
2. **Layer 2 — position stream (secondary).** Only meaningful when
   layer 1 alignment is high (≥90%). Measures entry-time, entry-price,
   and realized-PnL gaps on positions that both sides took. Headline
   metric is the **PnL haircut** (paper PnL / backtest PnL − 1).

Driver workflow:

```python
# In paper_vs_backtest.ipynb, cell 2:
USE_SYNTHETIC_DATA = False
RUN_ID = '<UUID from strategy_runs>'
INSTRUMENT_ID = 'ETH-USD-PERP.HYPERLIQUID'
BAR_INTERVAL = '15m'
P26_FAST, P26_SLOW, P26_MA_TYPE = 15, 35, 'EMA'  # MUST match paper

# Cell 3 then handles the rest: loads paper signals + positions from
# PG, computes the right warmup-matched backtest data window via
# compute_backtest_warmup_start, runs the backtest, and produces both
# stream pairs.
```

The notebook prints a verdict-snapshot block in cell 8 — paste into
`reports/decisions/PHASE_2_6_VERDICT.md` (gitignored) when a Stage B
run yields meaningful numbers.

**If layer-1 direction match is unexpectedly low** (<90% on a clean
v3+ run), the most common cause is EMA warmup divergence. Bump
`WARMUP_MULTIPLIER` in cell 2 from `2.0` to `3.0` or `4.0` to load
more pre-window data and re-run. For exact parity, extract the actual
`RequestBars(start=...)` value from the live trader's startup log and
pass it directly to `load_backtest_data`.

For ad-hoc PG-only queries (no backtest re-run):

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
```

## 6. Daily Monitoring

1. **Grafana** — check balance curve, fill frequency, PnL distribution
2. **Telegram** — review fill notifications, watch for drawdown alerts
3. **PostgreSQL** — query for anomalies
4. **Terminal** — check for errors in TradingNode stdout
5. **Compare to backtest** — if paper results lag backtest by >30-40%, investigate

## 7. NautilusTrader upgrades

This project currently pins NautilusTrader `1.227.0` in `pyproject.toml`. When
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
| Trader restart loop with `RuntimeError: "NOAUTH": Authentication required` | `REDIS_PASSWORD` missing/wrong in `.env`. Set it (`openssl rand -base64 32`), then `docker compose up -d --force-recreate <trader-svc>`. The redis container reads `REDIS_PASSWORD` at start time via `command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}"]`; if you change the value, `docker compose up -d --force-recreate redis` too. |
| Postgres data missing after `docker compose down` | `pgdata` volume mount target mismatch (PR #46 fix). Verify `docker-compose.yml` mounts `pgdata:/var/lib/postgresql/data`, NOT the legacy `/home/postgres/pgdata/data`. Regression test at `tests/unit/test_compose_pgdata_mount.py`. |
| Trader on a per-instrument profile crashloops with `NOAUTH` after a `docker compose build trader` | Likely you're on an old `docker-compose.yml` without PR #47 (single shared image). Pull main, then `docker compose build trader` once rebuilds the single `ntp-trader:latest` used by every profile. |
| Grafana panels empty | Data needs time to accumulate. Check datasource in Grafana → Settings → Data sources. |
| Trader container restart loop | Check `docker compose logs <service> --tail 50` (e.g. `trader`, `trader-eth`). Common cause: migrations not run — run `docker compose --profile single run --rm trader alembic upgrade head`. |
| `docker compose stop` but `stopped_at` is NULL | Either: (a) SIGTERM handler issue — verify the runner has the `signal.signal(signal.SIGTERM, ...)` handler at the top of `main()`; or (b) the trader crashed during startup before `_register_run` completed. PR #39's `try/finally` guards against (b) for newer runs; legacy orphan rows from before PR #39 can be cleaned with `UPDATE strategy_runs SET stopped_at = started_at + INTERVAL '5 minutes' WHERE stopped_at IS NULL AND started_at < NOW() - INTERVAL '1 day'`. SIGKILL bypasses both — see [`PROTECTIVE_STOP_RESTART_AUDIT.md`](PROTECTIVE_STOP_RESTART_AUDIT.md). |
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
