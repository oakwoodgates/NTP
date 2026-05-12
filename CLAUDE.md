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

### Findings and Decision Outputs Stay Local — Never Commit to Public Repo
This is a public GitHub repo. **Never commit research findings, picked configs, backtest result numbers, verdict files, or run summaries** — those constitute the project's edge and must not leak.

**Do NOT commit (these go to `reports/` which is `.gitignore`d):**
- Decision docs naming a picked strategy + params + measured PnL/PF/drawdown (e.g. `SANDBOX_CONFIG_DECISION.md`)
- Phase verdict docs (`PHASE_2_5_VERDICT.md`, `PHASE_2_6_VERDICT.md`, etc.) with measured numbers
- Per-(instrument, combo) validate verdict JSONs from `reports/validate/`
- Sweep parquets and HTML reports (already in gitignored `data/sweeps/` and `reports/sweeps/`)
- Anything that says "config X works on instrument Y at level Z" with attached numbers
- Live/paper-trade run summaries with PnL

**OK to commit (tooling, not findings):**
- Scripts that PRODUCE findings (e.g. `scripts/rank_sandbox_candidates.py`, `scripts/batch_backtest.py`)
- Schema migrations, helpers, infrastructure code
- General docs (ROADMAP, CONFIG, PAPER_TRADING_GUIDE, STRATEGY_ENTRY_RULES) that explain HOW the system works without naming a specific picked config
- Test code
- Default settings values can change (`MA_SLOW: 40 → 100`) without explaining the data-driven reason in a public commit message — the reason lives in `reports/decisions/`

**Where findings DO live:** `reports/decisions/<NAME>.md` (gitignored). Reference the path from PR descriptions when needed for review, but don't paste contents.

**If you're unsure, don't push. Ask the user.** This rule applies to every PR, every commit message, every code comment. Commit messages should reference WHAT changed (e.g. "data-driven MA_SLOW default") not WHAT was measured ("BTC PnL 74.7%/yr at fast=10/slow=100").

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
- Built-in indicators (EMA, SMA, HMA, DEMA, AMA/KAMA, VIDYA, MACD, Ichimoku, etc.) and `MovingAverageFactory` for generic MA construction
- Execution reconciliation on startup (crash recovery)
- HTML tearsheet generation

### What We Build (NT doesn't provide)
- **Sweep orchestration:** `run_sweep()` runs a parameter grid across any strategy, persists full analyzer stats to Parquet. `load_sweeps()` reads them back for comparison.
- **Walk-forward analysis:** `run_walk_forward()` trains on sliding windows, tests best params out-of-sample. Catches overfitting before paper trading.
- **Validation notebook:** 8-check go/no-go verdict per (instrument, combo) — plateau (with survival-rate accounting), walk-forward (OOS PnL + param-stability), bootstrap (PnL CI + max-drawdown CI, with capital-relative thresholds), rolling (active vs inactive windows), fee sensitivity, regime breakdown (Wilson CI on win-rate), yearly concentration. Override the auto-pick to validate cross-sweep robust combos. Persists each verdict as JSON for the consolidator.
- **Validate-all consolidator:** `validate_all.ipynb` reads every `reports/validate/*_verdict.json` and renders a strategy-level comparison matrix + per-check failure-rate. Answers "does this combo generalize across instruments?" without re-running validate.
- **Post-backtest analysis:** `rolling_performance()` checks PnL consistency across time windows, `tag_regimes()` + `performance_by_regime()` quantifies strategy behavior in trending vs ranging markets, `run_fee_sweep()` measures fee resilience and breakeven points, `bootstrap_total_pnl()` + `bootstrap_max_drawdown()` give per-trade resampling CIs.
- **Liquidation simulator:** `LiquidationAware` mixin places a reduce-only `StopMarketOrder` at the cross-margin liquidation price for every open position; `AccountAliveMonitor` actor halts the engine when account equity drops below the alive floor.  NT 1.226.0's `MARGIN` accounts don't enforce margin natively (verified — see `docs/LIQUIDATION_AND_SIZING.md`); this fills the gap.
- **Protective stop loss (`ProtectiveStopAware`):** strategy mixin (parallel to `LiquidationAware`) that places a fixed-percent reduce-only stop at `entry × (1 ± stop_pct)` for every open position.  Composes with the liq mixin — both stops are reduce-only at different prices; whichever fires first reduces the position.  Setting `stop_pct = 1/leverage` gives **isolated-margin equivalence under cross-margin accounting** — worst-case loss per trade equals the IM committed.  Used by `MACross`; opt-in for any other strategy by adding the mixin to the MRO.  See `notebooks/backtest/ma_cross_stop_loss.ipynb` for the sensitivity sweep notebook.
- **Comparison notebook:** cross-instrument, cross-timeframe sweep comparison. Parameter stability analysis across sweeps including coefficient-of-variation (CV = std/|mean| per combo across instruments — low = stable, high = sign-flipping).
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
| Trading engine | NautilusTrader 1.226.0 | Pinned version, pip dependency |
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

- **Subdirectory grouping.** Backtest notebooks live in `notebooks/backtest/`, verification notebooks in `notebooks/verify/`. Workflow notebooks (`compare_sweeps`, `validate_strategy`, `validate_all`, `review_live_run`) and shared helpers (`charts.py`, `utils.py`) stay in the `notebooks/` root. Subdirectory notebooks add `sys.path.insert(0, str(__import__("pathlib").Path("..").resolve()))` in Cell 1 so `charts` and `utils` imports resolve.
- **Notebook-private helpers go in `_<notebook>_helpers.py`.** Functions extracted purely to keep cells short — not reusable across notebooks — live in a leading-underscore module next to the notebook (`notebooks/_compare_helpers.py`, `notebooks/_validate_helpers.py`). Tested in `tests/unit/test_<notebook>_helpers.py`. Truly reusable helpers go in `notebooks/utils.py` or `notebooks/charts.py` (no prefix).
- **Sweep results auto-persist to Parquet.** Use `run_sweep()` instead of manual `run_single_backtest` loops. Results land in `data/sweeps/` with deterministic filenames.
- **Strategy factory pattern.** Each backtest notebook defines a `strategy_factory(engine, params)` callable that `run_sweep` and `run_walk_forward` use. This keeps sweep/validation code strategy-agnostic. Validate notebooks build the same callable via `_validate_helpers.make_strategy_factory(strategy, instrument_id, bar_type_str, trade_notional)` — uses the central `STRATEGIES` registry (also in `_validate_helpers.py`).
- **Shared config in Cell 1.** All tuneable values live in Cell 1: `STRATEGY`, `DATA_SOURCE`, `EXEC_VENUE`, `ASSET`, `INSTRUMENT_ID` (via `make_instrument_id(ASSET, DATA_SOURCE)`), `BAR_INTERVAL`, `OVERRIDE_PARAMS`, `RESULT_NAME`, etc. `RESULT_NAME` is the canonical filename stem; format is `{prefix}_{strategy}_{ASSET}_{EXEC_VENUE}_{interval}[_{params_tag}]` shared across backtest and validate.
- **`notebooks/utils.py` helpers.** `make_instrument_id`, `load_backtest_data`, `load_sweeps_filtered`, `print_validation_verdict` (with capital-relative bootstrap threshold + JSON persistence), `wilson_score_interval`, `load_verdict_jsons` + `build_verdict_matrix` (consumed by `validate_all.ipynb`), `save_tearsheet`, `save_notebook` / `save_notebook_html` / `save_notebook_snapshot` (default `category="backtest"`).
- **Suppress Jupyter cell auto-display with a trailing `;`.** `print_validation_verdict(...)` and `save_notebook_snapshot(...)` return values useful to programmatic consumers but redundant in cell output. End the call with `;` to suppress auto-echo.

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
├── backtest/                # Per-strategy backtest + sweep notebooks
│   ├── ma_cross.ipynb       # All 6 base MA types (EMA/SMA/HMA/DEMA/AMA/VIDYA)
│   │                        # — pick via MA_TYPE in cell 1.1
│   ├── ma_cross_atr.ipynb, ma_cross_bracket.ipynb, ma_cross_long_only.ipynb,
│   │   ma_cross_stop_entry.ipynb, ma_cross_take_profit.ipynb, ma_cross_trailing_stop.ipynb
│   │                        # specialised variants — same MA_TYPE selector pattern
│   ├── ma_cross_stop_loss.ipynb  # MACross + protective stop-loss sensitivity sweep
│   │                              # — defaults STOP_PCT=0.05 (isolated-margin
│   │                              #   equivalent at 20× leverage)
│   ├── bb_meanrev.ipynb, macd_rsi.ipynb, donchian_breakout.ipynb
│   └── ...
├── verify/                  # Data pipeline + signal verification
│   ├── 01_pipeline.ipynb, 02_data.ipynb, 03_signals.ipynb, 04_persistence.ipynb
├── compare_sweeps.ipynb     # Cross-instrument, cross-timeframe comparison
├── validate_strategy.ipynb  # 8-check go/no-go verdict per (instrument, combo)
├── validate_all.ipynb       # Strategy-level matrix consolidator (reads
│                            #   reports/validate/*_verdict.json)
├── review_live_run.ipynb    # Post-run analysis of live/paper trades
├── charts.py                # Plotly, matplotlib, TVLC HTML report generation
├── utils.py                 # Shared notebook helpers (make_instrument_id,
│                            #   load_sweeps_filtered, print_validation_verdict,
│                            #   load_verdict_jsons, build_verdict_matrix,
│                            #   wilson_score_interval, save_notebook_snapshot)
├── _compare_helpers.py      # Notebook-private helpers for compare_sweeps
│                            #   (build_stability_df, short_sweep_label)
└── _validate_helpers.py     # Notebook-private helpers for validate_strategy
                             #   (STRATEGIES registry, make_strategy_factory,
                             #    get_param_grid, plateau_scores, parse_pnl,
                             #    short_param_key, short_params_tag,
                             #    enrich_regime_with_wilson, collapse_to_grid)
data/
├── catalog/          # ParquetDataCatalog root (gitignored)
└── sweeps/           # Sweep result Parquet files (gitignored)
reports/              # Generated reports (gitignored)
├── charts/           # TradingView Lightweight Charts HTML reports
├── sweeps/           # Sortable HTML sweep tables (per-sweep + cross-sweep)
├── validate/         # Verdict JSONs from validate_strategy.ipynb
├── html/
│   ├── backtest/         # Notebook HTML exports from notebooks/backtest/
│   ├── validate/         # Notebook HTML exports from validate_strategy.ipynb
│   ├── validate_all/     # Notebook HTML exports from validate_all.ipynb
│   └── compare/          # Notebook HTML exports from compare_sweeps.ipynb
├── notebooks/
│   ├── backtest/         # Notebook snapshots from notebooks/backtest/
│   ├── validate/         # Notebook snapshots from validate_strategy.ipynb
│   ├── validate_all/     # Notebook snapshots from validate_all.ipynb
│   └── compare/          # Notebook snapshots from compare_sweeps.ipynb
└── tearsheets/       # NT tearsheet HTML (saved when SAVE_TEARSHEET=True)
tests/                # unit/ and integration/
```

## Development Phases

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the full roadmap including the
gates between phases. The condensed status here:

**Phase 1 (complete):** Backtesting with NT's `BacktestEngine`, project conveniences (`make_engine`, `run_sweep`, fee overrides, MA-cross + BB + Donchian + MACD-RSI), v2 metrics schema with realized-PnL-only stats.

**Phase 2 (complete):** TradingNode + PersistenceActor + AlertActor, paper trading via NT Sandbox adapter, PostgreSQL persistence, Telegram alerts, Grafana monitoring, Docker on Digital Ocean. Validated against original (polling-mode) MACross; needs revalidation against the post-cross-gate strategy as the first task of Phase 2.5.

**Phase 3a (complete) — Research tooling + strategy validation:**
- `run_sweep()` — parameter grid search with automatic Parquet persistence to `data/sweeps/`.
- `run_walk_forward()` — sliding-window walk-forward analysis (train on N%, test on M%, slide).
- `rolling_performance()` — per-window PnL consistency analysis.
- `tag_regimes()` + `performance_by_regime()` — market regime detection (ADX-based) and per-regime stats.
- `run_fee_sweep()` — fee sensitivity analysis with breakeven detection.
- `bootstrap_total_pnl()` + `bootstrap_max_drawdown()` — per-trade resampling CIs in `src.backtesting.metrics`.
- `compare_sweeps.ipynb` — load saved sweeps, side-by-side heatmaps with liquidated-cell flags, best-params table, parameter stability across instruments/timeframes (including coefficient-of-variation), sortable HTML cross-sweep table.
- `validate_strategy.ipynb` — 8-check go/no-go verdict per (instrument, combo): plateau (with survival-rate accounting), walk-forward (with param-stability check), bootstrap (PnL CI + max-drawdown CI, capital-relative thresholds), rolling (active vs inactive split), fee sensitivity, regime breakdown (Wilson CI on win-rate), yearly concentration. Optional `OVERRIDE_PARAMS` validates cross-sweep robust combos. Persists each verdict as JSON.
- `validate_all.ipynb` — strategy-level consolidator: reads every `reports/validate/*_verdict.json` and renders the comparison matrix + per-check failure-rate.
- v2 tearsheet template (no broken returns-based stats — see [`docs/ANALYZER_RETURNS_CAVEAT.md`](docs/ANALYZER_RETURNS_CAVEAT.md)).
- TradingView Lightweight Charts HTML reports with marker accuracy fixes (cross-gate-aware, side-aware stop visuals, per-fill OID attribution).
- `LiquidationAware` + `ProtectiveStopAware` strategy mixins; `AccountAliveMonitor` actor.
- **Cross-gate entry semantics** in `MACross` (signal-event-driven, not state-polled). See [`docs/STRATEGY_ENTRY_RULES.md`](docs/STRATEGY_ENTRY_RULES.md).
- **Centralized configuration** via `src/config/settings.py`; same `.env` flows through backtest → paper → live. See [`docs/CONFIG.md`](docs/CONFIG.md).
- `scripts/batch_backtest.py` headless cross-product runner with embedded sweep heatmaps and master-index summary. See [`docs/BATCH_BACKTEST.md`](docs/BATCH_BACKTEST.md).

**Phase 2.5 (next) — Paper-trading revalidation.** Confirm the cross-gated MACross + protective-stop + liquidation-simulator stack behaves the same in paper as in backtest. Two-week minimum run on Hyperliquid testnet. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Phase 2.6 — Backtest accuracy validation.** The keystone phase: build the comparison framework that answers "are our backtests accurate?" Live-vs-backtest harness, rolling accuracy regression, accuracy gate between research and live. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

**Phase 3 — Live trading (small capital).** Gated on Phase 2.6's measured haircut staying within tolerance.

**Phase 4 (later) — Multi-strategy + portfolio.** Strategy correlation, portfolio-level kill switches, capital allocation.

**Phase 5 — NT v2 migration when upstream lands.** Track [issue #4042](https://github.com/nautechsystems/nautilus_trader/issues/4042) for the v2 RFC. Particularly interested in returns-based stats fix, native liquidation simulation, bar-fill model improvements.

**Phase 3b (deferred) — Web layer.** FastAPI read-only API + React dashboard + StreamingActor → Redis Streams. Build only when Grafana + Telegram aren't enough. Plumbing already specified.

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
1. Create or open a notebook in `notebooks/backtest/` for the strategy.
2. Define a `strategy_factory(engine, params)` function and a list of `param_combos`.
3. Call `run_sweep()` — results auto-save to `data/sweeps/{strategy}_{instrument}_{interval}.parquet`. Repeat for each instrument you want to test (BTC/ETH/SOL/...).
4. Inspect heatmaps in the backtest notebook. For multi-stage sweeps (e.g. MACD periods → RSI thresholds), use a different `strategy_name` for the sensitivity sweep.
5. Compare across instruments/timeframes: open `notebooks/compare_sweeps.ipynb`, Run All. Review the best-params table, side-by-side heatmaps, and the parameter-stability table — pick a candidate combo (typically the cross-sweep robust one with low `cv_pnl_pct`).
6. Validate per-instrument: open `notebooks/validate_strategy.ipynb`, edit cell 1.1 to set `STRATEGY` / `ASSET` / `BAR_INTERVAL` (auto-pick) or also `OVERRIDE_PARAMS = {...}` (validate a specific combo). Run All. Each run drops a verdict JSON. Repeat per (instrument, pick) you want to compare.
7. Strategy-level rollup: open `notebooks/validate_all.ipynb`, Run All. The matrix + failure-rate view shows whether the candidate combo passes across instruments.
8. Only proceed to paper trading (Phase 2 workflow) if the verdict matrix is dominated by ✅ / ⚠️ (no consistent 🚩 across instruments).

### Adding a new strategy
1. Create a new file in `src/strategies/`.
2. Subclass `nautilus_trader.trading.strategy.Strategy`.
3. Implement `on_start()`, `on_bar()` (or `on_quote_tick()`), and order management callbacks.
4. Create a notebook in `notebooks/backtest/`. Define a `strategy_factory` and `param_combos`.
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
- **Analyzer returns stats are unreliable.** NT's `get_performance_stats_returns()` (Sharpe, Sortino, Volatility, returns-based Profit Factor) uses a flawed methodology in v1.225.0 and v1.226.0 — daily-resampled equity with `.ffill().pct_change()` zero-pads non-trading days, which biases Sharpe for sparse-trade strategies. PnL-section stats (Total PnL, Win Rate, Expectancy, PnL-based Profit Factor) are correct. See `docs/ANALYZER_RETURNS_CAVEAT.md`. Do not use Sharpe/Sortino for strategy selection or go/no-go decisions until NT fixes this upstream (likely NT v2 — see [issue #4042](https://github.com/nautechsystems/nautilus_trader/issues/4042)).
- **Actor callbacks must never block.** Use `self.run_in_executor(callable, args)` for all I/O. Blocking directly in `on_order_filled` or similar callbacks stalls the TradingNode.
- **asyncpg in Actor executors:** Use `asyncio.run(asyncpg.connect(...))` inside `run_in_executor` callables. A fresh connection per write is acceptable at hourly-bar frequency. No connection pool needed in Phase 2.
- **Actor has no `create_task`.** NT 1.226.0 Actor uses `run_in_executor` (ThreadPoolExecutor) for I/O, not `create_task`. Strategy has different APIs than Actor.
- **Position events in Actors:** Override `on_event(self, event)` and check `isinstance(event, PositionClosed)`. Actor has `on_order_filled` but no `on_position_closed` callback.
- **NT financial types to PostgreSQL NUMERIC:** Always `str(event.last_px)`, never `float(event.last_px)`. asyncpg accepts strings for NUMERIC columns and preserves precision.
- **HL_TESTNET defaults to True.** `run_live.py` requires explicitly setting `HL_TESTNET=false` in `.env` to trade on mainnet. Intentional friction.
- **HyperliquidDataClientConfig needs NO credentials.** Just `testnet=False` for real market data. Credentials are only needed on the exec client.
- **HyperliquidExecClientConfig uses `private_key` + `vault_address`** (NOT `wallet_address`). Verified in NT 1.226.0. The `testnet=` kwarg was removed in 1.226 — use `environment=HyperliquidEnvironment.{TESTNET,MAINNET}` (re-exported pyo3 enum, requires `# type: ignore[attr-defined]` on the import).
- **Adapter factories must be registered.** Call `node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)` and `node.add_exec_client_factory(...)` before `node.build()`.
- **Sweep filename is deterministic.** `run_sweep()` saves to `{strategy}_{instrument}_{interval}.parquet`. Re-running the same combo overwrites the previous file. The `_swept_at` metadata column inside the file records when it was generated.
- **Walk-forward is expensive.** `run_walk_forward()` runs the full param grid per fold. With 60 combos × 4 folds = 240 backtests. Budget 3-5 min for hourly bars, 15-20 min for 5m bars over a year.
- **Binance API geo-blocked in some regions.** Connect NordVPN (`nordvpn connect`) before running `fetch_binance_candles.py`, or use `--testnet` for development (no geo-block, perp only — spot has no testnet). The script detects connection failures and prints VPN instructions.
- **MIT/LIT orders never trigger in bar-only backtests.** NT's SimulatedExchange bid/ask are never initialized from OHLCV data, and synthetic TradeTicks from bar decomposition are not published to the message bus (so OrderEmulator can't trigger either). Use manual trigger checking in `on_bar` + MARKET orders instead. See `docs/BAR_BACKTESTING_GOTCHAS.md`.
- **Margin is not enforced on MARGIN accounts in NT 1.226.0.** RiskEngine short-circuits the margin check (`risk/engine.pyx:678`); no liquidation engine exists in the backtester (verified by exhaustive grep, unchanged from 1.225). The project simulates this via a `LiquidationAware` strategy mixin + `AccountAliveMonitor` actor. Opt in by passing `LiquidationConfig(enabled=True)` and `venue_config=` to `make_engine`; pass the resolved config into the strategy too. **Inheritance order is non-negotiable**: `class Foo(LiquidationAware, Strategy):` (mixin first; reverse silently disables it). **Disable in live** (`run_live.py`) — venues handle their own liquidation; currently no auto-detection. See `docs/LIQUIDATION_AND_SIZING.md`.
- **Stacking strategy mixins requires cooperative `super()` AND mixins-first MRO.** When a strategy inherits multiple mixins (e.g. `class MACross(ProtectiveStopAware, LiquidationAware, Strategy)`), every mixin's event handlers MUST call `super().on_*()` first — even when the mixin's "disabled" branch returns early. Without it, an upstream mixin returning early silently swallows events and downstream mixins never run. NT calls handlers by name via MRO; `Strategy`'s base no-op stubs sit at the end of the chain and won't trigger any mixin if MRO order is reversed. For new mixins, copy the pattern in `src/core/protective_stop_mixin.py` and add a regression test asserting super() chains (see `tests/unit/test_protective_stop_mixin.py::TestSuperChain`).
- **NT MessageBus caches subscriber lists per concrete topic.** Wildcard subscriptions (e.g. `events.account.*`) added AFTER a matching topic has been published once will not catch future events — re-resolution only fires when the cached list is empty. Subscribe in `make_engine` before `engine.run()`, not in actor `on_start`. See the `_register_account_alive_monitor` comment in `src/backtesting/engine.py` for the load-bearing example.

## Communicating with This Developer

- Be direct and opinionated when evidence supports it.
- Flag tradeoffs honestly — don't soft-pedal downsides.
- Use concrete numbers, versions, and specific technical details.
- Skip beginner explanations unless asked.
- If a recommendation would mean "fighting the framework," say so upfront.
