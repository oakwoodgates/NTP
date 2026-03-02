# Nautilus Trading Platform

A crypto algorithmic trading platform built on [NautilusTrader](https://nautilustrader.io/), with a FastAPI gateway and React frontend for backtest analysis, live monitoring, and strategy management.

## Architecture

**Modular monolith, event-driven.** NautilusTrader is the core engine (installed as a pip dependency). Everything else — API, frontend, persistence, bridging — is custom code that orchestrates NT.

```
┌─────────────────────────────────────────────────────┐
│                  React Frontend                      │
│   TradingView charts · Strategy rankings · P&L      │
└──────────────────────┬──────────────────────────────┘
                       │ REST + WebSocket
┌──────────────────────┴──────────────────────────────┐
│                  FastAPI Gateway                      │
│   Backtest triggers · Results API · Live streaming   │
└──────┬───────────────────────────────────┬──────────┘
       │                                   │
┌──────┴──────────┐              ┌─────────┴──────────┐
│   PostgreSQL    │              │      Redis          │
│  + TimescaleDB  │              │  Cache + Pub/Sub    │
│                 │              │                     │
│ Backtest results│              │ Live state (NT)     │
│ Trade history   │              │ Event streaming     │
│ Strategy meta   │              │ Frontend bridge     │
│ OHLCV (query)   │              │                     │
└──────┬──────────┘              └─────────┬──────────┘
       │                                   │
┌──────┴───────────────────────────────────┴──────────┐
│              NautilusTrader Engine                    │
│                                                      │
│  Strategies · Actors · RiskEngine · ExecutionEngine  │
│  BacktestEngine · TradingNode · MessageBus           │
│  Exchange Adapters (Binance, Bybit, etc.)            │
│  ParquetDataCatalog · FillModel · Portfolio          │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **NT as a library, not a fork.** We subclass `Strategy`, `Actor`, configure engines, call `node.run()`. NT's repo is never modified.
- **PostgreSQL + TimescaleDB** for all persistent data. Prices stored as `NUMERIC` or integer (smallest unit) — never floats.
- **Redis** for real-time layer. NT uses it natively for cache; we add pub/sub for bridging trade events to the frontend.
- **NT's ParquetDataCatalog** for feeding historical data to the backtester. Coexists with TimescaleDB (Parquet for NT, TimescaleDB for API queries).
- **Event-driven everywhere.** NT's MessageBus is the backbone. Custom Actors bridge events to persistence and frontend. No polling loops.

## Project Structure

```
├── src/
│   ├── strategies/          # NT Strategy subclasses
│   │   ├── __init__.py
│   │   └── examples/        # Reference implementations
│   ├── actors/              # Custom NT Actors
│   │   ├── __init__.py
│   │   ├── persistence.py   # MessageBus → PostgreSQL writer
│   │   └── streaming.py     # MessageBus → Redis pub/sub bridge
│   ├── api/                 # FastAPI application (outermost layer)
│   │   ├── __init__.py
│   │   ├── main.py          # App entrypoint
│   │   ├── routes/          # REST endpoints
│   │   └── ws/              # WebSocket handlers
│   ├── persistence/         # Database layer (schemas + repositories)
│   │   ├── __init__.py
│   │   ├── models.py        # SQLAlchemy Core / raw SQL schemas
│   │   └── repositories.py  # Query interfaces
│   ├── backtesting/         # Backtest orchestration
│   │   ├── __init__.py
│   │   ├── runner.py        # BacktestEngine/BacktestNode wrappers
│   │   └── data_loader.py   # OHLCV → ParquetDataCatalog pipeline
│   ├── config/              # Configuration management
│   │   ├── __init__.py
│   │   └── settings.py      # Single Pydantic Settings model, env var overrides
│   └── core/                # TIGHT: type aliases, constants, protocols, pure utils
│       ├── __init__.py
│       └── types.py         # Price/quantity type discipline
├── alembic/                 # DB migrations (deployment artifact, not runtime code)
│   ├── env.py
│   └── versions/
├── alembic.ini
├── frontend/                # React application
│   ├── src/
│   └── package.json
├── scripts/                 # Operational scripts
│   ├── run_backtest.py      # CLI backtest runner
│   └── run_live.py          # TradingNode launcher
├── notebooks/               # Jupyter exploration & prototyping
├── data/                    # ParquetDataCatalog root (gitignored)
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── pyproject.toml
├── CLAUDE.md
├── docker-compose.yml       # PostgreSQL + TimescaleDB + Redis
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
- Node.js 18+ (frontend — Phase 2)
- Docker + Docker Compose (PostgreSQL + TimescaleDB, Redis — Phase 2)
- A Binance/Bybit API key (for live/paper trading — Phase 3)

## Setup

### Phase 1 (current) — NT native workflow

```bash
# Clone
git clone <repo-url>
cd nautilus-platform

# Python environment
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Launch Jupyter
jupyter notebook notebooks/
```

### Phase 2+ — Full stack

```bash
# Infrastructure
docker compose up -d  # PostgreSQL+TimescaleDB on 5432, Redis on 6379

# Database migrations
alembic upgrade head

# Frontend
cd frontend
npm install
npm run dev
```

## Usage

### Explore strategies in Jupyter (Phase 1)

Open a notebook in `notebooks/`, load data into NT's ParquetDataCatalog, configure a BacktestEngine, and iterate on Strategy subclasses. Results are in-memory DataFrames — plot with matplotlib/plotly, generate HTML tearsheets.

### Run a backtest via CLI (Phase 2+)

```bash
python scripts/run_backtest.py \
  --strategy ema_cross \
  --instrument BTCUSDT.BINANCE \
  --start 2024-01-01 \
  --end 2024-12-31
```

### Start the API server (Phase 2+)

```bash
uvicorn src.api.main:app --reload --port 8000
```

### Start a live/paper trading node (Phase 3)

```bash
python scripts/run_live.py --config configs/paper_btc.toml
```

## Development Phases

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Strategy development + backtesting (NT native workflow) | 🟡 Active |
| 2 | Frontend + API + persistence layer | ⬜ Planned |
| 3 | Paper trading + live trading | ⬜ Planned |
| 4 | ML integration (entry/exit timing) | ⬜ Planned |
| 5 | Experimental (LSTM, LLM, sentiment) | ⬜ Planned |

## Key Constraints

- **NautilusTrader is pre-v2.0** — pin the version, expect API breakage between releases.
- **No floats for prices** — NT uses 128-bit fixed-point. Maintain this in PostgreSQL (`NUMERIC`), API responses (string-encoded decimals), and frontend.
- **The "NT + web dashboard" pattern has no community precedent.** When stuck, read NT source code — docs and community posts won't cover integration patterns.
- **LGPL-3.0 license** — NT can be used as a library without affecting your project's license, but modifications to NT's own source must be shared.
