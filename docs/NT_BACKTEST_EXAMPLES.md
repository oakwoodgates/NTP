# NautilusTrader v1.225.0 — Backtest Examples Report

Reference: `.ref/nautilus_trader-1.225.0/examples/backtest/`

---

## 1. Directory Overview

```
examples/backtest/
├── 20 standalone scripts          (.py — complete runnable backtests)
├── 11 structured examples         (example_01–11/ — multi-file, tutorial-style)
├── notebooks/                     (7 Jupytext .py files — Databento-focused)
└── model_configs_example.py       (config-only reference, no runnable backtest)
```

Plus `examples/strategies/` contains **17 strategy classes** used by the backtest scripts.

---

## 2. Standalone Backtest Scripts

### Crypto (most relevant to us)

| Script | Strategy | Instrument | Venue / Account | Data Source | Key Features |
|--------|----------|------------|-----------------|-------------|--------------|
| `crypto_ema_cross_ethusdt_trade_ticks.py` | EMACrossTWAP | ETHUSDT (Binance) | BINANCE / CASH, multi-currency | TradeTickDataWrangler from CSV | **TWAP exec algo**, 250-tick bars (INTERNAL), tearsheet generation |
| `crypto_ema_cross_ethusdt_trailing_stop.py` | EMACrossTrailingStop | ETHUSDT | BINANCE / MARGIN | TradeTickDataWrangler | ATR-based trailing stop, trigger type config |
| `crypto_ema_cross_with_binance_provider.py` | EMACrossTrailingStop | ETHUSDT-PERP | BINANCE / MARGIN | **BinanceFuturesInstrumentProvider** (async HTTP) + test bars | Live instrument provider in backtest context |
| `crypto_orderbook_imbalance.py` | OrderBookImbalance | BTCUSDT | BINANCE / CASH, NETTING | BinanceOrderBookDeltaDataLoader (CSV snapshots + updates) | L2_MBP order book, ~1M delta rows |
| `bitmex_grid_market_maker.py` | GridMarketMaker | XBTUSD (inverse perp) | BITMEX / MARGIN, BTC base, 1:100 leverage | TardisCSVDataLoader | **Maker rebate** (-0.025%), 3 grid levels, inventory skew |

### FX

| Script | Strategy | Instrument | Key Features |
|--------|----------|------------|--------------|
| `fx_ema_cross_audusd_bars_from_ticks.py` | EMACross | AUD/USD | **FXRolloverInterestModule**, tick-to-bar (INTERNAL), HEDGING OMS |
| `fx_ema_cross_audusd_ticks.py` | EMACross | AUD/USD | Simpler variant, no brackets |
| `fx_ema_cross_bracket_gbpusd_bars_external.py` | EMACrossBracket | GBP/USD | **FillModel** (20% limit fill, 50% slippage), HEDGING, bracket orders, **RiskEngine bypassed** |
| `fx_ema_cross_bracket_gbpusd_bars_internal.py` | EMACrossBracket | GBP/USD | Same but INTERNAL bar aggregation from ticks |
| `fx_market_maker_gbpusd_bars.py` | VolatilityMarketMaker | GBP/USD | Partial run via `engine.run(end=datetime(...))` |

### Equities / Futures

| Script | Strategy | Instrument | Key Features |
|--------|----------|------------|--------------|
| `databento_ema_cross_long_only_aapl_bars.py` | EMACrossLongOnly | AAPL (NASDAQ) | Multi-timeframe data (1s + 1m), CASH account, RiskEngine bypassed |
| `databento_ema_cross_long_only_spy_trades.py` | EMACrossLongOnly | SPY | Multiple monthly data files, 1000-tick bars |
| `databento_ema_cross_long_only_tsla_trades.py` | EMACrossLongOnly | TSLA (NYSE) | 1-min bars from trade data |
| `databento_cme_quoter.py` | SimpleQuoterStrategy | ES Future (CME) | $1M capital, RiskEngine bypassed, multiple DBN files |
| `synthetic_data_pnl_test.py` | MinimalStrategy | 6E (EUR FX futures) | **Synthetic bars**, PerContractFeeModel ($2.50), P&L debugging |

### Other Venues

| Script | Strategy | Key Features |
|--------|----------|--------------|
| `architect_ax_book_imbalance.py` | OrderBookImbalance | Custom AX venue, DatabentoDataLoader (.dbn.zst), L1_MBP |
| `architect_ax_mean_reversion.py` | BBMeanReversion | Custom CSV loading (TrueFX format), BB + RSI |
| `betfair_backtest_orderbook_imbalance.py` | OrderBookImbalance | BETFAIR, L2_MBP, betting instruments, multi-instrument |
| `polymarket_simple_quoter.py` | EMACrossLongOnly | Polymarket prediction market, async API data loading, USDC |

### Config Reference (not runnable)

| Script | Purpose |
|--------|---------|
| `model_configs_example.py` | Shows **ImportableStrategyConfig**, **ImportableFillModelConfig**, **ImportableLatencyModelConfig**, **ImportableFeeModelConfig**, **BacktestVenueConfig**, **BacktestRunConfig**, **BacktestNode** |

---

## 3. Structured Examples (01–11)

These are tutorial-style, each with `run_example.py` + `strategy.py`.

| # | Topic | What It Teaches | Platform Relevance |
|---|-------|-----------------|-------------------|
| 01 | Load bars from custom CSV | CSV → pandas → BarDataWrangler → engine.add_data() | Data pipeline reference |
| 02 | Clock timers | `clock.set_timer()` → `on_timer(TimeEvent)` | Periodic checks in strategies |
| 03 | Bar aggregation | Subscribe to 1-min, aggregate to 5-min internally. Syntax: `"{target}@{source}"` | **Multi-timeframe strategies** |
| 04 | ParquetDataCatalog | `catalog.write_data()`, `catalog.instruments()`, `catalog.bars()`, `catalog.list_data_types()`, date filtering | **We use this for data storage** |
| 05 | Portfolio API | `portfolio.is_flat()`, `.net_position()`, `.net_exposure()`, `.realized_pnl()`, `.unrealized_pnl()`, `.margins_init()`, `.margins_maint()`, `.balances_locked()` | Result extraction, position monitoring |
| 06 | Cache API | Custom objects via `cache.add()/get()`, instruments, accounts, bars (with index), orders (open/closed), positions (open/closed), strategy/actor IDs. Config: `bar_capacity`, `tick_capacity` | **Result extraction patterns** |
| 07 | Indicators | `MovingAverageFactory.create()`, `register_indicator_for_bars()`, `indicator.initialized` check, history via deque | Standard pattern we follow |
| 08 | Cascaded indicators | EMA of EMA, manual `indicator.update_raw(value)` feeding | Advanced indicator patterns |
| 09 | MessageBus events | Custom `Event` dataclass, `msgbus.subscribe(topic, handler)`, `msgbus.publish(topic, event)` | Actor/Strategy communication |
| 10 | Actor custom data | `Data` subclass, `subscribe_data(DataType(...))`, `publish_data()`, `on_data()` handler. Serializable variant with `@customdataclass` | Inter-component data sharing |
| 11 | Actor signals | Lightweight `publish_signal(name, value, ts_event)`, `subscribe_signal()`, `on_signal()`. Values: str/int/float only | Simple notifications |

---

## 4. Jupytext Notebooks

All in `examples/backtest/notebooks/`, Databento-focused (CME futures):

| Notebook | Focus |
|----------|-------|
| `databento_backtest_with_data_client.py` | **BacktestNode with live DatabentoDataClient** — registers data client factory, requests instruments dynamically, subscribes to bars + order book depth in backtest |
| `databento_test_request_bars.py` | **`request_aggregated_bars()`** — multi-timeframe aggregation (1m→2m→4m→5m), historical bar requests with callbacks, supports bars/quotes/trades data types, `DataEngineConfig.time_bars_origin_offset` |
| `databento_download.py` | Data download from Databento API |
| `databento_futures_settlement.py` | Contract expiry and settlement handling |
| `databento_option_exercise.py` | Options contract exercise |
| `databento_option_greeks.py` | Greeks calculation |
| `databento_test_order_book_deltas.py` | Order book delta processing |

---

## 5. Strategy Catalog

### Trend-Following

| Strategy | Order Types | Position Mgmt | Indicators | Notes |
|----------|-------------|---------------|------------|-------|
| **EMACross** | MARKET | Bidirectional flip (flat→long→short) | 2x EMA | Canonical example. Handles hedge mode with explicit LONG/SHORT position IDs |
| **EMACrossLongOnly** | MARKET (IOC) | Long only, enter/close | 2x EMA | For CASH accounts (spot). Uses IOC TIF |
| **EMACrossBracket** | LIMIT_IF_TOUCHED + bracket | Bracket (entry + SL + TP) | EMA, ATR | ATR-based bracket distance, GTD 30s expiry, OrderList for coordinated submission |
| **EMACrossBracketAlgo** | STOP_LIMIT + bracket | Same as bracket | EMA, ATR | Routes through execution algorithms (entry/SL/TP each get algo ID) |
| **EMACrossStopEntry** | MARKET_IF_TOUCHED + TRAILING_STOP_MARKET | Entry via MIT, exit via trailing | EMA, ATR | Watches OrderFilled via `on_event()`, dynamic trailing offset |
| **EMACrossTrailingStop** | MARKET + TRAILING_STOP_MARKET | Market entry, trailing exit | EMA, ATR | Watches PositionOpened/Changed/Closed, quote tick subscription for price ref |
| **EMACrossTWAP** | MARKET via TWAP algo | Same as EMACross | 2x EMA | `ExecAlgorithmId("TWAP")` with horizon/interval params |
| **EMACrossHedgeMode** | MARKET | Separate LONG/SHORT positions | 2x EMA | `PositionId("{instrument}-LONG/SHORT")` pattern for Binance |

### Mean Reversion

| Strategy | Order Types | Indicators | Notes |
|----------|-------------|------------|-------|
| **BBMeanReversion** | MARKET | Bollinger Bands, RSI | Buy lower band with RSI confirmation, sell upper band, exit at middle band |

### Market Making

| Strategy | Order Types | Key Features |
|----------|-------------|--------------|
| **GridMarketMaker** | LIMIT (post-only) | Multi-level grid, geometric spacing (bps), inventory skew (Avellaneda-Stoikov), requote threshold, GTD expiry |
| **MarketMaker** | LIMIT | Simple 2-sided quoting, position-based spread adjustment, OrderBook delta driven |
| **VolatilityMarketMaker** | LIMIT (post-only, GTD 10min) | ATR-based pricing, emulation trigger support |
| **SimpleQuoterStrategy** | LIMIT | Configurable TOB offset (ticks), minimal state machine |

### Order Book

| Strategy | Order Types | Key Features |
|----------|-------------|--------------|
| **OrderBookImbalance** | LIMIT (FOK) | Bid/ask size ratio trigger, cooldown, dry-run mode, configurable book type (L1/L2) |

### Utility / Testing

| Strategy | Purpose |
|----------|---------|
| **SignalStrategy** | Publishes signals on tick events (no trading) |
| **SubscribeStrategy** | Subscribes to all data types for debugging |
| **MyStrategy (blank.py)** | Template with all lifecycle hooks |

---

## 6. Key Configuration Patterns

### Venue Configuration

```python
engine.add_venue(
    venue=Venue("BINANCE"),
    oms_type=OmsType.NETTING,          # NETTING | HEDGING
    book_type=BookType.L1_MBP,         # L1_MBP | L2_MBP
    account_type=AccountType.MARGIN,   # CASH | MARGIN
    base_currency=None,                # None = multi-currency
    starting_balances=[Money(1_000_000, USDT), Money(10, ETH)],
    fill_model=fill_model,             # Optional
    latency_model=latency_model,       # Optional
    fee_model=fee_model,               # Optional
    modules=[fx_rollover_interest],    # Optional venue modules
    bar_execution=True,                # Bars trigger fills (default True)
    trade_execution=True,              # Only with L1_MBP or throttled book data
    default_leverage=Decimal("10"),    # For MARGIN accounts
)
```

### Fill Model

```python
fill_model = FillModel(
    prob_fill_on_limit=0.2,    # 20% chance limit orders fill
    prob_slippage=0.5,         # 50% chance of slippage
    random_seed=42,            # Reproducibility
)
```

Used in: `fx_ema_cross_bracket_gbpusd_bars_external.py`
**Most examples don't set a FillModel** — they use the default (100% fill, 0% slippage).

### Fee Models (3 types)

```python
# Crypto exchanges (maker/taker)
MakerTakerFeeModel()  # Uses instrument's maker_fee/taker_fee

# Fixed per trade
FixedFeeModel(commission=Money(1.50, USD), charge_commission_once=True)

# Futures (per contract)
PerContractFeeModel(commission=Money(2.50, USD))
```

### Latency Model

```python
LatencyModel(
    base_latency_nanos=5_000_000,      # 5ms base
    insert_latency_nanos=2_000_000,    # +2ms for new orders
    update_latency_nanos=3_000_000,    # +3ms for modifications
    cancel_latency_nanos=1_000_000,    # +1ms for cancels
)
```

### Risk Engine

```python
BacktestEngineConfig(
    risk_engine=RiskEngineConfig(bypass=True),  # Skip pre-trade checks
)
```

Used in bracket order examples and some equity examples. **Bypassed to allow bracket orders without pre-trade validation interfering.**

### Tearsheet Generation

```python
from nautilus_trader.analysis import TearsheetConfig
from nautilus_trader.analysis.tearsheet import create_tearsheet

tearsheet_config = TearsheetConfig(theme="plotly_white")
# Themes: "plotly_white", "plotly_dark", "nautilus", "nautilus_dark"

create_tearsheet(
    engine=engine,
    output_path="tearsheet.html",
    config=tearsheet_config,
)
```

Demonstrated in `crypto_ema_cross_ethusdt_trade_ticks.py`. Requires `plotly>=6.3.1`.

---

## 7. Data Loading Patterns

### Pattern A: Wranglers (most common in examples)

```python
# Trade ticks
wrangler = TradeTickDataWrangler(instrument=ETHUSDT)
ticks = wrangler.process(provider.read_csv_ticks("binance/ethusdt-trades.csv"))
engine.add_data(ticks)

# Quote ticks
wrangler = QuoteTickDataWrangler(instrument=AUDUSD)
ticks = wrangler.process(provider.read_csv_ticks("truefx/audusd-ticks.csv"))
engine.add_data(ticks)

# Bars (external)
wrangler = BarDataWrangler(bar_type=BarType.from_str("GBP/USD.SIM-1-MINUTE-BID-EXTERNAL"), instrument=GBPUSD)
bars = wrangler.process(data=provider.read_csv_bars("fxcm/gbpusd-m1-bid-2012.csv"))
engine.add_data(bars)

# Order book deltas
wrangler = OrderBookDeltaDataWrangler(instrument=BTCUSDT)
deltas = wrangler.process(snapshots + updates)
engine.add_data(deltas)
```

### Pattern B: ParquetDataCatalog (example 04)

```python
catalog = ParquetDataCatalog("./data_catalog")
catalog.write_data([instruments, bars])           # Persist
instruments = catalog.instruments()               # Retrieve all
bars = catalog.bars(bar_types=[...], start="2024-01-10", end="2024-01-15")
data_types = catalog.list_data_types()            # Discovery
```

### Pattern C: Vendor-specific loaders

```python
# Databento
loader = DatabentoDataLoader()
data = loader.from_dbn_file("path/to/file.dbn.zst", instrument_id=...)

# Tardis
loader = TardisCSVDataLoader()
ticks = loader.load(...)

# Binance order book
loader = BinanceOrderBookDeltaDataLoader()
deltas = loader.load(path_snapshot, path_update)

# Polymarket (async API)
loader = PolymarketDataLoader()
trades = await loader.load_trades(market_slug, ...)
```

### Pattern D: BacktestNode with DataCatalogConfig

```python
catalogs = [DataCatalogConfig(path=catalog.path)]
data = [BacktestDataConfig(
    data_cls=Bar,
    catalog_path=catalog.path,
    instrument_id=InstrumentId.from_str("ESU4.XCME"),
    bar_spec="1-MINUTE-LAST",
    start_time="2024-07-01",
    end_time="2024-07-02",
)]
config = BacktestRunConfig(engine=engine_config, venues=venues, data=data)
node = BacktestNode(configs=[config])
node.run()
```

---

## 8. Result Extraction Patterns

### Reports (DataFrame)

```python
engine.trader.generate_account_report(venue)     # Balance over time
engine.trader.generate_order_fills_report()       # All fills
engine.trader.generate_positions_report()         # Position history
```

### Portfolio API (example 05)

```python
portfolio.is_flat(instrument_id)
portfolio.is_completely_flat()
portfolio.net_position(instrument_id)
portfolio.net_exposure(instrument_id)
portfolio.realized_pnl(instrument_id)
portfolio.unrealized_pnl(instrument_id)
portfolio.margins_init(venue)
portfolio.margins_maint(venue)
portfolio.balances_locked(venue)
```

### Cache API (example 06)

```python
cache.instrument(id)
cache.instruments(venue=venue)
cache.account_for_venue(venue)
cache.bars(bar_type)                              # All cached bars
cache.bar(bar_type)                               # Latest bar
cache.bar(bar_type, index=1)                      # Previous bar
cache.orders()                                    # All orders
cache.orders_open()
cache.orders_closed()
cache.positions()                                 # Current positions (NETTING: 1 per instrument-strategy)
cache.positions_open()
cache.positions_closed()
cache.position_snapshots()                        # Historical closed positions
cache.strategy_ids()
cache.actor_ids()
# Custom data
cache.add("my_key", pickle.dumps(obj))
cache.get("my_key")  # returns bytes
```

### Analyzer (referenced in CLAUDE.md, not in examples)

```python
account = engine.cache.account_for_venue(venue)
positions = engine.cache.position_snapshots() + engine.cache.positions()
analyzer.calculate_statistics(account, positions)  # MUST call before accessing stats
```

---

## 9. Platform-Relevant Highlights

### Things we should verify in our backtests

1. **`bar_execution=True` is the default.** All crypto examples use it. This means bar OHLC data moves the simulated exchange's market. We should confirm our `make_engine()` uses this.

2. **`trade_execution=True` is only used with L1_MBP or throttled book data.** The ETHUSDT trade ticks example sets this explicitly. If we're using trade ticks, this matters.

3. **Multi-currency accounts for crypto.** The Binance examples use `base_currency=None` with `starting_balances=[Money(1_000_000, USDT), Money(10, ETH)]`. For perps, `base_currency` might be set to the settlement currency.

4. **NETTING OMS for crypto.** All crypto examples use NETTING (one position per instrument-strategy), which matches exchange behavior.

5. **FillModel is rarely used in examples.** Only the FX bracket example configures it. Most examples accept 100% fill probability and 0% slippage — this is unrealistic for production. Our CLAUDE.md correctly flags slippage modeling as important.

6. **No LatencyModel in any runnable example.** Only shown in the config reference (`model_configs_example.py`). Worth considering for more realistic backtests.

7. **RiskEngine bypass** is used in bracket order and some equity examples. We should understand when/why to bypass vs. keep enabled.

8. **`engine.reset()` before `engine.dispose()`** — the examples show this cleanup pattern for repeated runs. Relevant for our sweep orchestration.

9. **Tearsheet generation** uses `create_tearsheet(engine, output_path, config)` — a newer API than `generate_backtest_html()`. We should check if we're using the latest tearsheet API.

10. **`request_aggregated_bars()`** — the Databento notebook shows sophisticated multi-timeframe bar aggregation with callbacks and historical warmup. Could be useful for multi-timeframe strategies.

### Things the examples do NOT demonstrate

- **Sweep orchestration** (parameter grid search) — our `run_sweep()` is custom
- **Walk-forward analysis** — our `run_walk_forward()` is custom
- **Fee sweep / sensitivity analysis** — our `run_fee_sweep()` is custom
- **Regime detection** — our `tag_regimes()` / `performance_by_regime()` is custom
- **Automated Parquet persistence of results** — our sweep-to-Parquet pipeline is custom
- **Position snapshots for complete stats** — examples don't show `cache.position_snapshots() + cache.positions()` pattern (our CLAUDE.md documents this gotcha)

### Strategies we could learn from

- **GridMarketMaker** — sophisticated market making with inventory skew, requote thresholds, grid levels. Relevant if we add market-making strategies.
- **EMACrossBracket** — bracket order pattern (entry + SL + TP as coordinated OrderList). We currently use MARKET-only; brackets could improve risk management.
- **EMACrossStopEntry** — conditional entry via MARKET_IF_TOUCHED + trailing stop exit. More sophisticated entry timing than market orders.
- **EMACrossTWAP** — execution algorithm integration. Useful for larger position sizes where market impact matters.

### Data loading patterns we might adopt

- **Synthetic data for P&L testing** (`synthetic_data_pnl_test.py`) — creating controlled bar data to verify P&L calculations are correct. Could be valuable for testing our analyzer stats pipeline.
- **Live instrument provider in backtest** (`crypto_ema_cross_with_binance_provider.py`) — loads real instrument definitions from exchange API rather than hardcoding. Ensures correct tick sizes, lot sizes, fee rates.

---

## 10. Quick Reference: Engine Setup Checklist

Based on patterns across all examples, a well-configured backtest engine should address:

| Setting | Options | Crypto Default |
|---------|---------|---------------|
| `oms_type` | NETTING / HEDGING | NETTING |
| `account_type` | CASH / MARGIN | MARGIN (perps), CASH (spot) |
| `base_currency` | Currency / None | None (multi-currency) or settlement currency |
| `book_type` | L1_MBP / L2_MBP | L1_MBP (unless using order book data) |
| `bar_execution` | True / False | True |
| `trade_execution` | True / False | True (only with L1_MBP) |
| `fill_model` | FillModel / None | Should configure (examples don't, but we should) |
| `fee_model` | MakerTaker / Fixed / PerContract | MakerTakerFeeModel (uses instrument fees) |
| `latency_model` | LatencyModel / None | Optional (none in examples) |
| `risk_engine.bypass` | True / False | False (unless bracket orders) |
| `starting_balances` | List[Money] | Match intended account size |
| `default_leverage` | Decimal | Match exchange leverage |
| Logging | log_level | "ERROR" in notebooks, "INFO" in scripts |

---

## 11. File Index

### Standalone Scripts
```
examples/backtest/
├── architect_ax_book_imbalance.py
├── architect_ax_mean_reversion.py
├── betfair_backtest_orderbook_imbalance.py
├── bitmex_grid_market_maker.py
├── crypto_ema_cross_ethusdt_trade_ticks.py
├── crypto_ema_cross_ethusdt_trailing_stop.py
├── crypto_ema_cross_with_binance_provider.py
├── crypto_orderbook_imbalance.py
├── databento_cme_quoter.py
├── databento_ema_cross_long_only_aapl_bars.py
├── databento_ema_cross_long_only_spy_trades.py
├── databento_ema_cross_long_only_tsla_trades.py
├── fx_ema_cross_audusd_bars_from_ticks.py
├── fx_ema_cross_audusd_ticks.py
├── fx_ema_cross_bracket_gbpusd_bars_external.py
├── fx_ema_cross_bracket_gbpusd_bars_internal.py
├── fx_market_maker_gbpusd_bars.py
├── model_configs_example.py
├── polymarket_simple_quoter.py
└── synthetic_data_pnl_test.py
```

### Structured Examples
```
examples/backtest/
├── example_01_load_bars_from_custom_csv/
├── example_02_use_clock_timer/
├── example_03_bar_aggregation/
├── example_04_using_data_catalog/
├── example_05_using_portfolio/
├── example_06_using_cache/
├── example_07_using_indicators/
├── example_08_cascaded_indicator/
├── example_09_messaging_with_message_bus/
├── example_10_messaging_with_actor_data/
└── example_11_messaging_with_actor_signals/
```

### Notebooks
```
examples/backtest/notebooks/
├── databento_backtest_with_data_client.py
├── databento_download.py
├── databento_futures_settlement.py
├── databento_option_exercise.py
├── databento_option_greeks.py
├── databento_test_order_book_deltas.py
└── databento_test_request_bars.py
```

### Strategies
```
examples/strategies/
├── bb_mean_reversion.py          (BBMeanReversion)
├── blank.py                      (MyStrategy — template)
├── ema_cross.py                  (EMACross)
├── ema_cross_bracket.py          (EMACrossBracket)
├── ema_cross_bracket_algo.py     (EMACrossBracketAlgo)
├── ema_cross_hedge_mode.py       (EMACrossHedgeMode)
├── ema_cross_long_only.py        (EMACrossLongOnly)
├── ema_cross_stop_entry.py       (EMACrossStopEntry)
├── ema_cross_trailing_stop.py    (EMACrossTrailingStop)
├── ema_cross_twap.py             (EMACrossTWAP)
├── grid_market_maker.py          (GridMarketMaker)
├── market_maker.py               (MarketMaker)
├── orderbook_imbalance.py        (OrderBookImbalance)
├── signal_strategy.py            (SignalStrategy)
├── simpler_quoter.py             (SimpleQuoterStrategy)
├── subscribe.py                  (SubscribeStrategy)
└── volatility_market_maker.py    (VolatilityMarketMaker)
```
