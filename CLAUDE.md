# CLAUDE.md

## What This Project Is

A crypto algorithmic trading platform. NautilusTrader (NT) is the core engine, installed as a pip dependency. We build everything around it: custom Actors for persistence and alerting, a FastAPI gateway (Phase 3), React frontend (Phase 3), PostgreSQL persistence, Redis event bridging (Phase 3).

This is a solo-developer hobby project with production-grade ambitions. The developer is a senior software engineer who is technically strong, opinionated about architecture, and hates fighting frameworks.

## Critical Rules — Never Violate These

### No Floats for Prices, Quantities, or Money
NT uses 128-bit fixed-point integers internally (`Price`, `Quantity`, `Money` types). Every layer must maintain this:
- **PostgreSQL:** `NUMERIC` type or integer (satoshis/smallest unit). Never `FLOAT`, `DOUBLE PRECISION`, or `REAL`.
- **API responses:** String-encoded decimals (`"0.00123456"`) or integer smallest-unit. Never JSON floats.
- **Python code:** Use `Decimal` from stdlib or NT's native types. Never `float` for financial values.
- **Frontend:** String or BigNumber library. Never JavaScript `Number` for prices.
- **asyncpg inserts:** Convert NT types to `str` before passing to asyncpg for NUMERIC columns. `str(event.last_px)` not `float(event.last_px)`.

### Event-Driven, Not Polling
NT is event-driven (Actor model, MessageBus pub/sub). All custom code must follow this pattern:
- Custom Actors subscribe to events on the MessageBus.
- No `while True: sleep(1)` loops for checking state.
- WebSocket push to frontend, not REST polling (Phase 3).

### Don't Fight NautilusTrader
NT is the framework. Work with its patterns:
- Subclass `Strategy` for trading logic. Use its callbacks: `on_start()`, `on_bar()`, `on_quote_tick()`, `on_order_filled()`, etc.
- Subclass `Actor` for non-trading components (persistence, alerting, streaming).
- Use `MessageBus` pub/sub for inter-component communication.
- Use NT's native types (`InstrumentId`, `BarType`, `Price`, `Quantity`, `OrderSide`, etc.) — don't reinvent.
- Use NT's `ParquetDataCatalog` for feeding backtests — don't build a custom data loader that bypasses it.
- Use NT's `BacktestEngine` (low-level) or `BacktestNode` (high-level) — don't write your own backtesting loop.
- Use NT's `RiskEngine` and trading states (`ACTIVE`, `REDUCING`, `HALTED`) — don't build a parallel risk system.

### Actor Callbacks Must Never Block
NT's MessageBus dispatches events synchronously on a single thread. Any blocking operation in an Actor callback (`on_order_filled`, timer callbacks) stalls the entire TradingNode.
- All I/O (database writes, HTTP requests) must use `self.run_in_executor(callable, args)` which runs the callable in a ThreadPoolExecutor.
- Inside executor callables, `asyncio.run()` is safe for wrapping async libraries like asyncpg.
- Never do blocking I/O directly in a callback method — always dispatch to the executor.
- Actor imports from `nautilus_trader.common.actor` (NOT `nautilus_trader.trading.actor`).

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
React Frontend ←WebSocket/REST→ FastAPI Gateway        ← Phase 3
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

Grafana ←SQL→ PostgreSQL                               ← Phase 2 monitoring
Telegram ←HTTP→ AlertActor (inside TradingNode)        ← Phase 2 alerting
```

### What NT Provides (don't rebuild)
- Event-driven backtesting with nanosecond resolution
- L1/L2/L3 order book simulation with configurable FillModel
- RiskEngine intercepting every order before execution
- HALTED trading state (kill switch — all orders denied except cancels)
- Exchange adapters for Binance (Spot/Futures), Bybit, Hyperliquid, Interactive Brokers, dYdX, Kraken, OKX, and others
- Sandbox execution adapter for paper trading against live data (SandboxExecutionClient)
- Redis cache integration (positions, orders, account state)
- Portfolio analyzer (Sharpe, Sortino, drawdown, profit factor, win rate) — Rust-ported
- Built-in indicators (EMA, SMA, MACD, Ichimoku, etc.)
- Execution reconciliation on startup (crash recovery)
- HTML tearsheet generation

### What We Build (NT doesn't provide)
- **PersistenceActor:** custom Actor inside TradingNode that subscribes to NT MessageBus events and writes fills, positions, and account snapshots to PostgreSQL.
- **AlertActor:** custom Actor inside TradingNode that sends Telegram notifications on fills, position changes, and drawdown threshold breaches.
- **Grafana dashboards:** ambient monitoring reading from PostgreSQL. Not locked in — data is in PostgreSQL and accessible to any tool.
- **FastAPI gateway (Phase 3):** REST endpoints for querying results, managing strategies. WebSocket endpoints for live trade streaming.
- **StreamingActor (Phase 3):** bridges NT MessageBus to Redis Streams for external consumers (FastAPI WebSocket handler). Deferred until Phase 3 because there is no external consumer until then.
- **React frontend (Phase 3):** TradingView Lightweight Charts with buy/sell overlays, strategy comparison tables, equity curves, P&L dashboards.
- **Data pipeline:** converts existing OHLCV data into NT's ParquetDataCatalog format.

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Trading engine | NautilusTrader | Pinned version, pip dependency |
| Persistence | asyncpg → PostgreSQL 16 + TimescaleDB | Actors write via asyncpg in executor threads |
| Migrations | Alembic | |
| Cache | Redis | NT native cache (positions, orders, account state) |
| Pub/sub bridge | Redis Streams via StreamingActor | Phase 3 — deferred |
| Alerting | Telegram Bot API via AlertActor | httpx (sync, in executor thread) |
| Monitoring | Grafana | Reads PostgreSQL; not locked in |
| Backtest data | NT ParquetDataCatalog | Parquet files on disk |
| API | FastAPI + asyncpg | Phase 3 |
| Frontend | React + TradingView Lightweight Charts | Phase 3 |
| Indicators | NT built-in + TA-Lib or pandas-ta | C core / Numba accelerated |
| Process mgmt | Docker Compose (infra) + venv (TradingNode, dev) | |
| Config | Pydantic Settings | Single settings.py, env var overrides, .env file |

## Code Conventions

- **Python 3.12+** with type hints everywhere.
- **Pydantic** for API schemas and settings.
- **asyncpg** for database access (not SQLAlchemy ORM — too much overhead for time-series queries). SQLAlchemy Core is acceptable for schema definition and migrations.
- **Decimal or NT native types** for all financial values in Python code.
- **NUMERIC** columns in PostgreSQL for all financial values.
- **str(nt_type) for asyncpg inserts** — `str(event.last_px)` produces `"65432.1"` which asyncpg handles as NUMERIC. Never float().
- **JSONB columns** for strategy parameters (schema flexibility).
- **Every table includes `strategy_id`** for multi-strategy support.
- **Every table includes `run_id`** (UUID) for grouping rows by TradingNode run.
- **ISO 8601 timestamps with timezone** everywhere. NT uses nanosecond-resolution Unix timestamps internally — convert with `datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)`.

## Project Structure

```
src/
├── strategies/       # NT Strategy subclasses (trading logic)
├── actors/           # NT Actor subclasses (persistence, alerting)
│   ├── persistence.py    # PersistenceActor — writes to PostgreSQL
│   └── alert.py          # AlertActor — Telegram notifications
├── api/              # FastAPI app, routes, WebSocket handlers (Phase 3)
├── persistence/      # DB schemas (SQLAlchemy Core), no migrations
├── backtesting/      # Backtest orchestration (wraps NT's BacktestEngine/Node)
├── config/           # Pydantic Settings model
│   └── settings.py       # get_settings() — single source of truth
└── core/             # TIGHT SCOPE: type aliases, constants, interface protocols, pure utils
alembic/              # Alembic migrations (deployment artifact, not runtime code)
alembic.ini           # Alembic config
grafana/
├── provisioning/     # Declarative datasource + dashboard provisioning (committed)
└── dashboards/       # Dashboard JSON files (committed)
frontend/             # React application (Phase 3)
scripts/
├── fetch_hl_candles.py   # Hyperliquid OHLCV data fetcher
├── run_sandbox.py        # Paper trading runner (SandboxExecutionClient)
└── run_live.py           # Live trading runner (HyperliquidExecClient)
notebooks/            # Jupyter prototyping + charts.py plotting helpers
data/                 # ParquetDataCatalog root (gitignored)
reports/              # Generated HTML backtest reports (gitignored)
tests/                # unit/ and integration/
```

## Development Phases

**Phase 1 (complete):** Strategy development + backtesting using NT's native workflow. Jupyter notebooks, BacktestEngine, in-memory results, matplotlib/plotly charts, HTML tearsheets. No custom infrastructure — learn NT's patterns first.

**Phase 2 (current):** Deploy TradingNode, write PersistenceActor and AlertActor, paper trade via NT's Sandbox adapter, validate all fills and positions write to PostgreSQL, monitor via Grafana and Telegram. No FastAPI, no frontend. Backend before UI. Build the UI against real live data in Phase 3.

**Phase 3:** StreamingActor (Redis Streams bridge) + FastAPI gateway + React frontend. Build the web product against real data from Phase 2 paper trading runs. WebSocket streaming from live TradingNode to browser.

**Phase 4:** ML integration — feature engineering, model training, inference in strategy callbacks.

**Phase 5:** Experimental — LSTM, LLM sentiment, RL agents.

## Common Tasks

### Phase 1 workflow (backtesting)
1. Load OHLCV data into NT's `ParquetDataCatalog` (one-time conversion script).
2. In a Jupyter notebook: configure `BacktestEngine` (venue, instrument, fees, fill model). Use `backtesting/engine.py` helpers (`make_engine()`, `run_single_backtest()`) to avoid boilerplate.
3. Write or tweak a `Strategy` subclass.
4. Run the backtest, inspect DataFrames (`generate_orders_report()`, `generate_positions_report()`).
5. For analyzer stats: `analyzer.calculate_statistics(account, positions)` where `account = engine.cache.account_for_venue(venue)` and `positions = engine.cache.position_snapshots() + engine.cache.positions()`.
6. Plot with `notebooks/charts.py`.
7. Generate HTML tearsheets with `generate_backtest_html()`.
8. Set `log_level` to `"ERROR"` in `LoggingConfig` to avoid stdout flooding.

### Phase 2 workflow (paper trading)
1. Start infrastructure: `docker compose up -d`
2. Run migrations: `alembic upgrade head`
3. Start paper trading: `python scripts/run_sandbox.py`
4. Verify Telegram alert fires on first fill.
5. Verify `order_fills` table has rows after first fill: `SELECT COUNT(*) FROM order_fills;`
6. Open Grafana at `http://localhost:3000` — check balance and fill panels.
7. Let it run. Check daily. After 2+ weeks with stable behavior, proceed to live.

### Adding a new strategy
1. Create a new file in `src/strategies/`.
2. Subclass `nautilus_trader.trading.strategy.Strategy`.
3. Implement `on_start()`, `on_bar()` (or `on_quote_tick()`), and order management callbacks.
4. Add it to the runner script config (`run_sandbox.py` or `run_live.py`).

### Running a backtest
**Phase 1/2:** Directly in Jupyter notebooks using `BacktestEngine`. Results are in-memory DataFrames and stats dicts.
**Phase 3+:** FastAPI endpoint. Results persisted to PostgreSQL.

### Adding a new API endpoint (Phase 3+)
1. Create or modify a router in `src/api/routes/`.
2. Use asyncpg for database queries.
3. Return Pydantic models with string-encoded decimals for financial values.

### Bridging NT events to the frontend (Phase 3)
The `StreamingActor` (in `src/actors/streaming.py`) subscribes to NT MessageBus events and publishes to Redis Streams. The FastAPI WebSocket handler reads from Redis Streams and pushes to connected clients. This is deliberately deferred to Phase 3.

## Gotchas and Warnings

- **NT logging in Jupyter:** NT exceeds Jupyter's stdout rate limits, causing notebooks to hang. Set `log_level` to `"ERROR"` or `"WARNING"` in `LoggingConfig`.
- **TradingNode is not Jupyter-compatible.** Running a `TradingNode` inside a notebook causes asyncio event loop conflicts. The TradingNode is a long-running blocking process — run it from `scripts/run_sandbox.py` or `scripts/run_live.py` in a terminal, not a notebook.
- **NT community is small (~5K Discord).** The "NT + web dashboard" pattern is unprecedented. When stuck, read NT source code directly — don't expect blog posts or SO answers.
- **NT doesn't use CCXT.** It has its own exchange adapters. If a target exchange isn't supported, you'd need to write a custom adapter.
- **Backtest results are in-memory only.** NT's `engine.trader.generate_orders_report()` etc. return pandas DataFrames. The persistence layer captures these for Phase 3 API queries.
- **Expect 30-40% performance haircut** from backtest to live, and paper to live. If paper lags backtest by >30-40%, investigate before going live.
- **Slippage modeling matters.** Configure NT's `FillModel`: 0.05-0.1% for top-10 coins, 0.5-2% outside top 100, 5-10% for microcaps.
- **NETTING mode position stats:** `cache.positions()` returns only the current Position object per instrument-strategy pair — NOT all historical positions. Closed positions are stored as snapshots. For correct analyzer stats, use `cache.position_snapshots() + cache.positions()`.
- **`analyzer.returns()` requires `calculate_statistics()` first.** Call `calculate_statistics()` immediately after `engine.run()`, before any plotting or stats access.
- **Actor callbacks must never block.** Use `self.run_in_executor(callable, args)` for all I/O. Blocking directly in `on_order_filled` or similar callbacks stalls the TradingNode.
- **asyncpg in Actor executors:** Use `asyncio.run(asyncpg.connect(...))` inside `run_in_executor` callables. A fresh connection per write is acceptable at hourly-bar frequency. No connection pool needed in Phase 2.
- **Actor has no `create_task`.** NT 1.223.0 Actor uses `run_in_executor` (ThreadPoolExecutor) for I/O, not `create_task`. Strategy has different APIs than Actor.
- **Position events in Actors:** Override `on_event(self, event)` and check `isinstance(event, PositionClosed)`. Actor has `on_order_filled` but no `on_position_closed` callback.
- **NT financial types to PostgreSQL NUMERIC:** Always `str(event.last_px)`, never `float(event.last_px)`. asyncpg accepts strings for NUMERIC columns and preserves precision.
- **HL_TESTNET defaults to True.** `run_live.py` requires explicitly setting `HL_TESTNET=false` in `.env` to trade on mainnet. Intentional friction.
- **HyperliquidDataClientConfig needs NO credentials.** Just `testnet=False` for real market data. Credentials are only needed on the exec client.
- **HyperliquidExecClientConfig uses `private_key` + `vault_address`** (NOT `wallet_address`). Verified in NT 1.223.0.
- **Adapter factories must be registered.** Call `node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)` and `node.add_exec_client_factory(...)` before `node.build()`.

## Communicating with This Developer

- Be direct and opinionated when evidence supports it.
- Flag tradeoffs honestly — don't soft-pedal downsides.
- Use concrete numbers, versions, and specific technical details.
- Skip beginner explanations unless asked.
- If a recommendation would mean "fighting the framework," say so upfront.
