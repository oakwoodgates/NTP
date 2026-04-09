# Nautilus Trading Platform

A crypto algorithmic trading platform built on [NautilusTrader](https://nautilustrader.io/), with custom Actors for persistence and alerting, research tooling for strategy validation, and Grafana for monitoring.

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
│   │   ├── ema_cross_bracket.py
│   │   ├── ma_cross_long_only.py       # Long-only MA crossover (all MA types)
│   │   ├── ema_cross_stop_entry.py
│   │   ├── ema_cross_tp.py
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
│   └── core/                # Type aliases, constants, instruments, pure utils
│       ├── constants.py
│       ├── instruments.py
│       └── utils.py
├── grafana/
│   ├── provisioning/        # Declarative datasource + dashboard config
│   └── dashboards/          # Dashboard JSON (committed)
├── notebooks/               # Jupyter research + validation
│   ├── backtest_ema_cross.ipynb       # EMA crossover backtest + sweep
│   ├── backtest_sma_cross.ipynb       # SMA crossover backtest + sweep
│   ├── backtest_hma_cross.ipynb       # HMA (Hull) crossover backtest + sweep
│   ├── backtest_dema_cross.ipynb      # DEMA (Double EMA) crossover backtest + sweep
│   ├── backtest_ama_cross.ipynb       # AMA (Kaufman Adaptive) crossover backtest + sweep
│   ├── backtest_vidya_cross.ipynb     # VIDYA crossover backtest + sweep
│   ├── backtest_ema_cross_atr.ipynb   # MA crossover + ATR bracket (all MA types)
│   ├── backtest_ema_cross_bracket.ipynb
│   ├── backtest_ema_cross_long_only.ipynb
│   ├── backtest_bb_meanrev.ipynb
│   ├── backtest_macd_rsi.ipynb
│   ├── backtest_donchian_breakout.ipynb
│   ├── compare_sweeps.ipynb       # Cross-instrument/timeframe comparison
│   ├── validate_strategy.ipynb    # Walk-forward, plateau, bootstrap
│   ├── review_live_run.ipynb      # Post-run analysis of live/paper trades
│   ├── verify_01_pipeline.ipynb   # Data pipeline verification
│   ├── verify_02_data.ipynb       # Catalog vs exchange spot-checks
│   ├── verify_03_signals.ipynb    # Indicator / signal verification
│   ├── verify_04_persistence.ipynb # DB persistence verification
│   ├── charts.py                  # Plotting helpers (plotly, matplotlib, TVLC reports)
│   └── utils.py                   # Shared notebook helpers (make_instrument_id, save_tearsheet,
│                                  #   save_notebook, save_notebook_html)
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
# Edit .env — fill in POSTGRES_PASSWORD, TELEGRAM_TOKEN, HL credentials

# Build trader image
docker compose build trader

# Run migrations (first time only)
docker compose run --rm trader alembic upgrade head

# Start everything — infra + trader container
docker compose up -d

# Tail trader logs
docker compose logs -f trader --tail 200

# Monitoring
open http://localhost:3000   # Grafana (admin / your GRAFANA_PASSWORD)
```

To run the trader natively instead (quick iteration / debugging):

```bash
docker compose up -d postgres redis grafana
alembic upgrade head
python scripts/run_sandbox.py
```

### Phase 3a (current) — Research + validation

```bash
# Already set up from Phase 1. Just open notebooks:
jupyter notebook notebooks/

# Workflow:
# 1. backtest_*.ipynb → run_sweep() → data/sweeps/*.parquet
# 2. compare_sweeps.ipynb → load_sweeps() → side-by-side analysis
# 3. validate_strategy.ipynb → walk-forward + plateau + bootstrap
```

## Usage

### Develop and validate strategies (Phase 3a)

This is the current focus. The research workflow:

1. **Write a strategy** in `src/strategies/`. Subclass `Strategy`, implement `on_start()` and `on_bar()`.
2. **Sweep parameters** in a `backtest_*.ipynb` notebook using `run_sweep()`. Results auto-save to `data/sweeps/`.
3. **Compare across instruments and timeframes.** Open `compare_sweeps.ipynb`, call `load_sweeps()`. Review side-by-side heatmaps and parameter stability.
4. **Validate before paper trading.** Open `validate_strategy.ipynb`. Run plateau detection (are best params robust?), walk-forward analysis (do they work out-of-sample?), and bootstrap confidence intervals (is the result statistically reliable?).
5. **Paper trade validated strategies** via Phase 2 infrastructure.

### Run paper trading (Phase 2)

Requires infrastructure running first (`docker compose up -d` + migrations).

**Docker (recommended for multi-day runs):**

```bash
docker compose up -d          # starts infra + trader container; auto-restarts on crash
docker compose logs -f trader  # tail logs
docker compose stop trader     # graceful shutdown (SIGTERM → node.stop() → DB updated)
```

**Native (quick iteration):**

```bash
docker compose up -d postgres redis grafana
python scripts/run_sandbox.py  # Ctrl+C for graceful shutdown
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
| 2 | TradingNode deployment, PersistenceActor, AlertActor, paper + live trading | ✅ Complete |
| 3a | Research tooling — sweep persistence, cross-sweep comparison, walk-forward validation, bootstrap CI | 🟡 Active |
| 3b | Web layer — FastAPI gateway, React frontend, StreamingActor, Redis Streams | ⬜ Future |
| 4 | ML integration (feature engineering, model training, inference in callbacks) | ⬜ Planned |
| 5 | Experimental (LSTM, LLM sentiment, RL agents) | ⬜ Planned |

## Key Constraints

- **NautilusTrader is pre-v2.0** — pin the version, expect API breakage between releases.
- **No floats for prices** — NT uses 128-bit fixed-point. Maintain this in PostgreSQL (`NUMERIC`), asyncpg inserts (`str(nt_type)`), API responses (string-encoded decimals), and frontend.
- **Actor callbacks must never block** — use `self.run_in_executor()` for all I/O. Blocking the event loop stalls the TradingNode.
- **TradingNode is not Jupyter-compatible** — asyncio event loop conflicts. Run from scripts, not notebooks.
- **The "NT + web dashboard" pattern has no community precedent.** When stuck, read NT source code — docs and community posts won't cover integration patterns.
- **LGPL-3.0 license** — NT can be used as a library without affecting your project's license, but modifications to NT's own source must be shared.
