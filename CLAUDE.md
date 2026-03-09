# CLAUDE.md

## What This Project Is

A crypto algorithmic trading platform. NautilusTrader (NT) is the core engine, installed as a pip dependency. We build everything around it: FastAPI API, React frontend, PostgreSQL persistence, Redis event bridging.

This is a solo-developer hobby project with production-grade ambitions. The developer is a senior software engineer who is technically strong, opinionated about architecture, and hates fighting frameworks.

## Critical Rules — Never Violate These

### No Floats for Prices, Quantities, or Money
NT uses 128-bit fixed-point integers internally (`Price`, `Quantity`, `Money` types). Every layer must maintain this:
- **PostgreSQL:** `NUMERIC` type or integer (satoshis/smallest unit). Never `FLOAT`, `DOUBLE PRECISION`, or `REAL`.
- **API responses:** String-encoded decimals (`"0.00123456"`) or integer smallest-unit. Never JSON floats.
- **Python code:** Use `Decimal` from stdlib or NT's native types. Never `float` for financial values.
- **Frontend:** String or BigNumber library. Never JavaScript `Number` for prices.

### Event-Driven, Not Polling
NT is event-driven (Actor model, MessageBus pub/sub). All custom code must follow this pattern:
- Custom Actors subscribe to events on the MessageBus.
- No `while True: sleep(1)` loops for checking state.
- WebSocket push to frontend, not REST polling.

### Don't Fight NautilusTrader
NT is the framework. Work with its patterns:
- Subclass `Strategy` for trading logic. Use its callbacks: `on_start()`, `on_bar()`, `on_quote_tick()`, `on_order_filled()`, etc.
- Subclass `Actor` for non-trading components (persistence, streaming, monitoring).
- Use `MessageBus` pub/sub for inter-component communication.
- Use NT's native types (`InstrumentId`, `BarType`, `Price`, `Quantity`, `OrderSide`, etc.) — don't reinvent.
- Use NT's `ParquetDataCatalog` for feeding backtests — don't build a custom data loader that bypasses it.
- Use NT's `BacktestEngine` (low-level) or `BacktestNode` (high-level) — don't write your own backtesting loop.
- Use NT's `RiskEngine` and trading states (`ACTIVE`, `REDUCING`, `HALTED`) — don't build a parallel risk system.

### Pin NT Version
NT is pre-v2.0 with breaking changes between releases. The version is pinned in `pyproject.toml`. Never upgrade without testing in a branch first.

### Module Dependency Direction
Dependencies flow inward. Outer layers depend on inner layers, never the reverse.

```
core/                       ← depends on nothing internal (NT types, stdlib only)
  ↑
strategies/, actors/        ← depend on core/ (for types, interfaces, constants)
  ↑
backtesting/, persistence/  ← depend on core/ (peers, not interdependent)
  ↑
api/                        ← depends on persistence/, backtesting/, core/
```

Rules:
- `core/` imports nothing from other `src/` modules. Ever.
- `strategies/` and `actors/` import from `core/` only (plus NT and stdlib).
- `persistence/` imports from `core/` only — never from `strategies/`, `actors/`, or `api/`.
- `api/` is the outermost layer — it can import from anything.
- `backtesting/` imports from `core/` and may reference strategy classes for registration, but never imports from `api/` or `persistence/` directly (it receives a persistence interface from `core/`).
- If you're tempted to create a circular import, the abstraction belongs in `core/` as a Protocol.

### `core/` Scope — Keep It Tight
`core/` contains **only**:
- **Type aliases and newtypes** wrapping NT types or `Decimal` (e.g., `StrategyId`, price/quantity type helpers)
- **Constants** (exchange names, fee tiers, shared enums)
- **Interface protocols** (`typing.Protocol` ABCs that other modules implement — e.g., `BacktestResultWriter`, `TradeEventPublisher`)
- **Pure utility functions** that depend on nothing internal (e.g., timestamp conversion, decimal formatting)

`core/` does **not** contain: business logic, database code, API schemas, configuration, or anything that imports from another `src/` module. If it doesn't fit the list above, it belongs in a more specific module.

## Architecture Context

```
React Frontend ←WebSocket/REST→ FastAPI Gateway
                                      ↕
                              PostgreSQL+TimescaleDB (persistence)
                              Redis (cache + pub/sub)
                                      ↕
                              NautilusTrader Engine
                              (Strategies, Actors, RiskEngine,
                               BacktestEngine, TradingNode,
                               Exchange Adapters, MessageBus)
                                      ↕
                              ParquetDataCatalog (backtest data)
                              Exchange APIs (Hyperliquid, Binance, etc.)
```

### What NT Provides (don't rebuild)
- Event-driven backtesting with nanosecond resolution
- L1/L2/L3 order book simulation with configurable FillModel
- RiskEngine intercepting every order before execution
- HALTED trading state (kill switch — all orders denied except cancels)
- Exchange adapters for Binance (Spot/Futures), Bybit, Hyperliquid, Interactive Brokers, dYdX, Kraken, OKX, and others
- Redis cache integration (positions, orders, account state)
- Portfolio analyzer (Sharpe, Sortino, drawdown, profit factor, win rate) — Rust-ported
- Built-in indicators (EMA, SMA, MACD, Ichimoku, etc.)
- Execution reconciliation on startup (crash recovery)
- HTML tearsheet generation
- Sandbox execution adapter for paper trading against live data

### What We Build (NT doesn't provide)
- **FastAPI gateway:** REST endpoints for triggering backtests, querying results, managing strategies. WebSocket endpoints for live trade streaming.
- **React frontend:** TradingView Lightweight Charts with buy/sell overlays, strategy comparison tables, equity curves, P&L dashboards.
- **Persistence layer:** Write NT's in-memory backtest results (DataFrames, stats dicts) to PostgreSQL+TimescaleDB. Custom Actors to persist live trade events.
- **Redis pub/sub bridge:** Custom Actor subscribes to NT MessageBus events, publishes to Redis pub/sub. FastAPI reads Redis pub/sub, pushes to frontend via WebSocket.
- **Data pipeline:** Convert existing OHLCV data (from a separate ingestion platform) into NT's ParquetDataCatalog format.

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Trading engine | NautilusTrader | Pinned version, pip dependency |
| API | FastAPI + asyncpg | Async everywhere |
| Database | PostgreSQL 16 + TimescaleDB | Hypertables for time-series |
| Cache/Pub-sub | Redis | NT native cache + custom pub/sub |
| Backtest data | NT ParquetDataCatalog | Parquet files on disk |
| Frontend | React + TradingView Lightweight Charts | Vite build |
| Indicators | NT built-in + TA-Lib or pandas-ta | C core / Numba accelerated |
| Migrations | Alembic | |
| Process mgmt | systemd (production) | For TradingNode long-running process |

## Code Conventions

- **Python 3.12+** with type hints everywhere.
- **Pydantic** for API schemas and settings.
- **asyncpg** for database access (not SQLAlchemy ORM — too much overhead for time-series queries). SQLAlchemy Core is acceptable for schema definition and migrations.
- **Decimal or NT native types** for all financial values in Python code.
- **NUMERIC** columns in PostgreSQL for all financial values.
- **JSONB columns** for strategy parameters (schema flexibility).
- **Every table includes `strategy_id`** for multi-strategy support.
- **ISO 8601 timestamps with timezone** everywhere. NT uses nanosecond-resolution Unix timestamps internally.

## Project Structure

```
src/
├── strategies/       # NT Strategy subclasses (trading logic)
├── actors/           # NT Actor subclasses (persistence bridge, streaming bridge)
├── api/              # FastAPI app, routes, WebSocket handlers
├── persistence/      # DB schemas, repositories (no migrations — those are in alembic/)
├── backtesting/      # Backtest orchestration (wraps NT's BacktestEngine/Node)
├── config/           # Pydantic Settings model (single settings.py, env var overrides)
└── core/             # TIGHT SCOPE: type aliases, constants, interface protocols, pure utils
alembic/              # Alembic migrations (deployment artifact, not runtime code)
alembic.ini           # Alembic config
frontend/             # React application
scripts/              # CLI runners (run_backtest.py, run_live.py)
notebooks/            # Jupyter prototyping
data/                 # ParquetDataCatalog root (gitignored)
tests/                # unit/ and integration/
```

## Development Phases

**Phase 1 (current):** Strategy development + backtesting using NT's native workflow. Jupyter notebooks, BacktestEngine, in-memory results, matplotlib/plotly charts, HTML tearsheets. No custom infrastructure — learn NT's patterns first.
**Phase 2:** Frontend + API + persistence. FastAPI gateway, React frontend, PostgreSQL+TimescaleDB for backtest result storage.
**Phase 3:** Paper trading (NT Sandbox mode) + live trading (NT TradingNode). Redis event bridge to frontend.
**Phase 4:** ML integration — feature engineering, model training, inference in strategy callbacks.
**Phase 5:** Experimental — LSTM, LLM sentiment, RL agents.

## Common Tasks

### Phase 1 workflow (current)
1. Load OHLCV data into NT's `ParquetDataCatalog` (one-time conversion script).
2. In a Jupyter notebook: configure `BacktestEngine` (venue, instrument, fees, fill model).
3. Write or tweak a `Strategy` subclass.
4. Run the backtest, inspect DataFrames (`generate_orders_report()`, `generate_positions_report()`).
5. For analyzer stats: `analyzer.calculate_statistics(account, positions)` where `account = engine.cache.account_for_venue(venue)` and `positions = engine.cache.position_snapshots() + engine.cache.positions()`.
6. Plot equity curves with matplotlib/plotly.
7. Generate an HTML tearsheet for runs worth saving.
8. Set `log_level` to `"ERROR"` in `LoggingConfig` to avoid stdout flooding.

### Adding a new strategy
1. Create a new file in `src/strategies/`.
2. Subclass `nautilus_trader.trading.strategy.Strategy`.
3. Implement `on_start()`, `on_bar()` (or `on_quote_tick()`), and order management callbacks.
4. Register it in the backtest runner config or TradingNode config.

### Running a backtest
**Phase 1:** Directly in Jupyter notebooks using `BacktestEngine`. Results are in-memory DataFrames and stats dicts.
**Phase 2+:** Use `scripts/run_backtest.py` or the FastAPI endpoint. Results persisted to PostgreSQL.

### Adding a new API endpoint (Phase 2+)
1. Create or modify a router in `src/api/routes/`.
2. Use asyncpg for database queries.
3. Return Pydantic models with string-encoded decimals for financial values.

### Bridging NT events to the frontend (Phase 2+)
The `StreamingActor` (in `src/actors/streaming.py`) subscribes to NT MessageBus events and publishes to Redis pub/sub. The FastAPI WebSocket handler reads from Redis pub/sub and pushes to connected clients.

## Gotchas and Warnings

- **NT logging in Jupyter:** NT exceeds Jupyter's stdout rate limits, causing notebooks to hang. Set `log_level` to `"ERROR"` or `"WARNING"` in `LoggingConfig`.
- **NT community is small (~5K Discord).** The "NT + web dashboard" pattern is unprecedented. When stuck, read NT source code directly — don't expect blog posts or SO answers.
- **NT doesn't use CCXT.** It has its own exchange adapters. If a target exchange isn't supported, you'd need to write a custom adapter.
- **Backtest results are in-memory only.** NT's `engine.trader.generate_orders_report()` etc. return pandas DataFrames. The persistence layer must capture these.
- **Expect 30-40% performance haircut** from backtest to live. If paper lags backtest by >30-40%, investigate before going live.
- **Slippage modeling matters.** Configure NT's `FillModel`: 0.05-0.1% for top-10 coins, 0.5-2% outside top 100, 5-10% for microcaps.
- **NETTING mode position stats:** `cache.positions()` returns only the current Position object per instrument-strategy pair — NOT all historical positions. Closed positions are stored as snapshots. For correct analyzer stats, use `cache.position_snapshots() + cache.positions()` when calling `analyzer.calculate_statistics(account, positions)`. Without this, Win Rate, Long Ratio, Sharpe, and all position-level stats will be wrong.
- **`analyzer.returns()` requires `calculate_statistics()` first.** `returns()` is a getter for an internal Series that starts empty. It only gets populated when `calculate_statistics()` processes positions. Call `calculate_statistics()` immediately after `engine.run()`, before any plotting or stats access.

## Communicating with This Developer

- Be direct and opinionated when evidence supports it.
- Flag tradeoffs honestly — don't soft-pedal downsides.
- Use concrete numbers, versions, and specific technical details.
- Skip beginner explanations unless asked.
- If a recommendation would mean "fighting the framework," say so upfront.
