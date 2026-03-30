"""Fetch Binance OHLCV candles into NautilusTrader's ParquetDataCatalog.

Supports both USDM Futures (perpetuals) and Spot markets via the --market flag.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from scripts._catalog import (
    CATALOG_PATH,
    candles_to_dataframe,
    get_catalog_range,
    merge_and_write,
    retry_request,
    validate_dataframe,
    wrangle_and_write,
)
from src.core.constants import (
    BINANCE_CANDLE_LIMIT,
    BINANCE_FUTURES_API_URL,
    BINANCE_SPOT_API_URL,
    BINANCE_TESTNET_API_URL,
    INTERVAL_TO_BAR_SPEC,
)
from src.core.instruments import make_binance_perp, make_binance_spot

# Rate limit threshold — back off when used weight approaches the 1200/min limit.
_WEIGHT_BACKOFF_THRESHOLD = 1000

# Default number of days when no mode is specified.
_DEFAULT_DAYS = 180


# ── Binance API ──────────────────────────────────────────────────────


def get_base_url(testnet: bool, market: str) -> str:
    """Return the appropriate Binance API base URL for the given market type."""
    if market == "spot":
        if testnet:
            print("ERROR: Binance Spot has no testnet API. Remove --testnet.", file=sys.stderr)
            sys.exit(1)
        return BINANCE_SPOT_API_URL
    return BINANCE_TESTNET_API_URL if testnet else BINANCE_FUTURES_API_URL


def fetch_instrument_metadata(
    client: httpx.Client,
    base_url: str,
    coins: list[str],
    market: str,
) -> dict[str, dict[str, str]]:
    """Fetch tick_size and step_size for each coin from Binance exchangeInfo.

    Returns a dict mapping coin → {"tick_size": str, "step_size": str}.
    Errors if a requested coin is not found.
    """
    if market == "spot":
        endpoint = f"{base_url}/api/v3/exchangeInfo"
    else:
        endpoint = f"{base_url}/fapi/v1/exchangeInfo"
    resp = retry_request(client, "GET", endpoint)
    data: dict[str, Any] = resp.json()

    symbols_by_base: dict[str, dict[str, Any]] = {}
    for s in data.get("symbols", []):
        if market == "spot":
            if (
                s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("isSpotTradingAllowed") is True
            ):
                symbols_by_base[s["baseAsset"]] = s
        else:
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT":
                symbols_by_base[s["baseAsset"]] = s

    result: dict[str, dict[str, str]] = {}
    for coin in coins:
        if coin not in symbols_by_base:
            kind = "spot pair" if market == "spot" else "perpetual"
            print(f"ERROR: {coin}USDT {kind} not found on Binance", file=sys.stderr)
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
    market: str,
) -> list[dict[str, Any]]:
    """Fetch all candles for a symbol/interval range, paginating as needed.

    Binance klines returns arrays: [open_time, open, high, low, close, volume, close_time, ...].
    We convert each to a dict for consistency with the shared candles_to_dataframe().
    """
    if market == "spot":
        klines_endpoint = f"{base_url}/api/v3/klines"
    else:
        klines_endpoint = f"{base_url}/fapi/v1/klines"

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
        resp = retry_request(client, "GET", klines_endpoint, params=params)

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


def _parse_mode(args: argparse.Namespace) -> str:
    """Resolve the fetch mode from CLI args. Exits on conflicts."""
    flags = []
    if args.backfill:
        flags.append("backfill")
    if args.update:
        flags.append("update")
    if args.start is not None:
        flags.append("start")
    if args.days is not None:
        flags.append("days")

    if len(flags) > 1:
        print(
            f"ERROR: --{' and --'.join(flags)} are mutually exclusive",
            file=sys.stderr,
        )
        sys.exit(1)

    if not flags:
        return "days"  # default
    return flags[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Binance OHLCV (Futures or Spot) to ParquetDataCatalog"
    )
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--intervals", nargs="+", default=["1h", "4h", "1d"])
    parser.add_argument("--days", type=int, default=None, help="Fetch last N days (default: 180)")
    parser.add_argument("--start", type=str, default=None, help="Explicit start date YYYY-MM-DD")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Extend data backwards to exchange's earliest available",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Extend data forwards from last bar to now",
    )
    parser.add_argument(
        "--market",
        choices=["perp", "spot"],
        default="perp",
        help="Market type: perp (USDM Futures, default) or spot",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance testnet API (no geo-block, perp only)",
    )
    args = parser.parse_args()

    mode = _parse_mode(args)
    days = args.days if args.days is not None else _DEFAULT_DAYS

    for interval in args.intervals:
        if interval not in INTERVAL_TO_BAR_SPEC:
            print(f"ERROR: Unknown interval '{interval}'", file=sys.stderr)
            sys.exit(1)

    market: str = args.market
    base_url = get_base_url(args.testnet, market)
    if args.testnet:
        print(f"Using Binance TESTNET: {base_url}")
    if market == "spot":
        print("Market: Binance Spot")

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    col_map = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}

    with httpx.Client(timeout=30.0) as client:
        # Fetch instrument metadata from exchange
        print("Fetching instrument metadata from Binance exchangeInfo...")
        try:
            metadata = fetch_instrument_metadata(client, base_url, args.coins, market)
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
            if market == "spot":
                inst = make_binance_spot(coin, meta["tick_size"], meta["step_size"])
            else:
                inst = make_binance_perp(coin, meta["tick_size"], meta["step_size"])
            instruments[coin] = inst
            catalog.write_data([inst])
            print(f"Wrote instrument: {inst.id}")

        for coin in args.coins:
            instrument = instruments[coin]
            symbol = f"{coin}USDT"
            for interval in args.intervals:
                step, aggregation = INTERVAL_TO_BAR_SPEC[interval]
                bar_type_str = f"{instrument.id}-{step}-{aggregation}-LAST-EXTERNAL"
                bar_type = BarType.from_str(bar_type_str)

                # ── Resolve fetch range based on mode ──
                catalog_range = get_catalog_range(catalog, bar_type_str)

                if mode == "backfill":
                    if not catalog_range:
                        print(
                            f"  {symbol} {interval}: no existing data — use --days first",
                            file=sys.stderr,
                        )
                        continue
                    fetch_start_ms = 0
                    fetch_end_ms = catalog_range[0]
                    label = f"backfill -> {datetime.fromtimestamp(fetch_end_ms / 1000, tz=UTC):%Y-%m-%d}"

                elif mode == "update":
                    if not catalog_range:
                        print(
                            f"  {symbol} {interval}: no existing data — use --days first",
                            file=sys.stderr,
                        )
                        continue
                    fetch_start_ms = catalog_range[1]
                    fetch_end_ms = now_ms
                    # Skip if already up to date (within 2x interval duration)
                    interval_ms = step * {"MINUTE": 60_000, "HOUR": 3_600_000, "DAY": 86_400_000}[aggregation]
                    if (fetch_end_ms - fetch_start_ms) < interval_ms * 2:
                        print(f"  {symbol} {interval}: already up to date, skipping")
                        continue
                    label = f"update {datetime.fromtimestamp(fetch_start_ms / 1000, tz=UTC):%Y-%m-%d} -> now"

                elif mode == "start":
                    fetch_start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
                    fetch_end_ms = now_ms
                    label = f"{args.start} -> now"

                else:  # days
                    fetch_start_ms = now_ms - days * 86_400_000
                    fetch_end_ms = now_ms
                    label = f"last {days} days"

                print(f"Fetching {symbol} {interval} ({label})...")
                candles = fetch_candles(client, base_url, symbol, interval, fetch_start_ms, fetch_end_ms, market)

                if not candles:
                    print(f"  No data returned for {symbol} {interval}", file=sys.stderr)
                    continue

                df = candles_to_dataframe(candles, ts_col="open_time", col_map=col_map)
                validate_dataframe(df, symbol, interval)

                # Merge with existing data if catalog has bars, otherwise write fresh
                if catalog_range:
                    bar_count = merge_and_write(df, instrument, interval, bar_type, bar_type_str, catalog)
                else:
                    bar_count = wrangle_and_write(df, instrument, interval, bar_type, catalog)
                    print(f"  Written {bar_count:,} bars, range: {df.index[0]} -> {df.index[-1]}")

                time.sleep(0.5)

    print(f"\nDone. Catalog at: {CATALOG_PATH.resolve()}")


if __name__ == "__main__":
    main()
