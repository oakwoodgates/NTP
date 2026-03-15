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
    HL_CANDLE_LIMIT,
    HYPERLIQUID_API_URL,
    INTERVAL_TO_BAR_SPEC,
)
from src.core.instruments import make_hyperliquid_perp

# Column mapping: HL candleSnapshot keys → standard OHLCV names
_HL_COL_MAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


# ── Hyperliquid API ──────────────────────────────────────────────────


def fetch_instrument_metadata(
    client: httpx.Client,
    coins: list[str],
) -> dict[str, dict[str, int]]:
    """Fetch szDecimals and maxLeverage for each coin from HL meta endpoint.

    Returns a dict mapping coin → {"sz_decimals": int, "max_leverage": int}.
    Errors if a requested coin is not found.
    """
    resp = retry_request(client, "POST", HYPERLIQUID_API_URL, json={"type": "meta"})
    data: dict[str, Any] = resp.json()

    universe = data.get("universe", [])
    by_name: dict[str, dict[str, Any]] = {item["name"]: item for item in universe}

    result: dict[str, dict[str, int]] = {}
    for coin in coins:
        if coin not in by_name:
            print(f"ERROR: {coin} not found in HL meta universe", file=sys.stderr)
            sys.exit(1)

        info = by_name[coin]
        result[coin] = {
            "sz_decimals": info["szDecimals"],
            "max_leverage": info["maxLeverage"],
        }
        print(f"  {coin}: szDecimals={info['szDecimals']}, maxLeverage={info['maxLeverage']}")

    return result


def infer_price_precision(client: httpx.Client, coin: str) -> int:
    """Infer price precision from recent candle price strings.

    HL uses a 5-significant-figure rule for prices. The tick size is
    price-magnitude-dependent and not exposed by any API endpoint.
    We infer it from the actual price strings in recent candle data.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = now_ms - 86_400_000  # 1 day ago

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": now_ms},
    }
    resp = retry_request(client, "POST", HYPERLIQUID_API_URL, json=payload)
    candles: list[dict[str, Any]] = resp.json()

    if not candles:
        print(f"ERROR: No recent candle data for {coin} to infer price precision", file=sys.stderr)
        sys.exit(1)

    # Examine OHLC price strings from recent candles to find max decimal places.
    # Do NOT strip trailing zeros — "70677.0" means precision 1 (tick = 0.1).
    max_decimals = 0
    for candle in candles[-5:]:  # Last few candles
        for key in ("o", "h", "l", "c"):
            price_str: str = candle[key]
            if "." in price_str:
                decimals = len(price_str.split(".")[1])
                max_decimals = max(max_decimals, decimals)

    return max_decimals


def fetch_candles(
    client: httpx.Client,
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Fetch all candles for a coin/interval range, paginating as needed."""
    all_candles: list[dict[str, Any]] = []
    cursor_ms = start_ms

    while cursor_ms < end_ms:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": cursor_ms,
                "endTime": end_ms,
            },
        }
        resp = retry_request(client, "POST", HYPERLIQUID_API_URL, json=payload)
        batch: list[dict[str, Any]] = resp.json()

        if not batch:
            break

        all_candles.extend(batch)

        # Advance cursor past the last candle's close time
        last_close_ms = batch[-1]["T"]
        if last_close_ms <= cursor_ms:
            break  # Safety: avoid infinite loop
        cursor_ms = last_close_ms + 1

        if len(batch) < HL_CANDLE_LIMIT:
            break  # No more data available

        time.sleep(0.5)

    return all_candles


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Hyperliquid OHLCV to ParquetDataCatalog")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--intervals", nargs="+", default=["1h", "4h", "1d"])
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    for interval in args.intervals:
        if interval not in INTERVAL_TO_BAR_SPEC:
            print(f"ERROR: Unknown interval '{interval}'", file=sys.stderr)
            sys.exit(1)

    now = datetime.now(UTC)
    start_dt = now - timedelta(days=args.days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    with httpx.Client(timeout=30.0) as client:
        # Fetch instrument metadata from HL meta endpoint
        print("Fetching instrument metadata from HL meta endpoint...")
        metadata = fetch_instrument_metadata(client, args.coins)

        # Infer price precision from recent candle data
        print("Inferring price precision from recent candles...")
        instruments = {}
        for coin in args.coins:
            price_prec = infer_price_precision(client, coin)
            meta = metadata[coin]
            print(f"  {coin}: price_precision={price_prec}")

            inst = make_hyperliquid_perp(
                coin, price_prec, meta["sz_decimals"], meta["max_leverage"],
            )
            instruments[coin] = inst
            catalog.write_data([inst])
            print(f"Wrote instrument: {inst.id}")

        for coin in args.coins:
            instrument = instruments[coin]
            for interval in args.intervals:
                print(f"Fetching {coin} {interval} ({args.days} days)...")
                candles = fetch_candles(client, coin, interval, start_ms, end_ms)

                if not candles:
                    print(f"  No data returned for {coin} {interval}", file=sys.stderr)
                    continue

                df = candles_to_dataframe(candles, ts_col="t", col_map=_HL_COL_MAP)
                validate_dataframe(df, coin, interval)

                step, aggregation = INTERVAL_TO_BAR_SPEC[interval]
                bar_type_str = f"{instrument.id}-{step}-{aggregation}-LAST-EXTERNAL"
                bar_type = BarType.from_str(bar_type_str)

                bar_count = wrangle_and_write(df, instrument, interval, bar_type, catalog)
                print(f"  Written {bar_count} bars, range: {df.index[0]} -> {df.index[-1]}")

                time.sleep(0.5)

    print(f"\nDone. Catalog at: {CATALOG_PATH.resolve()}")


if __name__ == "__main__":
    main()
