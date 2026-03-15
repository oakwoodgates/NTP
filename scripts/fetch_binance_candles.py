"""Fetch Binance USDM Futures OHLCV candles into NautilusTrader's ParquetDataCatalog."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from scripts._catalog import (
    CATALOG_PATH,
    candles_to_dataframe,
    retry_request,
    validate_dataframe,
    wrangle_and_write,
)
from src.core.constants import (
    BINANCE_CANDLE_LIMIT,
    BINANCE_FUTURES_API_URL,
    BINANCE_TESTNET_API_URL,
    INTERVAL_TO_BAR_SPEC,
)
from src.core.instruments import make_binance_perp

# Rate limit threshold — back off when used weight approaches the 1200/min limit.
_WEIGHT_BACKOFF_THRESHOLD = 1000


# ── Binance API ──────────────────────────────────────────────────────


def get_base_url(testnet: bool) -> str:
    """Return the appropriate Binance Futures API base URL."""
    return BINANCE_TESTNET_API_URL if testnet else BINANCE_FUTURES_API_URL


def fetch_instrument_metadata(
    client: httpx.Client,
    base_url: str,
    coins: list[str],
) -> dict[str, dict[str, str]]:
    """Fetch tick_size and step_size for each coin from Binance exchangeInfo.

    Returns a dict mapping coin → {"tick_size": str, "step_size": str}.
    Errors if a requested coin is not found.
    """
    resp = retry_request(client, "GET", f"{base_url}/fapi/v1/exchangeInfo")
    data: dict[str, Any] = resp.json()

    symbols_by_base: dict[str, dict[str, Any]] = {}
    for s in data.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT":
            symbols_by_base[s["baseAsset"]] = s

    result: dict[str, dict[str, str]] = {}
    for coin in coins:
        if coin not in symbols_by_base:
            print(f"ERROR: {coin}USDT perpetual not found on Binance", file=sys.stderr)
            sys.exit(1)

        info = symbols_by_base[coin]
        tick_size: str | None = None
        step_size: str | None = None

        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick_size = f["tickSize"]
            elif f["filterType"] == "LOT_SIZE":
                step_size = f["stepSize"]

        if not tick_size or not step_size:
            print(
                f"ERROR: Missing PRICE_FILTER or LOT_SIZE for {coin}USDT",
                file=sys.stderr,
            )
            sys.exit(1)

        result[coin] = {"tick_size": tick_size, "step_size": step_size}
        print(f"  {coin}: tick_size={tick_size}, step_size={step_size}")

    return result


def fetch_candles(
    client: httpx.Client,
    base_url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Fetch all candles for a symbol/interval range, paginating as needed.

    Binance klines returns arrays: [open_time, open, high, low, close, volume, close_time, ...].
    We convert each to a dict for consistency with the shared candles_to_dataframe().
    """
    all_candles: list[dict[str, Any]] = []
    cursor_ms = start_ms

    while cursor_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": BINANCE_CANDLE_LIMIT,
        }
        resp = retry_request(client, "GET", f"{base_url}/fapi/v1/klines", params=params)

        # Check rate limit weight
        used_weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1m", "0"))
        if used_weight > _WEIGHT_BACKOFF_THRESHOLD:
            wait = 10
            print(
                f"  Rate limit approaching ({used_weight}/1200 weight), "
                f"waiting {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)

        batch_raw: list[list[Any]] = resp.json()

        if not batch_raw:
            break

        # Convert array-of-arrays to list-of-dicts
        for row in batch_raw:
            all_candles.append({
                "open_time": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "close_time": row[6],
            })

        # Advance cursor past the last candle's close time
        last_close_ms: int = batch_raw[-1][6]
        if last_close_ms <= cursor_ms:
            break  # Safety: avoid infinite loop
        cursor_ms = last_close_ms + 1

        if len(batch_raw) < BINANCE_CANDLE_LIMIT:
            break  # No more data available

        time.sleep(0.5)

    return all_candles


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Binance USDM Futures OHLCV to ParquetDataCatalog"
    )
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--intervals", nargs="+", default=["1h", "4h", "1d"])
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance testnet API (no geo-block)",
    )
    args = parser.parse_args()

    for interval in args.intervals:
        if interval not in INTERVAL_TO_BAR_SPEC:
            print(f"ERROR: Unknown interval '{interval}'", file=sys.stderr)
            sys.exit(1)

    base_url = get_base_url(args.testnet)
    if args.testnet:
        print(f"Using Binance TESTNET: {base_url}")

    now = datetime.now(UTC)
    start_dt = now - timedelta(days=args.days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    col_map = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}

    with httpx.Client(timeout=30.0) as client:
        # Fetch instrument metadata from exchange
        print("Fetching instrument metadata from Binance exchangeInfo...")
        try:
            metadata = fetch_instrument_metadata(client, base_url, args.coins)
        except (httpx.ConnectError, httpx.TimeoutException):
            print(
                "ERROR: Cannot reach Binance API.\n"
                "If you're in a restricted region, connect NordVPN first:\n"
                "  nordvpn connect\n"
                "Or use --testnet to fetch from the Binance testnet (no geo-block).",
                file=sys.stderr,
            )
            sys.exit(1)

        # Build instruments from live metadata, write to catalog before bar data.
        instruments = {}
        for coin in args.coins:
            meta = metadata[coin]
            inst = make_binance_perp(coin, meta["tick_size"], meta["step_size"])
            instruments[coin] = inst
            catalog.write_data([inst])
            print(f"Wrote instrument: {inst.id}")

        for coin in args.coins:
            instrument = instruments[coin]
            symbol = f"{coin}USDT"
            for interval in args.intervals:
                print(f"Fetching {symbol} {interval} ({args.days} days)...")
                candles = fetch_candles(client, base_url, symbol, interval, start_ms, end_ms)

                if not candles:
                    print(f"  No data returned for {symbol} {interval}", file=sys.stderr)
                    continue

                df = candles_to_dataframe(candles, ts_col="open_time", col_map=col_map)
                validate_dataframe(df, symbol, interval)

                step, aggregation = INTERVAL_TO_BAR_SPEC[interval]
                bar_type_str = f"{instrument.id}-{step}-{aggregation}-LAST-EXTERNAL"
                bar_type = BarType.from_str(bar_type_str)

                bar_count = wrangle_and_write(df, instrument, interval, bar_type, catalog)
                print(f"  Written {bar_count} bars, range: {df.index[0]} -> {df.index[-1]}")

                time.sleep(0.5)

    print(f"\nDone. Catalog at: {CATALOG_PATH.resolve()}")


if __name__ == "__main__":
    main()
