# NautilusTrader — Backtest Examples, Strategies & Indicators Reference

## Overview

This report catalogs what NautilusTrader 1.225.0 (the version pinned in `pyproject.toml`) ships with: example backtest scripts, example strategies, example execution algorithms, example indicators, and the full built-in indicator library. The goal is to provide a quick reference for strategy development and research tooling on the NTP platform.

> **Version note:** content was originally captured against 1.224.0 and re-checked at the 1.225.0 pin.  The indicator + execution-algo APIs are stable across this minor bump; if you upgrade NT, re-verify with `python -c "import nautilus_trader; print(nautilus_trader.__version__)"` and refresh by browsing `.ref/nautilus_trader-<version>/` in the project tree.

---

## 1. Built-in Indicators

NT ships a comprehensive Cython-accelerated indicator library. All indicators follow the same pattern: they subclass `Indicator`, accept bar/tick/raw updates, and expose a `.value` property (plus additional outputs where applicable). They auto-initialize after receiving enough data (`.initialized` becomes `True`).

### Moving Averages (`nautilus_trader.indicators.averages`)

| Indicator | Class | Key Parameters | Notes |
|-----------|-------|----------------|-------|
| Simple MA | `SimpleMovingAverage` | `period` | Rolling window mean |
| Exponential MA | `ExponentialMovingAverage` | `period` | Alpha = 2/(period+1) |
| Double Exponential MA | `DoubleExponentialMovingAverage` | `period` | 2×EMA₁ − EMA₂, less lag |
| Weighted MA | `WeightedMovingAverage` | `period`, `weights` | Custom weight array (must sum > 0) |
| Hull MA | `HullMovingAverage` | `period` | Fast and smooth (Alan Hull) |
| Adaptive MA | `AdaptiveMovingAverage` | `period_er`, `period_alpha_fast`, `period_alpha_slow` | Kaufman AMA, adjusts to noise |
| Wilder MA | `WilderMovingAverage` | `period` | EMA with alpha = 1/period |
| Variable Index Dynamic Avg | `VariableIndexDynamicAverage` | `period` | Uses CMO for dynamic smoothing |

All MAs support `handle_bar()`, `handle_quote_tick()`, `handle_trade_tick()`, and `update_raw(double)`. The `MovingAverageFactory.create(period, ma_type)` factory method instantiates any of these by enum.

### Momentum Indicators (`nautilus_trader.indicators.momentum`)

| Indicator | Class | Key Parameters | Output(s) |
|-----------|-------|----------------|-----------|
| RSI | `RelativeStrengthIndex` | `period`, `ma_type` | `.value` (0–1 range by default) |
| Rate of Change | `RateOfChange` | `period`, `use_log` | `.value` (simple or log return) |
| Chande Momentum Oscillator | `ChandeMomentumOscillator` | `period`, `ma_type` | `.value` (−100 to +100) |
| Stochastics | `Stochastics` | `period_k`, `period_d`, `slowing`, `ma_type`, `d_method` | `.value_k`, `.value_d` |
| Commodity Channel Index | `CommodityChannelIndex` | `period`, `scalar`, `ma_type` | `.value` |
| Efficiency Ratio | `EfficiencyRatio` | `period` | `.value` (0–1, Kaufman noise proxy) |
| Relative Volatility Index | `RelativeVolatilityIndex` | `period`, `scalar`, `ma_type` | `.value` |
| Psychological Line | `PsychologicalLine` | `period`, `ma_type` | `.value` (% of up closes) |

**Stochastics note:** Supports two `d_method` options: `"ratio"` (Nautilus native, range-weighted) and `"moving_average"` (cTrader/MetaTrader compatible). Also supports a `slowing` parameter for %K smoothing.

### Trend Indicators (`nautilus_trader.indicators.trend`)

| Indicator | Class | Key Parameters | Output(s) |
|-----------|-------|----------------|-----------|
| MACD | `MovingAverageConvergenceDivergence` | `fast_period`, `slow_period`, `ma_type` | `.value` (fast MA − slow MA) |
| Directional Movement | `DirectionalMovement` | `period`, `ma_type` | `.pos`, `.neg` |
| Aroon Oscillator | `AroonOscillator` | `period` | `.aroon_up`, `.aroon_down`, `.value` |
| Archer MA Trends | `ArcherMovingAveragesTrends` | `fast_period`, `slow_period`, `signal_period`, `ma_type` | `.long_run`, `.short_run` |
| Ichimoku Cloud | `IchimokuCloud` | `tenkan_period`, `kijun_period`, `senkou_period`, `displacement` | `.tenkan_sen`, `.kijun_sen`, `.senkou_span_a`, `.senkou_span_b`, `.chikou_span` |
| Linear Regression | `LinearRegression` | `period` | `.value`, `.slope`, `.intercept`, `.degree`, `.cfo`, `.R2` |
| Bias | `Bias` | `period`, `ma_type` | `.value` (close/MA − 1) |
| Swings | `Swings` | `period` | `.direction`, `.changed`, `.high_price`, `.low_price`, `.length`, `.duration` |

### Volatility Indicators (`nautilus_trader.indicators.volatility`)

| Indicator | Class | Key Parameters | Output(s) |
|-----------|-------|----------------|-----------|
| Average True Range | `AverageTrueRange` | `period`, `ma_type`, `use_previous`, `value_floor` | `.value` |
| Bollinger Bands | `BollingerBands` | `period`, `k` (std dev multiple), `ma_type` | `.upper`, `.middle`, `.lower` |
| Donchian Channel | `DonchianChannel` | `period` | `.upper`, `.middle`, `.lower` |
| Keltner Channel | `KeltnerChannel` | `period`, `k_multiplier`, `ma_type`, `ma_type_atr` | `.upper`, `.middle`, `.lower` |
| Keltner Position | `KeltnerPosition` | `period`, `k_multiplier` | `.value` (position within channel, ±1 = band) |
| Vertical Horizontal Filter | `VerticalHorizontalFilter` | `period`, `ma_type` | `.value` |
| Volatility Ratio | `VolatilityRatio` | `fast_period`, `slow_period`, `ma_type` | `.value` (slow ATR / fast ATR) |

### Volume Indicators (`nautilus_trader.indicators.volume`)

| Indicator | Class | Key Parameters | Output(s) |
|-----------|-------|----------------|-----------|
| On Balance Volume | `OnBalanceVolume` | `period` (0 = no window) | `.value` |
| VWAP | `VolumeWeightedAveragePrice` | (none) | `.value` (resets daily) |
| Klinger Volume Oscillator | `KlingerVolumeOscillator` | `fast_period`, `slow_period`, `signal_period`, `ma_type` | `.value` |
| Pressure | `Pressure` | `period`, `ma_type`, `atr_floor` | `.value`, `.value_cumulative` |

### Other Indicators

| Indicator | Class | Notes |
|-----------|-------|-------|
| Fuzzy Candlesticks | `FuzzyCandlesticks` | Dimensionality reduction via fuzzy membership. Outputs `FuzzyCandle` with direction, size, body, wick enums |
| Spread Analyzer | `SpreadAnalyzer` | Tracks current and average bid-ask spread for an instrument |

### Indicator Registration Pattern

In strategies, indicators auto-update via registration:

```python
self.register_indicator_for_bars(bar_type, self.ema)  # auto-updates on each bar
```

For cascaded indicators (e.g., EMA of EMA), register the primary and manually feed values to the secondary in `on_bar()`.

---

## 2. Example Strategies

NT ships 14 example strategies under `nautilus_trader.examples.strategies`. All are explicitly marked as having no alpha advantage and are not intended for live trading.

### EMA Cross (`ema_cross.py`)

The canonical NT example strategy. Bi-directional (long + short).

- **Indicators:** Fast EMA, Slow EMA
- **Logic:** Fast crosses above slow → buy; fast crosses below slow → sell. Flips positions on cross.
- **Config:** `instrument_id`, `bar_type`, `trade_size`, `fast_ema_period` (default 10), `slow_ema_period` (default 20)
- **Features:** Optional quote/trade tick subscriptions, historical bar requests, configurable TIF, quantity precision, reduce-only on stop

### EMA Cross Long Only (`ema_cross_long_only.py`)

Long-only variant suitable for equity CASH accounts.

- **Logic:** Fast crosses above slow → buy; fast crosses below slow → close longs (no shorting)
- **Config:** Same as EMA Cross but without short side

### EMA Cross Bracket (`ema_cross_bracket.py`)

EMA cross with bracket orders (entry + SL + TP).

- **Indicators:** Fast EMA, Slow EMA, ATR
- **Logic:** On cross, submits `LIMIT_IF_TOUCHED` entry with SL/TP brackets at ATR×multiple distance
- **Config:** Adds `atr_period` (default 20), `bracket_distance_atr` (default 3.0), `emulation_trigger`

### EMA Cross Bracket Algo (`ema_cross_bracket_algo.py`)

Bracket strategy with pluggable execution algorithms for entry, SL, and TP orders.

- **Config:** Adds `entry_exec_algorithm_id`, `sl_exec_algorithm_id`, `tp_exec_algorithm_id` and their params

### EMA Cross Trailing Stop (`ema_cross_trailing_stop.py`)

EMA cross with trailing stop management.

- **Indicators:** Fast EMA, Slow EMA, ATR
- **Logic:** Market entry on cross, then `TRAILING_STOP_MARKET` at ATR×multiple distance
- **Config:** Adds `trailing_atr_multiple`, `trailing_offset_type`, `trigger_type`

### EMA Cross Stop Entry (`ema_cross_stop_entry.py`)

EMA cross with `MARKET_IF_TOUCHED` entry and trailing stop.

- **Logic:** On cross, submits MIT order above/below current bar. If filled, submits trailing stop.
- **Config:** Adds `trailing_offset`, `trailing_offset_type`, `trigger_type`

### EMA Cross TWAP (`ema_cross_twap.py`)

EMA cross that executes via the TWAP algorithm.

- **Logic:** Same cross logic but orders route through `TWAP` execution algorithm
- **Config:** Adds `twap_horizon_secs` (default 30), `twap_interval_secs` (default 3)

### EMA Cross Hedge Mode (`ema_cross_hedge_mode.py`)

EMA cross for Binance hedge mode (separate long/short position IDs).

- **Logic:** Uses `PositionId` with `-LONG`/`-SHORT` suffixes recognized by Binance adapter

### Bollinger Band Mean Reversion (`bb_mean_reversion.py`)

Mean reversion using Bollinger Bands + RSI confirmation.

- **Indicators:** Bollinger Bands (period 20, 2σ), RSI (period 14)
- **Logic:** Price touches lower band + RSI below 0.30 → buy; upper band + RSI above 0.70 → sell; exit at middle band
- **Config:** `bb_period`, `bb_std`, `rsi_period`, `rsi_buy_threshold`, `rsi_sell_threshold`

### Volatility Market Maker (`volatility_market_maker.py`)

Brackets top of book based on ATR-measured volatility.

- **Indicators:** ATR
- **Logic:** Places limit buy/sell orders at ATR×multiple distance from last quote. Replaces on each bar.
- **Config:** `atr_period`, `atr_multiple`, `trade_size`, `emulation_trigger`

### Grid Market Maker (`grid_market_maker.py`)

Inventory-aware grid market maker (Avellaneda-Stoikov inspired).

- **Logic:** Maintains symmetric grid of post-only limit orders around mid-price. Grid shifts by `skew_factor × net_position` to discourage inventory buildup. Orders persist until mid moves beyond `requote_threshold_bps`.
- **Config:** `max_position`, `num_levels` (default 3), `grid_step_bps` (default 10), `skew_factor`, `requote_threshold_bps`, `expire_time_secs`
- **Notable:** Most sophisticated example strategy. Handles position limits, pending order exposure, and self-cancel tracking.

### Order Book Imbalance (`orderbook_imbalance.py`)

Trades bid/ask size imbalances.

- **Logic:** When bid size significantly exceeds ask size (ratio below threshold) and size exceeds minimum → buy at ask (FOK). Reverse for sell.
- **Config:** `max_trade_size`, `trigger_min_size` (default 100), `trigger_imbalance_ratio` (default 0.20), `min_seconds_between_triggers`, `book_type` (L1/L2)
- **Supports:** Both quote ticks (L1) and order book deltas (L2)

### Simple Quoter (`simpler_quoter.py`)

Minimal quoter that places one limit order per side at top-of-book ± offset.

- **Logic:** Places buy at bid − offset, sell at ask + offset. Replaces on fill.
- **Config:** `order_qty`, `tob_offset_ticks`

### Market Maker (`market_maker.py`)

Simpler market maker that adjusts quotes based on inventory.

- **Logic:** Maintains L2 order book, brackets mid ±1%. Shifts by inventory/max_size × 1%.

### Signal Strategy (`signal_strategy.py`)

Testing utility that emits a signal counter on each tick. Demonstrates `publish_signal()`.

### Subscribe Strategy (`subscribe.py`)

Testing utility that subscribes to various data types and logs them. Useful for adapter testing.

### Blank Strategy (`blank.py`)

Template with all callback stubs. Starting point for new strategies.

---

## 3. Execution Algorithms

### TWAP (`nautilus_trader.examples.algorithms.twap`)

Time-Weighted Average Price execution.

- **Logic:** Receives a primary market order, splits it into equal-sized child orders, submits them at regular intervals over a horizon
- **Parameters (via `exec_algorithm_params`):** `horizon_secs`, `interval_secs`
- **Behavior:** First order immediate, last order is the primary itself. Handles remainders. Cancels timer on completion or primary close.

### Blank Algorithm (`nautilus_trader.examples.algorithms.blank`)

Template with all callback stubs (`on_order`, `on_order_list`, lifecycle methods).

---

## 4. Example Backtest Scripts

NT ships 20+ example backtest scripts demonstrating different data sources, instruments, and patterns.

### By Data Source

**Databento (institutional market data):**
- `architect_ax_book_imbalance.py` — XAU-PERP order book imbalance on AX venue
- `architect_ax_mean_reversion.py` — EURUSD-PERP BB mean reversion using TrueFX CSV data
- `databento_cme_quoter.py` — ES futures simple quoter with CME mbp-1 data
- `databento_ema_cross_long_only_aapl_bars.py` — AAPL equity with 1s + 1m bars
- `databento_ema_cross_long_only_spy_trades.py` — SPY equity with trade ticks → tick bars
- `databento_ema_cross_long_only_tsla_trades.py` — TSLA equity with trade ticks

**Databento Notebooks:**
- `databento_backtest_with_data_client.py` — Using Databento data client with BacktestNode
- `databento_download.py` — Downloading data from Databento
- `databento_futures_settlement.py` — Futures expiry and settlement
- `databento_option_exercise.py` — Option exercise at expiry
- `databento_option_greeks.py` — Options trading with portfolio greeks, spread instruments
- `databento_test_order_book_deltas.py` — Order book delta processing
- `databento_test_request_bars.py` — Historical bar aggregation (composite bars, internal bars)

**Tardis (free crypto data):**
- `bitmex_grid_market_maker.py` — XBTUSD inverse perp grid market maker with free Tardis quote data

**Binance (crypto):**
- `crypto_ema_cross_ethusdt_trade_ticks.py` — ETHUSDT with TWAP execution, tick bars, tearsheet generation
- `crypto_ema_cross_ethusdt_trailing_stop.py` — ETHUSDT with trailing stops
- `crypto_ema_cross_with_binance_provider.py` — Using live Binance instrument provider for backtest
- `crypto_orderbook_imbalance.py` — BTCUSDT L2 order book imbalance

**FX (forex):**
- `fx_ema_cross_audusd_bars_from_ticks.py` — AUDUSD from quote ticks with FX rollover interest
- `fx_ema_cross_audusd_ticks.py` — AUDUSD tick bars with FillModel (slippage)
- `fx_ema_cross_bracket_gbpusd_bars_external.py` — GBPUSD bracket with external bid/ask bars
- `fx_ema_cross_bracket_gbpusd_bars_internal.py` — GBPUSD bracket from internal tick-to-bar aggregation
- `fx_market_maker_gbpusd_bars.py` — GBPUSD volatility market maker

**Polymarket (prediction markets):**
- `polymarket_simple_quoter.py` — Fetches live historical trades, runs EMA cross long-only

**Betfair (sports betting):**
- `betfair_backtest_orderbook_imbalance.py` — Order book imbalance on betting exchange

**Synthetic data:**
- `synthetic_data_pnl_test.py` — Manually constructed bars for PnL verification, portfolio P&L debugging

### Tutorials (examples/backtest/example_01 through _11)

Numbered tutorial series with README + strategy + runner:

| # | Topic | Key Concept |
|---|-------|-------------|
| 01 | Load bars from custom CSV | `BarDataWrangler`, CSV → Bar objects |
| 02 | Clock timer | `set_timer()`, `TimeEvent`, periodic callbacks |
| 03 | Bar aggregation | Internal 5-min bars from 1-min data via `@` syntax |
| 04 | Data catalog | `ParquetDataCatalog`, write/read/filter |
| 05 | Portfolio | `portfolio.realized_pnl()`, margins, bracket orders |
| 06 | Cache | Custom object storage (pickle), instrument/account/order/position queries |
| 07 | Indicators | EMA registration, `register_indicator_for_bars()`, history deque |
| 08 | Cascaded indicators | EMA of EMA, manual `update_raw()` feeding |
| 09 | MessageBus events | Custom `Event` dataclass, `msgbus.publish()` / `msgbus.subscribe()` |
| 10 | Actor data pub/sub | Custom `Data` class, `publish_data()` / `subscribe_data()`, serializable vs non-serializable |
| 11 | Actor signals | `publish_signal()` / `subscribe_signal()`, lightweight string-based notifications |

### Configuration-based backtest (`model_configs_example.py`)

Demonstrates `BacktestNode` with importable configs:
- `ImportableStrategyConfig` — strategy loaded by path string
- `ImportableFillModelConfig` — FillModel, LatencyModel, FeeModel (MakerTaker, Fixed, PerContract)
- Multiple venue configs with different fee/fill/latency models

---

## 5. Python Example Indicator

### PyExponentialMovingAverage (`nautilus_trader.examples.indicators.ema_python`)

Pure Python EMA implementation demonstrating how to build custom indicators without Cython:

- Subclasses `Indicator` directly (not `MovingAverage`)
- Implements `handle_quote_tick()`, `handle_trade_tick()`, `handle_bar()`, `update_raw()`
- Shows initialization logic (`_set_has_inputs`, `_set_initialized`)
- Shows `_reset()` for stateful value cleanup

---

## 6. Key Patterns for NTP Strategy Development

### What's directly usable from NT examples

1. **EMA Cross pattern** — Your `EMACross` and `EMACrossATR` strategies already follow this. NT's example confirms the pattern: register indicators, check `indicators_initialized()`, use `portfolio.is_flat()` / `is_net_long()` / `is_net_short()` for position checks.

2. **Notional-based sizing** — NT examples use `instrument.make_qty(trade_size)` which is what your strategies do via `trade_notional`.

3. **ATR for SL/TP** — Your `EMACrossATR` uses ATR multipliers for stops, matching `ema_cross_bracket.py` and `ema_cross_trailing_stop.py` patterns.

4. **Bar type string format** — `{instrument_id}-{step}-{aggregation}-{price_type}-{source}` confirmed across all examples.

### Strategies worth exploring for new NTP strategies

| NT Example | Potential NTP Adaptation | Complexity |
|------------|--------------------------|-----------|
| `bb_mean_reversion.py` | BB + RSI mean reversion for crypto | Low — direct port |
| `grid_market_maker.py` | Grid MM for perps with skew | High — needs careful risk management |
| `orderbook_imbalance.py` | L1 imbalance for Hyperliquid | Medium — need quote tick data |
| `volatility_market_maker.py` | ATR-based limit order placement | Medium |
| `ema_cross_trailing_stop.py` | Add trailing stops to existing strategies | Low — enhancement to EMACrossATR |

### Indicators not yet used in NTP strategies

Your current strategies use EMA, SMA, ATR, MACD, and RSI. Available but unused:

- **Bollinger Bands** — mean reversion signals
- **Ichimoku Cloud** — trend identification with multiple confirmation levels
- **Keltner Channel / Position** — volatility-based trend following
- **Donchian Channel** — breakout strategies
- **Stochastics** — overbought/oversold with %K/%D crossovers
- **Aroon Oscillator** — trend strength and direction
- **Linear Regression** — slope/R² for trend quality assessment
- **Swings** — swing high/low detection for support/resistance
- **Pressure** — volume-weighted buying/selling pressure
- **VWAP** — intraday fair value reference

### Execution algorithms

NT's TWAP example is production-quality and directly usable. For strategies with larger position sizes, routing through TWAP via `exec_algorithm_id` and `exec_algorithm_params` on market orders would reduce market impact.

---

## 7. Data Loading Patterns

From the examples, NT supports these data ingestion paths:

| Method | Use Case | Example |
|--------|----------|---------|
| `ParquetDataCatalog` | Primary catalog for backtests | Your `fetch_*.py` scripts |
| `DatabentoDataLoader.from_dbn_file()` | Databento `.dbn.zst` files | Multiple Databento examples |
| `TardisCSVDataLoader.load_quotes()` | Tardis CSV quote data | `bitmex_grid_market_maker.py` |
| `BinanceOrderBookDeltaDataLoader.load()` | Binance L2 depth CSV | `crypto_orderbook_imbalance.py` |
| `BarDataWrangler.process(df)` | Custom CSV → Bar objects | Example 01 |
| `QuoteTickDataWrangler.process(df)` | DataFrame → QuoteTick | FX examples |
| `TradeTickDataWrangler.process(df)` | DataFrame → TradeTick | ETHUSDT examples |
| `QuoteTickDataWrangler.process_bar_data()` | Bid/ask bar CSVs → QuoteTick | GBPUSD examples |
| `PolymarketDataLoader.load_trades()` | Polymarket API → TradeTick | Polymarket example |

---

## 8. Backtest Configuration Options

### Fill Models

| Model | Description |
|-------|-------------|
| Default | Deterministic fills at market price |
| `FillModel` | Probabilistic: `prob_fill_on_limit`, `prob_slippage`, `random_seed` |
| `BestPriceFillModel` | Fills limit orders anywhere between bid/ask |
| `MakerTakerFeeModel` | Uses instrument's maker/taker fees |
| `FixedFeeModel` | Fixed commission per order (e.g., $1.50 USD) |
| `PerContractFeeModel` | Per-contract commission (e.g., $2.50 per contract) |

### Latency Models

| Model | Parameters |
|-------|-----------|
| `LatencyModel` | `base_latency_nanos`, `insert_latency_nanos`, `update_latency_nanos`, `cancel_latency_nanos` |

### Venue Configuration

| Parameter | Notes |
|-----------|-------|
| `oms_type` | `NETTING` (crypto/futures) or `HEDGING` (FX, generates position IDs) |
| `account_type` | `MARGIN` or `CASH` |
| `base_currency` | `None` for multi-currency, specific for single-currency |
| `book_type` | `L1_MBP`, `L2_MBP`, `L3_MBO` |
| `bar_execution` | If bar data moves the market (default True) |
| `trade_execution` | If trade tick data fills orders (use with L1_MBP) |
| `settlement_prices` | Dict of instrument_id → price for futures/options settlement at expiry |

### Tearsheet Generation

NT 1.225.0 includes interactive tearsheet generation (note: the project does NOT use this — `create_tearsheet()` is built on the broken returns analyzer; see `docs/ANALYZER_RETURNS_CAVEAT.md` and the v2 tearsheet helper `generate_v2_tearsheet` in `notebooks/charts.py` for the trustworthy replacement):

```python
from nautilus_trader.analysis import TearsheetConfig, create_tearsheet

config = TearsheetConfig(theme="plotly_white")  # or "plotly_dark", "nautilus", "nautilus_dark"
create_tearsheet(engine=engine, output_path="tearsheet.html", config=config)
```

Supports custom chart composition with `TearsheetStatsTableChart`, `TearsheetEquityChart`, `TearsheetBarsWithFillsChart`.
