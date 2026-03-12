# Nautilus Trading Platform

A crypto algorithmic trading platform built on [NautilusTrader](https://nautilustrader.io/), with custom Actors for persistence and alerting, Grafana for monitoring, and a FastAPI + React frontend planned for Phase 3.

## Architecture

**Modular monolith, event-driven.** NautilusTrader is the core engine (installed as a pip dependency). Everything else — Actors, persistence, alerting, API, frontend — is custom code that orchestrates NT.

```
┌─────────────────────────────────────────────────────┐  Phase 3
│                  React Frontend                      │
│   TradingView charts · Strategy rankings · P&L      │
└──────────────────────┬──────────────────────────────┘
                       │ REST + WebSocket
┌──────────────────────┴──────────────────────────────┐  Phase 3
│                  FastAPI Gateway                      │
│   Backtest triggers · Results API · Live streaming   │
└──────┬───────────────────────────────────┬──────────┘
       │                                   │
┌──────┴──────────┐              ┌─────────┴──────────┐
│   PostgreSQL    │              │      Redis          │
│  + TimescaleDB  │              │  Cache + Pub/Sub    │
│                 │              │                     │
│ Fills, positions│              │ Live state (NT)     │
│ Account history │              │ Event streaming     │
│ Strategy meta   │              │ (Phase 3 bridge)    │
└──────┬──────────┘              └─────────┬──────────┘
       │                                   │
┌──────┴───────────────────────────────────┴──────────┐
│              NautilusTrader Engine                    │
│                                                      │
│  Strategies · Actors · RiskEngine · ExecutionEngine  │
│  BacktestEngine · TradingNode · MessageBus           │
│  Exchange Adapters (Hyperliquid, Binance, Bybit...)  │
│  ParquetDataCatalog · FillModel · Portfolio          │
└─────────────────────────────────────────────────────┘
       │ writes                   │ alerts
┌──────┴──────────┐    ┌──────────┴──────────┐
│ PersistenceActor│    │    AlertActor        │  Phase 2
│ (inside node)   │    │   (inside node)      │
└─────────────────┘    └─────────────────────┘
                                 │ Telegram
┌──────────────────┐
│     Grafana      │  Phase 2 — reads PostgreSQL
│  Balance · PnL   │  Not locked in
│  Fills · Stats   │
└──────────────────┘
```

### Key Design Decisions

- **NT as a library, not a fork.** We subclass `Strategy`, `Actor`, configure engines, call `node.run()`. NT's repo is never modified.
- **Actors are the extension point.** `PersistenceActor` and `AlertActor` live inside the TradingNode process, subscribe to NT's MessageBus, and do the work (DB writes, Telegram) via `run_in_executor()` — I/O runs in a thread pool without blocking the event loop.
- **PostgreSQL + TimescaleDB** for all persistent data. Prices stored as `NUMERIC` — never floats.
- **Redis** for real-time layer. NT uses it natively for cache; we add a `StreamingActor` in Phase 3 for bridging trade events to the frontend.
- **NT's ParquetDataCatalog** for feeding historical data to the backtester. Coexists with TimescaleDB (Parquet for NT, TimescaleDB for API queries).
- **Grafana for ambient monitoring** in Phase 2. Reads PostgreSQL directly. Not locked in — replace with any tool at any time without touching the persistence layer.
- **Backend before UI.** Phase 2 delivers paper and live trading with full persistence and alerting. The custom React frontend is Phase 3, built against real data from Phase 2 runs instead of theoretical event shapes.
- **Event-driven everywhere.** NT's MessageBus is the backbone. Custom Actors bridge events to persistence and the frontend. No polling loops.

## Project Structure

```
├── src/
│   ├── strategies/          # NT Strategy subclasses
│   │   ├── ema_cross.py
│   │   ├── ...
│   ├── actors/              # Custom NT Actors
│   │   ├── persistence.py   # PersistenceActor — writes fills/positions to PostgreSQL
│   │   └── alert.py         # AlertActor — Telegram notifications
│   ├── backtesting/         # Backtest orchestration
│   │   └── engine.py        # make_engine() + run_single_backtest() helpers
│   ├── persistence/         # SQLAlchemy Core table definitions (no ORM)
│   │   └── schema.py
│   ├── config/              # Pydantic Settings
│   │   └── settings.py      # get_settings() — single source of truth
│   ├── api/                 # FastAPI application (Phase 3)
│   └── core/                # Type aliases, constants, instruments, pure utils
│       ├── constants.py
│       ├── instruments.py
│       └── utils.py
├── grafana/
│   ├── provisioning/        # Declarative datasource + dashboard config
│   └── dashboards/          # Dashboard JSON (committed)
├── notebooks/               # Jupyter exploration & prototyping
│   ├── 01_verify_pipeline.ipynb
│   ├── 02_backtest_ema_cross.ipynb
│   ├── ...
│   └── charts.py            # Plotting helpers (plotly, matplotlib, HTML reports)
├── scripts/
│   ├── fetch_hl_candles.py  # Hyperliquid OHLCV data fetcher
│   ├── run_sandbox.py       # Paper trading runner (SandboxExecutionClient)
│   └── run_live.py          # Live trading runner (HyperliquidExecClient)
├── data/                    # ParquetDataCatalog root (gitignored)
├── reports/                 # Generated HTML backtest reports (gitignored)
├── tests/
│   ├── unit/
│   │   └── test_core.py
│   └── integration/
├── alembic/                 # DB migrations
├── frontend/                # React application (Phase 3)
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
- Node.js 18+ (frontend — Phase 3)
- A Hyperliquid wallet private key (for live/paper trading)

## Setup

### Phase 1 (complete) — NT native workflow

```bash
git clone <repo-url>
cd NTP
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
jupyter notebook notebooks/
```

### Phase 2 (current) — Paper + live trading

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

### Phase 3+ — Full stack

```bash
# API server
uvicorn src.api.main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev
```

## Usage

### Explore strategies in Jupyter (Phase 1)

Open a notebook in `notebooks/`, load data into NT's ParquetDataCatalog, configure a BacktestEngine, and iterate on Strategy subclasses. Results are in-memory DataFrames — plot with matplotlib/plotly, generate HTML tearsheets.

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

### Start the API server (Phase 3)

```bash
uvicorn src.api.main:app --reload --port 8000
```

## Development Phases

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Strategy development + backtesting (NT native workflow, Jupyter) | ✅ Complete |
| 2 | TradingNode deployment, PersistenceActor, AlertActor, paper + live trading | 🟡 Active |
| 3 | StreamingActor + FastAPI gateway + React frontend | ⬜ Planned |
| 4 | ML integration (entry/exit timing) | ⬜ Planned |
| 5 | Experimental (LSTM, LLM, sentiment, RL) | ⬜ Planned |

## Key Constraints

- **NautilusTrader is pre-v2.0** — pin the version, expect API breakage between releases.
- **No floats for prices** — NT uses 128-bit fixed-point. Maintain this in PostgreSQL (`NUMERIC`), asyncpg inserts (`str(nt_type)`), API responses (string-encoded decimals), and frontend.
- **Actor callbacks must never block** — use `self.run_in_executor()` for all I/O. Blocking the event loop stalls the TradingNode.
- **TradingNode is not Jupyter-compatible** — asyncio event loop conflicts. Run from scripts, not notebooks.
- **The "NT + web dashboard" pattern has no community precedent.** When stuck, read NT source code — docs and community posts won't cover integration patterns.
- **LGPL-3.0 license** — NT can be used as a library without affecting your project's license, but modifications to NT's own source must be shared.
