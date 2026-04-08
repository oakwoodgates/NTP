# CLAUDE.md

## What This Project Is

A crypto algorithmic trading platform. NautilusTrader (NT) is the core engine, installed as a pip dependency. We build everything around it: custom Actors for persistence and alerting, research tooling for strategy validation, PostgreSQL persistence, and (future) a FastAPI gateway + React frontend.

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
- WebSocket push to frontend, not REST polling (future web layer).

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
Jupyter Notebooks (research + validation)          ← Phase 1 + 3a
  └── backtesting/engine.py
        ├── run_sweep()        → data/sweeps/*.parquet
        ├── run_walk_forward() → in-memory DataFrame
        └── load_sweeps()      ← reads from data/sweeps/
  └── notebooks/charts.py     → reports/charts/*.html (TVLC interactive)

                              PostgreSQL+TimescaleDB (persistence)
                              Redis (cache)
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

React Frontend ←WebSocket/REST→ FastAPI Gateway        ← Phase 3b (future)
  └── StreamingActor → Redis Streams → WebSocket
```

### What NT Provides (don't rebuild)
- Event-driven backtesting with nanosecond resolution
- L1/L2/L3 order book simulation with configurable FillModel
- RiskEngine intercepting every order before execution
- HALTED trading state (kill switch — all orders denied except cancels)
- Exchange adapters for Binance (Spot/Futures), Bybit, Hyperliquid, Interactive Brokers, dYdX, Kraken, OKX, and others
- Sandbox execution adapter for paper trading against live data (SandboxExecutionClient)
- Redis cache integration (positions, orders, account state)
- Portfolio analyzer (drawdown, profit factor, win rate) — Rust-ported. Sharpe/Sortino returns are unreliable (see `docs/ANALYZER_RETURNS_CAVEAT.md`).
- Built-in indicators (EMA, SMA, MACD, Ichimoku, etc.)
- Execution reconciliation on startup (crash recovery)
- HTML tearsheet generation

### What We Build (NT doesn't provide)
- **Sweep orchestration:** `run_sweep()` runs a parameter grid across any strategy, persists full analyzer stats to Parquet. `load_sweeps()` reads them back for comparison.
- **Walk-forward analysis:** `run_walk_forward()` trains on sliding windows, tests best params out-of-sample. Catches overfitting before paper trading.
- **Validation notebook:** plateau detection (are best params robust or fragile?), bootstrap confidence intervals (how much depends on a few lucky trades?), go/no-go assessment.
- **Post-backtest analysis:** `rolling_performance()` checks PnL consistency across time windows, `tag_regimes()` + `performance_by_regime()` quantifies strategy behavior in trending vs ranging markets, `run_fee_sweep()` measures fee resilience and breakeven points.
- **Comparison notebook:** cross-instrument, cross-timeframe sweep comparison. Parameter stability analysis across sweeps.
- **Interactive HTML reports:** TradingView Lightweight Charts with buy/sell markers, hover tooltips, trade table with click-to-zoom, stats bar. Self-contained HTML files in `reports/`.
- **PersistenceActor:** custom Actor inside TradingNode that subscribes to NT MessageBus events and writes fills, positions, and account snapshots to PostgreSQL.
- **AlertActor:** custom Actor inside TradingNode that sends Telegram notifications on fills, position changes, and drawdown threshold breaches.
- **Grafana dashboards:** ambient monitoring reading from PostgreSQL. Not locked in — data is in PostgreSQL and accessible to any tool.
- **Data pipeline:** fetches OHLCV candles from Hyperliquid and Binance, converts to NT's ParquetDataCatalog format. Shared utilities in `scripts/_catalog.py`.
- **FastAPI gateway (Phase 3b, future):** REST endpoints for querying results, managing strategies. WebSocket endpoints for live trade streaming.
- **StreamingActor (Phase 3b, future):** bridges NT MessageBus to Redis Streams for external consumers.
- **React frontend (Phase 3b, future):** TradingView Lightweight Charts with buy/sell overlays, strategy comparison tables, equity curves, P&L dashboards.

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Trading engine | NautilusTrader 1.225.0 | Pinned version, pip dependency |
| Persistence | asyncpg → PostgreSQL 16 + TimescaleDB | Actors write via asyncpg in executor threads |
| Migrations | Alembic | |
| Cache | Redis | NT native cache (positions, orders, account state) |
| Sweep results | Parquet files in `data/sweeps/` | One file per strategy × instrument × interval |
| Alerting | Telegram Bot API via AlertActor | httpx (sync, in executor thread) |
| Monitoring | Grafana | Reads PostgreSQL; not locked in |
| Backtest data | NT ParquetDataCatalog | Parquet files on disk |
| Charting | Plotly (notebooks) + TradingView Lightweight Charts (HTML reports) | charts.py in notebooks/ |
| API | FastAPI + asyncpg | Phase 3b — deferred |
| Frontend | React + TradingView Lightweight Charts | Phase 3b — deferred |
| Pub/sub bridge | Redis Streams via StreamingActor | Phase 3b — deferred |
| Indicators | NT built-in + TA-Lib or pandas-ta | C core / Numba accelerated |
| Process mgmt | Docker Compose (infra + trader container) + venv (dev/debug) | Trader runs as `trader` service; native venv for quick iteration |
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

### Static analysis and typing

- **Authoritative tools:** `ruff` (lint, imports, style), `mypy` (type checking), `pytest` (tests). CI runs `ruff check src tests scripts alembic` and `mypy src tests scripts`; both must pass before merge.
- **Untyped dependencies:** NautilusTrader, pandas, plotly, and asyncpg have no stubs. They are handled via `[[tool.mypy.overrides]]` in `pyproject.toml` with `ignore_missing_imports = true`, so mypy does not complain about missing stubs. Strict mode remains enabled for first-party code in `src/`, `tests/`, and `scripts/`.
- **New code expectations:** Full type hints on public APIs and callbacks; no `Any` in signatures except where required by NT (e.g. `on_event(event: Any)`). No floats for prices, quantities, or money. Run `ruff check` and `mypy` locally and fix any new violations before opening a PR. On NT version bumps, re-run both; if new NT modules trigger `import-not-found`, add them to the mypy overrides.
- **Notebooks:** Excluded from ruff/mypy in config; not part of CI static checks.

### Notebook conventions

- **Category-prefix naming, not sequential numbers.** `backtest_ema_cross.ipynb`, not `02_backtest_ema_cross.ipynb`. Prefixes group by purpose: `backtest_*`, `compare_*`, `validate_*`, `verify_*`.
- **Sweep results auto-persist to Parquet.** Use `run_sweep()` instead of manual `run_single_backtest` loops. Results land in `data/sweeps/` with deterministic filenames.
- **Strategy factory pattern.** Each backtest notebook defines a `strategy_factory(engine, params)` callable that `run_sweep` and `run_walk_forward` use. This keeps sweep/validation code strategy-agnostic.
- **Shared config in Cell 1.** All tuneable values live in Cell 1: `EXCHANGE`, `ASSET`, `INSTRUMENT_ID` (via `make_instrument_id(ASSET, EXCHANGE)`), `BAR_INTERVAL`, `SAVE_TEARSHEET`, `RESULT_NAME`, etc. `RESULT_NAME` is the canonical filename stem used by tearsheet save, TradingView HTML export, and notebook snapshot save.
- **`notebooks/utils.py` helpers.** `make_instrument_id(asset, exchange)` builds the correct instrument ID format per exchange (Hyperliquid vs Binance). `save_tearsheet(html, result_name)` saves tearsheet HTML to `reports/tearsheets/`. `save_notebook` and `save_notebook_html` copy/export notebooks to `reports/notebooks/{category}/` and `reports/html/{category}/` respectively (default `category="backtest"`).

## Project Structure

```
src/
├── strategies/       # NT Strategy subclasses (trading logic)
├── actors/           # NT Actor subclasses (persistence, alerting)
│   ├── persistence.py    # PersistenceActor — writes to PostgreSQL
│   └── alert.py          # AlertActor — Telegram notifications
├── api/              # FastAPI app, routes, WebSocket handlers (Phase 3b)
├── persistence/      # DB schemas (SQLAlchemy Core), no migrations
│   └── schema.py
├── backtesting/      # Backtest orchestration (wraps NT's BacktestEngine/Node)
│   ├── engine.py         # make_engine, run_single_backtest, run_sweep,
│   │                     # load_sweeps, run_walk_forward
│   └── analysis.py       # rolling_performance, tag_regimes,
│                         # performance_by_regime, run_fee_sweep
├── config/           # Pydantic Settings model
│   └── settings.py       # get_settings() — single source of truth
└── core/             # TIGHT SCOPE: type aliases, constants, interface protocols, pure utils
│   ├── constants.py
│   ├── instruments.py
│   └── utils.py
alembic/              # Alembic migrations (deployment artifact, not runtime code)
alembic.ini           # Alembic config
grafana/
├── provisioning/     # Declarative datasource + dashboard provisioning (committed)
└── dashboards/       # Dashboard JSON files (committed)
frontend/             # React application (Phase 3b)
scripts/
├── _catalog.py           # Shared data-fetch utilities (retry, validation, catalog write)
├── fetch_hl_candles.py   # Hyperliquid OHLCV data fetcher
├── fetch_binance_candles.py # Binance OHLCV data fetcher (Futures + Spot via --market)
├── run_sandbox.py        # Paper trading runner (SandboxExecutionClient)
└── run_live.py           # Live trading runner (HyperliquidExecClient)
notebooks/            # Jupyter research + charts.py plotting helpers
├── backtest_*.ipynb      # Per-strategy backtest + sweep notebooks
├── compare_sweeps.ipynb       # Cross-instrument, cross-timeframe comparison
├── validate_strategy.ipynb    # Walk-forward, plateau, bootstrap validation
├── review_live_run.ipynb      # Post-run analysis of live/paper trades
├── verify_01_pipeline.ipynb   # Data pipeline verification
├── verify_02_data.ipynb       # Catalog vs exchange spot-checks
├── verify_03_signals.ipynb    # Indicator / signal verification
├── verify_04_persistence.ipynb # DB persistence verification
├── charts.py                  # Plotly, matplotlib, TVLC HTML report generation
└── utils.py                   # Shared notebook helpers (make_instrument_id, save_tearsheet,
│                              #   save_notebook, save_notebook_html)
data/
├── catalog/          # ParquetDataCatalog root (gitignored)
└── sweeps/           # Sweep result Parquet files (gitignored)
reports/              # Generated reports (gitignored)
├── charts/           # TradingView Lightweight Charts HTML reports
├── html/
│   ├── backtest/     # Notebook HTML exports from backtest_*.ipynb
│   └── validate/     # Notebook HTML exports from validate_strategy.ipynb
├── notebooks/
│   ├── backtest/     # Notebook snapshots from backtest_*.ipynb
│   └── validate/     # Notebook snapshots from validate_strategy.ipynb
└── tearsheets/       # NT tearsheet HTML (saved when SAVE_TEARSHEET=True)
tests/                # unit/ and integration/
```

## Development Phases

**Phase 1 (complete):** Strategy development + backtesting using NT's native workflow. Jupyter notebooks, BacktestEngine, in-memory results, matplotlib/plotly charts, HTML tearsheets. No custom infrastructure — learn NT's patterns first.

**Phase 2 (complete):** Deploy TradingNode, write PersistenceActor and AlertActor, paper trade via NT's Sandbox adapter, validate all fills and positions write to PostgreSQL, monitor via Grafana and Telegram. Containerized trader on Digital Ocean with auto-restart and graceful shutdown.

**Phase 3a (current) — Research tooling + strategy validation:**
- `run_sweep()` — parameter grid search with automatic Parquet persistence to `data/sweeps/`.
- `run_walk_forward()` — sliding-window walk-forward analysis (train on N%, test on M%, slide).
- `rolling_performance()` — per-window PnL consistency analysis.
- `tag_regimes()` + `performance_by_regime()` — market regime detection (ADX-based) and per-regime stats.
- `run_fee_sweep()` — fee sensitivity analysis with breakeven detection.
- `compare_sweeps.ipynb` — load saved sweeps, side-by-side heatmaps, best-params table, parameter stability across instruments/timeframes.
- `validate_strategy.ipynb` — plateau detection, walk-forward, bootstrap confidence intervals, rolling performance, fee sensitivity, regime analysis, go/no-go assessment.
- Focus: validate strategies before committing them to paper/live trading. Build more strategies, test on more instruments.

**Phase 3a — future research tools (build when pain emerges):**
- Equity curve overlay — do strategies draw down together or diversify?
- Randomized entry baseline — does the strategy beat random?
- Strategy return correlation matrix — portfolio-level diversification analysis.

**Phase 3b (future) — Web layer (build when multiple validated strategies are running live and Grafana/SQL isn't enough):**
- FastAPI read-only API — query runs, fills, positions, equity curves from PostgreSQL.
- `bars` hypertable + PersistenceActor bar writing — candle data in PostgreSQL for chart overlay.
- React dashboard — TradingView Lightweight Charts with trade markers (the one view Grafana can't do).
- StreamingActor + Redis Streams + WebSocket — real-time trade streaming to browser.
- Write endpoints + auth — remote control (halt/resume trading), JWT auth.

**Phase 4:** ML integration — feature engineering from sweep results and bar data, model training, inference in strategy callbacks.

**Phase 5:** Experimental — LSTM, LLM sentiment, RL agents.

## Common Tasks

### Data management (fetch, backfill, update)
Both fetch scripts (`fetch_binance_candles.py`, `fetch_hl_candles.py`) support four mutually exclusive modes:
- **Default (`--days 180`):** Fetch last N days. Merges with existing data if present.
- **`--backfill`:** Extend history backwards to exchange's earliest available. Requires existing data — run `--days` first.
- **`--update`:** Extend data from last bar to now. Skips if already up to date.
- **`--start YYYY-MM-DD`:** Fetch from explicit date to now.

All modes merge with existing catalog data (dedup on timestamp, fresh exchange data wins). See `docs/DATA_FETCHING.md` for full details.

```bash
# Seed initial data (perp, default)
python scripts/fetch_binance_candles.py --coins BTC ETH SOL --intervals 1h 4h 1d

# Seed spot data
python scripts/fetch_binance_candles.py --market spot --coins BTC ETH SOL --intervals 1h 4h 1d

# Backfill to exchange's earliest (Binance has ~9 years for BTC)
python scripts/fetch_binance_candles.py --backfill --coins BTC --intervals 1h 4h 1d

# Periodic update to now
python scripts/fetch_binance_candles.py --update
```

### Phase 1 workflow (backtesting)
1. Fetch OHLCV data with `scripts/fetch_hl_candles.py` or `scripts/fetch_binance_candles.py`. Data writes to NT's `ParquetDataCatalog` at `data/catalog/`. Use `--backfill` to extend history, `--update` to bring data to present.
2. In a Jupyter notebook: configure `BacktestEngine` (venue, instrument, fees, fill model). Use `backtesting/engine.py` helpers (`make_engine()`, `run_single_backtest()`) to avoid boilerplate.
3. Write or tweak a `Strategy` subclass.
4. Run the backtest, inspect DataFrames (`generate_orders_report()`, `generate_positions_report()`).
5. For analyzer stats: `analyzer.calculate_statistics(account, positions)` where `account = engine.cache.account_for_venue(venue)` and `positions = engine.cache.position_snapshots() + engine.cache.positions()`.
6. Plot with `notebooks/charts.py`.
7. Generate HTML tearsheets with `generate_backtest_html()`.
8. Set `log_level` to `"ERROR"` in `LoggingConfig` to avoid stdout flooding.

### Phase 2 workflow (paper trading)
1. Build trader image: `docker compose build trader`
2. Run migrations: `docker compose run --rm trader alembic upgrade head`
3. Start everything: `docker compose up -d` (infra + trader container; auto-restarts on crash)
4. Tail logs: `docker compose logs -f trader --tail 200`
5. Verify Telegram alert fires on first fill.
6. Verify `order_fills` table has rows after first fill: `SELECT COUNT(*) FROM order_fills;`
7. Open Grafana at `http://localhost:3000` — check balance and fill panels.
8. Let it run. Check daily. After 2+ weeks with stable behavior, proceed to live.

**Graceful shutdown:** `docker compose stop trader` sends SIGTERM → Python SIGTERM handler raises `KeyboardInterrupt` → `finally` block calls `node.stop()`, `node.dispose()`, `_close_run()`. Verify `strategy_runs.stopped_at` is not NULL after stop.

**For quick iteration/debugging:** `docker compose up -d postgres redis grafana` then `python scripts/run_sandbox.py` natively.

### Phase 3a workflow (research + validation)
1. Create or open a `backtest_*.ipynb` notebook for the strategy.
2. Define a `strategy_factory(engine, params)` function and a list of `param_combos`.
3. Call `run_sweep()` — results auto-save to `data/sweeps/{strategy}_{instrument}_{interval}.parquet`.
4. Inspect heatmaps in the backtest notebook. For multi-stage sweeps (e.g. MACD periods → RSI thresholds), use a different `strategy_name` for the sensitivity sweep.
5. Compare across instruments/timeframes: open `compare_sweeps.ipynb`, call `load_sweeps()` (optionally filtered by strategy/instrument/interval). Review the best-params table and parameter stability analysis.
6. Validate before paper trading: open `validate_strategy.ipynb`, point it at the sweep file and strategy factory. Run plateau detection, walk-forward, and bootstrap. Check the go/no-go assessment.
7. Only proceed to paper trading (Phase 2 workflow) if validation passes.

### Adding a new strategy
1. Create a new file in `src/strategies/`.
2. Subclass `nautilus_trader.trading.strategy.Strategy`.
3. Implement `on_start()`, `on_bar()` (or `on_quote_tick()`), and order management callbacks.
4. Create a `backtest_*.ipynb` notebook. Define a `strategy_factory` and `param_combos`.
5. Run sweep → validate → paper trade → live.

### Adding a new API endpoint (Phase 3b+)
1. Create or modify a router in `src/api/routes/`.
2. Use asyncpg for database queries.
3. Return Pydantic models with string-encoded decimals for financial values.

### Bridging NT events to the frontend (Phase 3b)
The `StreamingActor` (in `src/actors/streaming.py`) subscribes to NT MessageBus events and publishes to Redis Streams. The FastAPI WebSocket handler reads from Redis Streams and pushes to connected clients. Deferred until Phase 3b — there is no external consumer until then.

## Gotchas and Warnings

- **NT logging in Jupyter:** NT exceeds Jupyter's stdout rate limits, causing notebooks to hang. Set `log_level` to `"ERROR"` or `"WARNING"` in `LoggingConfig`.
- **TradingNode is not Jupyter-compatible.** Running a `TradingNode` inside a notebook causes asyncio event loop conflicts. The TradingNode is a long-running blocking process — run it from `scripts/run_sandbox.py` or `scripts/run_live.py` in a terminal, not a notebook.
- **NT community is small (~5K Discord).** The "NT + web dashboard" pattern is unprecedented. When stuck, read NT source code directly — don't expect blog posts or SO answers.
- **NT doesn't use CCXT.** It has its own exchange adapters. If a target exchange isn't supported, you'd need to write a custom adapter.
- **Backtest results are in-memory only** unless persisted. NT's `engine.trader.generate_orders_report()` etc. return pandas DataFrames. Use `run_sweep()` to persist sweep results to Parquet. Live/paper results persist to PostgreSQL via PersistenceActor.
- **Expect 30-40% performance haircut** from backtest to live, and paper to live. If paper lags backtest by >30-40%, investigate before going live.
- **Slippage modeling matters.** Configure NT's `FillModel`: 0.05-0.1% for top-10 coins, 0.5-2% outside top 100, 5-10% for microcaps.
- **NETTING mode position stats:** `cache.positions()` returns only the current Position object per instrument-strategy pair — NOT all historical positions. Closed positions are stored as snapshots. For correct analyzer stats, use `cache.position_snapshots() + cache.positions()`.
- **`analyzer.returns()` requires `calculate_statistics()` first.** Call `calculate_statistics()` immediately after `engine.run()`, before any plotting or stats access.
- **Analyzer returns stats are unreliable.** NT's `get_performance_stats_returns()` (Sharpe, Sortino, Volatility, returns-based Profit Factor) uses a flawed methodology in v1.225.0 — equity pct_change at event timestamps zero-padded to a daily calendar, which massively deflates Sharpe. PnL-section stats (Total PnL, Win Rate, Expectancy, PnL-based Profit Factor) are correct. See `docs/ANALYZER_RETURNS_CAVEAT.md`. Do not use Sharpe/Sortino for strategy selection or go/no-go decisions until NT fixes this upstream.
- **Actor callbacks must never block.** Use `self.run_in_executor(callable, args)` for all I/O. Blocking directly in `on_order_filled` or similar callbacks stalls the TradingNode.
- **asyncpg in Actor executors:** Use `asyncio.run(asyncpg.connect(...))` inside `run_in_executor` callables. A fresh connection per write is acceptable at hourly-bar frequency. No connection pool needed in Phase 2.
- **Actor has no `create_task`.** NT 1.225.0 Actor uses `run_in_executor` (ThreadPoolExecutor) for I/O, not `create_task`. Strategy has different APIs than Actor.
- **Position events in Actors:** Override `on_event(self, event)` and check `isinstance(event, PositionClosed)`. Actor has `on_order_filled` but no `on_position_closed` callback.
- **NT financial types to PostgreSQL NUMERIC:** Always `str(event.last_px)`, never `float(event.last_px)`. asyncpg accepts strings for NUMERIC columns and preserves precision.
- **HL_TESTNET defaults to True.** `run_live.py` requires explicitly setting `HL_TESTNET=false` in `.env` to trade on mainnet. Intentional friction.
- **HyperliquidDataClientConfig needs NO credentials.** Just `testnet=False` for real market data. Credentials are only needed on the exec client.
- **HyperliquidExecClientConfig uses `private_key` + `vault_address`** (NOT `wallet_address`). Verified in NT 1.225.0.
- **Adapter factories must be registered.** Call `node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)` and `node.add_exec_client_factory(...)` before `node.build()`.
- **Sweep filename is deterministic.** `run_sweep()` saves to `{strategy}_{instrument}_{interval}.parquet`. Re-running the same combo overwrites the previous file. The `_swept_at` metadata column inside the file records when it was generated.
- **Walk-forward is expensive.** `run_walk_forward()` runs the full param grid per fold. With 60 combos × 4 folds = 240 backtests. Budget 3-5 min for hourly bars, 15-20 min for 5m bars over a year.
- **Binance API geo-blocked in some regions.** Connect NordVPN (`nordvpn connect`) before running `fetch_binance_candles.py`, or use `--testnet` for development (no geo-block, perp only — spot has no testnet). The script detects connection failures and prints VPN instructions.
- **MIT/LIT orders never trigger in bar-only backtests.** NT's SimulatedExchange bid/ask are never initialized from OHLCV data, and synthetic TradeTicks from bar decomposition are not published to the message bus (so OrderEmulator can't trigger either). Use manual trigger checking in `on_bar` + MARKET orders instead. See `docs/BAR_BACKTESTING_GOTCHAS.md`.

## Communicating with This Developer

- Be direct and opinionated when evidence supports it.
- Flag tradeoffs honestly — don't soft-pedal downsides.
- Use concrete numbers, versions, and specific technical details.
- Skip beginner explanations unless asked.
- If a recommendation would mean "fighting the framework," say so upfront.
