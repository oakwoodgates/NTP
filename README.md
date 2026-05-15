# Nautilus Trading Platform

A crypto algorithmic trading platform built on [NautilusTrader](https://nautilustrader.io/), with custom Actors for persistence and alerting, research tooling for strategy validation, and Grafana for monitoring.

## Status

**Phase 3a (research tooling) — complete.** Sweep persistence, cross-instrument comparison, walk-forward, bootstrap CIs, 8-check validation, validate-all consolidator, batch backtest runner, and centralized config flow are all in place. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what's next (Phase 2.5 paper-trading revalidation → Phase 2.6 backtest accuracy validation → Phase 3 live trading).

**Quick start:**

```bash
cp .env.example .env             # edit STARTING_CAPITAL, TRADE_NOTIONAL, etc.
python scripts/batch_backtest.py --dry-run    # list combos
python scripts/batch_backtest.py              # full BTC/ETH/SOL × 4h/1d × 5%/10% grid
```

**Key docs:**
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phase plan and gates
- [`docs/CONFIG.md`](docs/CONFIG.md) — settings.py vs .env, override patterns
- [`docs/BATCH_BACKTEST.md`](docs/BATCH_BACKTEST.md) — headless cross-product runner
- [`docs/STRATEGY_ENTRY_RULES.md`](docs/STRATEGY_ENTRY_RULES.md) — cross-gate contract for new strategies
- [`docs/BAR_BACKTESTING_GOTCHAS.md`](docs/BAR_BACKTESTING_GOTCHAS.md) — bar-fill artifacts to expect
- [`docs/LIQUIDATION_AND_SIZING.md`](docs/LIQUIDATION_AND_SIZING.md) — liq simulator + protective stop mixins
- [`docs/ANALYZER_RETURNS_CAVEAT.md`](docs/ANALYZER_RETURNS_CAVEAT.md) — why we don't trust NT's Sharpe

## Architecture

**Modular monolith, event-driven.** NautilusTrader is the core engine (installed as a pip dependency). Everything else — Actors, persistence, alerting, research tooling, API, frontend — is custom code that orchestrates NT.

```
┌─────────────────────────────────────────────────────┐  Phase 3a (current)
│              Jupyter Research Notebooks               │
│   Sweep → Parquet · Compare · Validate · Charts      │
└──────────────────────┬──────────────────────────────┘
                       │ backtesting/engine.py
                       │ data/sweeps/*.parquet
┌──────────────────────┴──────────────────────────────┐
│              NautilusTrader Engine                    │
│                                                      │
│  Strategies · Actors · RiskEngine · ExecutionEngine  │
│  BacktestEngine · TradingNode · MessageBus           │
│  Exchange Adapters (Hyperliquid, Binance, Bybit...)  │
│  ParquetDataCatalog · FillModel · Portfolio          │
└──────┬───────────────────────────────────┬──────────┘
       │                                   │
┌──────┴──────────┐              ┌─────────┴──────────┐
│   PostgreSQL    │              │      Redis          │
│  + TimescaleDB  │              │  Cache              │
│                 │              │                     │
│ Fills, positions│              │ Live state (NT)     │
│ Account history │              │                     │
│ Strategy meta   │              │                     │
└──────┬──────────┘              └────────────────────┘
       │ writes                   │ alerts
┌──────┴──────────┐    ┌──────────┴──────────┐
│ PersistenceActor│    │    AlertActor        │  Phase 2 (complete)
│ (inside node)   │    │   (inside node)      │
└─────────────────┘    └─────────────────────┘
                                 │ Telegram
┌──────────────────┐
│     Grafana      │  Phase 2 — reads PostgreSQL
│  Balance · PnL   │  Not locked in
│  Fills · Stats   │
└──────────────────┘

┌─────────────────────────────────────────────────────┐  Phase 3b (future)
│  React Frontend ←WS/REST→ FastAPI ←Redis Streams→   │
│  StreamingActor (inside TradingNode)                 │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **NT as a library, not a fork.** We subclass `Strategy`, `Actor`, configure engines, call `node.run()`. NT's repo is never modified.
- **Actors are the extension point.** `PersistenceActor` and `AlertActor` live inside the TradingNode process, subscribe to NT's MessageBus, and do the work (DB writes, Telegram) via `run_in_executor()` — I/O runs in a thread pool without blocking the event loop.
- **PostgreSQL + TimescaleDB** for all persistent data. Prices stored as `NUMERIC` — never floats.
- **Parquet for sweep results.** Parameter sweep outputs persist to `data/sweeps/` as Parquet files, one per strategy × instrument × interval. No database needed for research data — files on disk, read back with `load_sweeps()`.
- **Redis** for real-time layer. NT uses it natively for cache; we add a `StreamingActor` in Phase 3b for bridging trade events to the frontend.
- **NT's ParquetDataCatalog** for feeding historical data to the backtester. Coexists with TimescaleDB (Parquet for NT, TimescaleDB for API queries).
- **Grafana for ambient monitoring** in Phase 2. Reads PostgreSQL directly. Not locked in — replace with any tool at any time without touching the persistence layer.
- **Research before UI.** Phase 3a delivers research tooling and strategy validation. The custom React frontend is Phase 3b, built when multiple validated strategies are running live and Grafana isn't enough.
- **Event-driven everywhere.** NT's MessageBus is the backbone. Custom Actors bridge events to persistence and the frontend. No polling loops.

## Project Structure

```
├── src/
│   ├── strategies/          # NT Strategy subclasses
│   │   ├── ma_cross.py         # Unified MA crossover (EMA/SMA/HMA/DEMA/AMA/VIDYA)
│   │   ├── bb_meanrev.py
│   │   ├── donchian_breakout.py
│   │   ├── ma_cross_atr.py            # MA crossover + ATR bracket TP/SL (all MA types)
│   │   ├── ma_cross_bracket.py          # MA regime + symmetric ATR bracket exits (all MA types)
│   │   ├── ma_cross_long_only.py       # Long-only MA crossover (all MA types)
│   │   ├── ma_cross_stop_entry.py          # MA regime + breakout entry + trailing stop (all MA types)
│   │   ├── ma_cross_take_profit.py             # MA crossover + pct take-profit (all MA types)
│   │   ├── ma_cross_trailing_stop.py    # MA crossover + ATR trailing stop (all MA types)
│   │   ├── macd_rsi.py
│   │   └── ...
│   ├── actors/              # Custom NT Actors
│   │   ├── persistence.py   # PersistenceActor — writes fills/positions to PostgreSQL
│   │   └── alert.py         # AlertActor — Telegram notifications
│   ├── backtesting/         # Backtest orchestration
│   │   └── engine.py        # make_engine, run_single_backtest, run_sweep,
│   │                        # load_sweeps, run_walk_forward
│   ├── persistence/         # SQLAlchemy Core table definitions (no ORM)
│   │   └── schema.py
│   ├── config/              # Pydantic Settings
│   │   └── settings.py      # get_settings() — single source of truth
│   ├── api/                 # FastAPI application (Phase 3b)
│   └── core/                # Type aliases, constants, instruments, mixins, pure utils
│       ├── constants.py
│       ├── instruments.py
│       ├── liquidation.py             # LiquidationConfig, PositionLiquidated/AccountLiquidated events
│       ├── liquidation_mixin.py       # LiquidationAware — places cross-margin liq stop per position
│       ├── protective_stop_mixin.py   # ProtectiveStopAware — places fixed-pct protective stop per position
│       ├── sizing.py                  # SizingConfig, compute_notional (fixed / equity_frac modes)
│       ├── venues.py                  # VenueConfig + per-venue defaults (mm_rate, fees)
│       └── utils.py
├── grafana/
│   ├── provisioning/        # Declarative datasource + dashboard config
│   └── dashboards/          # Dashboard JSON (committed)
├── notebooks/               # Jupyter research + validation
│   ├── backtest/             # Per-strategy backtest + sweep notebooks
│   │   ├── ma_cross.ipynb           # MA crossover backtest + sweep — covers all
│   │   │                            #   6 MA types (EMA/SMA/HMA/DEMA/AMA/VIDYA)
│   │   │                            #   via MA_TYPE selector in cell 1.1
│   │   ├── ma_cross_atr.ipynb       # MA crossover + ATR bracket (TP + SL)
│   │   ├── ma_cross_bracket.ipynb   # MA crossover + symmetric ATR bracket
│   │   ├── ma_cross_long_only.ipynb # MA crossover, long-only variant
│   │   ├── ma_cross_stop_entry.ipynb # MA cross + stop-entry confirmation + trailing stop
│   │   ├── ma_cross_take_profit.ipynb        # MA crossover + percentage take-profit
│   │   ├── ma_cross_trailing_stop.ipynb # MA crossover + ATR-multiple trailing stop
│   │   ├── ma_cross_stop_loss.ipynb     # MA crossover + protective stop-loss sensitivity
│   │   │                                #   (5% default = isolated-margin equivalent at 20× lev)
│   │   ├── bb_meanrev.ipynb
│   │   ├── macd_rsi.ipynb
│   │   └── donchian_breakout.ipynb
│   ├── verify/               # Data pipeline + signal verification
│   │   ├── 01_pipeline.ipynb        # Data pipeline verification
│   │   ├── 02_data.ipynb            # Catalog vs exchange spot-checks
│   │   ├── 03_signals.ipynb         # Indicator / signal verification
│   │   └── 04_persistence.ipynb     # DB persistence verification
│   ├── compare_sweeps.ipynb       # Cross-instrument/timeframe comparison
│   ├── validate_strategy.ipynb    # 8-check go/no-go verdict per (instrument, combo)
│   ├── validate_all.ipynb         # Strategy-level matrix consolidator
│   ├── review_live_run.ipynb      # Post-run analysis of live/paper trades
│   ├── charts.py                  # Plotting helpers (plotly, matplotlib, TVLC reports)
│   ├── utils.py                   # Shared notebook helpers (load_sweeps_filtered,
│   │                              #   print_validation_verdict, save_notebook_snapshot, ...)
│   ├── _compare_helpers.py        # Notebook-private helpers for compare_sweeps
│   └── _validate_helpers.py       # Notebook-private helpers for validate_strategy
│                                  #   (STRATEGIES registry, plateau scoring, ...)
├── scripts/
│   ├── _catalog.py            # Shared utilities for data fetch scripts (crash-safe writes)
│   ├── fetch_hl_candles.py    # Hyperliquid OHLCV data fetcher
│   ├── fetch_binance_candles.py # Binance OHLCV data fetcher (Futures + Spot via --market)
│   ├── run_sandbox.py         # Paper trading runner (SandboxExecutionClient)
│   └── run_live.py            # Live trading runner (HyperliquidExecClient)
├── data/
│   ├── catalog/             # ParquetDataCatalog root (gitignored)
│   └── sweeps/              # Sweep result Parquet files (gitignored)
├── reports/                 # Generated reports (gitignored)
│   ├── backtest/            # TradingView Lightweight Charts HTML reports
│   ├── html/                # Exported notebook HTML snapshots
│   ├── notebooks/           # Copied notebook snapshots (.ipynb)
│   └── tearsheets/          # NT tearsheet HTML (saved when SAVE_TEARSHEET=True)
├── tests/
│   ├── unit/
│   │   ├── test_core.py
│   │   ├── test_catalog.py    # Crash-safe write recovery + swap tests
│   │   ├── test_schema.py
│   │   ├── test_settings.py
│   │   └── test_actors.py
│   └── integration/
├── alembic/                 # DB migrations
├── frontend/                # React application (Phase 3b)
├── pyproject.toml
├── Dockerfile               # Trader container (run_sandbox.py / run_live.py)
├── docker-entrypoint.sh     # Entrypoint — passthrough for ad-hoc cmds, exec Python as PID 1
├── .dockerignore
├── docker-compose.yml       # PostgreSQL + TimescaleDB + Redis + Grafana + trader
├── .env.example             # Secrets template (committed)
├── CLAUDE.md
└── README.md
```

### Dependency Direction

Dependencies flow inward — outer layers depend on inner layers, never the reverse:

```
core/                       ← depends on nothing internal
  ↑
strategies/, actors/        ← depend on core/ only
  ↑
backtesting/, persistence/  ← depend on core/ only
  ↑
api/                        ← outermost layer, can import from anything
```

`core/` is kept intentionally tight: NT type aliases, constants, interface protocols (`typing.Protocol`), and pure utility functions. No business logic, no DB code, no API schemas.

## Prerequisites

- Python 3.12+ (NT requirement)
- Docker + Docker Compose (PostgreSQL + TimescaleDB, Redis, Grafana)
- A Hyperliquid wallet private key (for live/paper trading)

## Setup

### Phase 1 (complete) — NT native workflow

```bash
git clone <repo-url>
cd NTP
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Fetch historical data for backtesting (run from project root)
python scripts/fetch_hl_candles.py               # Hyperliquid candles
python scripts/fetch_binance_candles.py           # Binance Futures candles (may need VPN)
python scripts/fetch_binance_candles.py --market spot  # Binance Spot candles

jupyter notebook notebooks/
```

### Phase 2 (complete) — Paper + live trading

```bash
cp .env.example .env
# Edit .env — POSTGRES_PASSWORD required; TELEGRAM_TOKEN/CHAT_ID recommended;
# HL credentials only needed for live (sandbox does NOT need them).

# Build trader image
docker compose build trader

# Start infra (postgres + redis + grafana). Trader services are profile-gated;
# `up -d` alone no longer starts a trader.
docker compose up -d

# Run migrations once before starting any trader
docker compose --profile single run --rm trader alembic upgrade head

# Pick a profile and bring up a trader.
# (A) Single-instrument (legacy default):
docker compose --profile single up -d trader

# (B) OR per-instrument multi-instrument deploy (Phase 2.5/2.6 verification):
cp .env.eth.example .env.eth          # then edit with your picks
docker compose --profile eth up -d trader-eth
# (similarly trader-btc / trader-sol; or `--profile multi` for all three)

# Tail logs (substitute trader-eth/btc/sol if multi)
docker compose logs -f trader --tail 200

# Monitoring
open http://localhost:3000   # Grafana (admin / your GRAFANA_PASSWORD)
```

To run the trader natively instead (quick iteration / debugging):

```bash
docker compose up -d                  # just infra (postgres, redis, grafana)
alembic upgrade head
python scripts/run_sandbox.py
```

### Phase 3a (current) — Research + validation

```bash
# Already set up from Phase 1. Just open notebooks:
jupyter notebook notebooks/

# Workflow:
# 1. backtest_*.ipynb (per instrument) → run_sweep() → data/sweeps/*.parquet
# 2. compare_sweeps.ipynb → cross-sweep table, stability + CV column
# 3. validate_strategy.ipynb (per instrument, optional override) → 8-check
#    verdict per (instrument, combo), drops JSON to reports/validate/
# 4. validate_all.ipynb → reads all verdict JSONs, renders strategy-level
#    comparison matrix + per-check failure-rate
```

## Usage

### Develop and validate strategies (Phase 3a)

This is the current focus. The research workflow:

1. **Write a strategy** in `src/strategies/`. Subclass `Strategy`, implement `on_start()` and `on_bar()`.
2. **Sweep parameters** in a `backtest_*.ipynb` notebook using `run_sweep()`. Run for each instrument you want to test (BTC/ETH/SOL/...). Results auto-save to `data/sweeps/`.
3. **Compare across instruments.** Open `compare_sweeps.ipynb`, Run All. Review the best-params table, side-by-side heatmaps with liquidated-cell flags, and the parameter-stability table (sorted by avg PnL%, with `cv_pnl_pct` showing cross-instrument stability). Pick a candidate combo — typically the cross-sweep robust one with low CV.
4. **Validate per (instrument, combo).** Open `validate_strategy.ipynb`. Edit cell 1.1 to set the instrument; optionally set `OVERRIDE_PARAMS` to validate a specific combo instead of the per-sweep best. Run All — produces an 8-check verdict (plateau, walk-forward, param-stability, bootstrap, rolling, fee, regime, yearly concentration) and drops a JSON to `reports/validate/`. Repeat for each (instrument, pick) you care about.
5. **Strategy-level rollup.** Open `validate_all.ipynb`, Run All. The comparison matrix shows which checks consistently flag across instruments — separates strategy-level signal from instrument-specific noise.
6. **Paper trade validated strategies** via Phase 2 infrastructure — only when no check is consistently 🚩 across instruments.

### Run paper trading (Phase 2)

Requires infrastructure running first (`docker compose up -d` + migrations). All trader services are profile-gated — see [`docs/PAPER_TRADING_GUIDE.md`](docs/PAPER_TRADING_GUIDE.md) for the full single-vs-multi-instrument walkthrough.

**Docker (recommended for multi-day runs):**

```bash
docker compose up -d                       # infra only (postgres + redis + grafana)
docker compose --profile single up -d trader   # single-instrument legacy path
# OR
docker compose --profile eth up -d trader-eth  # per-instrument (needs .env.eth)

docker compose logs -f trader              # tail logs (or trader-eth/btc/sol)
docker compose stop trader                 # graceful shutdown (SIGTERM → node.stop())
```

**Native (quick iteration):**

```bash
docker compose up -d                       # just infra
python scripts/run_sandbox.py              # Ctrl+C for graceful shutdown
```

Uses NT's `SandboxExecutionClient` against live Hyperliquid market data. Every fill and closed position persists to PostgreSQL via `PersistenceActor`. Telegram alerts fire on fills and position changes (if `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`). Monitor at `http://localhost:3000`.

### Run live trading (Phase 2 — after paper validation)

```bash
# Native — interactive confirmation prompt
# Requires HL_TESTNET=false + HL_PRIVATE_KEY in .env
python scripts/run_live.py

# Docker — set in .env: TRADING_SCRIPT=scripts/run_live.py, HL_TESTNET=false,
#           LIVE_CONFIRM=yes, HL_PRIVATE_KEY=<key>
docker compose restart trader
```

## Development Phases

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Strategy development + backtesting (NT native workflow, Jupyter) | ✅ Complete |
| 2 | TradingNode code, PersistenceActor, AlertActor, paper + live runners | ✅ Complete |
| 3a | Research tooling — sweep persistence, comparison, walk-forward, bootstrap CIs, validate, batch runner, centralized config | ✅ Complete |
| 2.5 | Paper-trading revalidation against post-cross-gate strategies | 🟡 Next ([`ROADMAP.md`](docs/ROADMAP.md)) |
| 2.6 | Backtest accuracy validation — paper vs backtest divergence within tolerance | ⬜ Planned |
| 3 | Live trading — capital deployment after 2.5 + 2.6 pass | ⬜ Planned |
| 4 | Multi-strategy portfolio + correlation tooling | ⬜ Planned |
| 5 | NT v2 migration (tracking [nautechsystems/nautilus_trader#4042](https://github.com/nautechsystems/nautilus_trader/issues/4042)) | ⬜ Watching |
| 3b | Web layer — FastAPI gateway, React frontend, StreamingActor, Redis Streams | ⬜ Deferred |

## Key Constraints

- **NautilusTrader is pre-v2.0** — pin the version, expect API breakage between releases.
- **No floats for prices** — NT uses 128-bit fixed-point. Maintain this in PostgreSQL (`NUMERIC`), asyncpg inserts (`str(nt_type)`), API responses (string-encoded decimals), and frontend.
- **Actor callbacks must never block** — use `self.run_in_executor()` for all I/O. Blocking the event loop stalls the TradingNode.
- **TradingNode is not Jupyter-compatible** — asyncio event loop conflicts. Run from scripts, not notebooks.
- **The "NT + web dashboard" pattern has no community precedent.** When stuck, read NT source code — docs and community posts won't cover integration patterns.
- **LGPL-3.0 license** — NT can be used as a library without affecting your project's license, but modifications to NT's own source must be shared.
