# Data Fetching Guide

Fetch OHLCV candlestick data from exchanges and write it to NautilusTrader's `ParquetDataCatalog` for backtesting.

## Overview

Each exchange has a dedicated fetch script in `scripts/`. They share common utilities via `scripts/_catalog.py` (retry logic, DataFrame validation, catalog writing). All scripts write to the same catalog at `data/catalog/`.

**Pipeline:** Exchange REST API → OHLCV DataFrame → `BarDataWrangler` → NT `Bar` objects → `ParquetDataCatalog`

## Hyperliquid

```bash
# Default: BTC, ETH, SOL — 1h, 4h, 1d — 180 days
python scripts/fetch_hl_candles.py

# Custom
python scripts/fetch_hl_candles.py --coins BTC ETH --intervals 1h --days 90

# Backfill — extend history to exchange's earliest available
python scripts/fetch_hl_candles.py --backfill --coins BTC --intervals 1h 4h 1d

# Update — extend data from last bar to now
python scripts/fetch_hl_candles.py --update

# Explicit start date
python scripts/fetch_hl_candles.py --start 2021-01-01 --coins BTC --intervals 1h
```

| Arg | Default | Description |
|-----|---------|-------------|
| `--coins` | `BTC ETH SOL` | Coin tickers |
| `--intervals` | `1h 4h 1d` | Candle intervals |
| `--days` | `180` | Lookback period (default if no mode specified) |
| `--backfill` | off | Extend data backwards to exchange's earliest available |
| `--update` | off | Extend data forwards from last bar to now |
| `--start` | — | Explicit start date (`YYYY-MM-DD`) to now |

`--days`, `--backfill`, `--update`, and `--start` are mutually exclusive. If none specified, defaults to `--days 180`.

**API:** POST `https://api.hyperliquid.xyz/info` — no auth required, max 5000 candles/request.

## Binance (USDM Futures)

```bash
# Default: BTC, ETH, SOL — 1h, 4h, 1d — 180 days
python scripts/fetch_binance_candles.py

# Testnet (no geo-block)
python scripts/fetch_binance_candles.py --testnet

# Custom
python scripts/fetch_binance_candles.py --coins BTC --intervals 1h 4h --days 90

# Backfill — extend history to exchange's earliest available (Binance has ~9 years for BTC)
python scripts/fetch_binance_candles.py --backfill --coins BTC --intervals 1h 4h 1d

# Update — extend data from last bar to now
python scripts/fetch_binance_candles.py --update

# Explicit start date
python scripts/fetch_binance_candles.py --start 2019-09-01 --coins BTC --intervals 1h
```

| Arg | Default | Description |
|-----|---------|-------------|
| `--coins` | `BTC ETH SOL` | Coin tickers |
| `--intervals` | `1h 4h 1d` | Candle intervals |
| `--days` | `180` | Lookback period (default if no mode specified) |
| `--backfill` | off | Extend data backwards to exchange's earliest available |
| `--update` | off | Extend data forwards from last bar to now |
| `--start` | — | Explicit start date (`YYYY-MM-DD`) to now |
| `--testnet` | off | Use Binance testnet API (no geo-restrictions) |

`--days`, `--backfill`, `--update`, and `--start` are mutually exclusive. If none specified, defaults to `--days 180`.

**API:** GET `https://fapi.binance.com/fapi/v1/klines` — no auth required, max 1500 candles/request, weight-based rate limiting (1200/min).

### VPN / Geo-Block

Binance blocks API access from some regions. If you get a connection error:

```bash
# Connect NordVPN first
nordvpn connect

# Then run the script
python scripts/fetch_binance_candles.py
```

Or use `--testnet` for development — the testnet has no geo-restrictions.

## Supported Coins

Both scripts fetch instrument metadata at runtime — no hardcoded precision values.

- **Hyperliquid:** `szDecimals` and `maxLeverage` from the meta endpoint. Price precision inferred from recent candle price strings (HL uses a 5-significant-figure rule that's price-magnitude-dependent).
- **Binance:** `tickSize` and `stepSize` from `exchangeInfo` PRICE_FILTER and LOT_SIZE filters.

Any perpetual available on either exchange can be fetched by passing its ticker via `--coins`.

## Supported Intervals

`1m`, `5m`, `15m`, `1h`, `4h`, `1d`

Both exchanges use identical interval strings. The mapping to NT `BarType` components is in `src/core/constants.py` (`INTERVAL_TO_BAR_SPEC`).

## Catalog Output

Data lands in `data/catalog/` (gitignored). After fetching:

```
data/catalog/
├── data/
│   ├── bar/
│   │   ├── BTC-USD-PERP.HYPERLIQUID-1-HOUR-LAST-EXTERNAL/
│   │   ├── BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL/
│   │   └── ...
│   └── crypto_perpetual/
│       ├── HYPERLIQUID/
│       └── BINANCE/
```

**Merge-on-write.** All modes merge new data with existing catalog data. Fresh exchange data wins on timestamp collisions (deduplication keeps the latest value). The underlying write still cleans + rewrites the full bar type directory, but data is never silently lost. Safe to re-run at any time.

**Instrument IDs:**
- Hyperliquid: `BTC-USD-PERP.HYPERLIQUID`
- Binance: `BTCUSDT-PERP.BINANCE`

## Adding a New Coin

**Both exchanges:** Just pass the coin ticker via `--coins`. All instrument metadata is fetched at runtime — no hardcoded defaults to maintain.

## Adding a New Exchange

Follow the existing pattern:

1. **Constants** — add venue URL, candle limit, and fee constants to `src/core/constants.py`.
2. **Instrument factory** — add a `make_<exchange>_perp()` function to `src/core/instruments.py`. Use NT's built-in venue constant if available (e.g., `BINANCE_VENUE` from `nautilus_trader.adapters.binance.common.constants`).
3. **Fetch script** — create `scripts/fetch_<exchange>_candles.py`. Import shared utilities from `scripts/_catalog.py`:
   - `retry_request()` — HTTP calls with exponential backoff
   - `candles_to_dataframe()` — convert raw data to OHLCV DataFrame (pass exchange-specific column mapping)
   - `validate_dataframe()` — gap detection, zero-volume warnings
   - `wrangle_and_write()` — BarDataWrangler + catalog write with ts_init_delta correction
4. **Export** — add new constants/functions to `src/core/__init__.py`.

## Shared Utilities (`scripts/_catalog.py`)

| Function | Purpose |
|----------|---------|
| `retry_request()` | HTTP request with exponential backoff (retries on 429, 5xx, timeouts) |
| `candles_to_dataframe()` | Generic OHLCV DataFrame builder — takes column mapping dict |
| `validate_dataframe()` | Warns on non-monotonic timestamps, zero-volume bars, gaps > 2x interval |
| `clean_catalog_data()` | Deletes existing parquet dirs for a bar type before rewrite |
| `wrangle_and_write()` | BarDataWrangler + ts_init_delta shift + catalog write |
| `bars_to_dataframe()` | Convert NT Bar objects back to OHLCV DataFrame (for merge workflow) |
| `get_catalog_range()` | Return `(first_ts_ms, last_ts_ms)` of existing catalog data, or `None` |
| `merge_and_write()` | Read existing bars, merge with new data, dedup, validate, write |

`CATALOG_PATH` is also defined here (`data/catalog`).

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Binance connection error | Connect NordVPN (`nordvpn connect`) or use `--testnet` |
| Binance rate limit (HTTP 429) | Script auto-retries with backoff. If persistent, reduce `--coins` or wait. |
| `WARNING: timestamps not monotonic` | Exchange returned out-of-order data. Usually harmless — NT sorts internally. |
| `WARNING: X gaps > 2x interval` | Missing candles. Normal for low-liquidity coins/periods. Check if the gap is a market closure or data issue. |
| Duplicate bars in backtest | Re-run the fetch script — it merges and deduplicates automatically. |
| `--backfill` or `--update` says "no existing data" | Run with `--days` first to seed initial data, then backfill/update. |
