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

# Default instrument metadata: (price_precision, size_precision/szDecimals, maxLeverage)
# price_precision is derived from HL's 5-significant-figure rule based on price magnitude.
# szDecimals and maxLeverage fetched from HL meta endpoint (2026-03-03).
COIN_DEFAULTS: dict[str, tuple[int, int, int]] = {
    "BTC": (1, 5, 40),
    "ETH": (2, 4, 25),
    "SOL": (3, 2, 20),
}

# Column mapping: HL candleSnapshot keys → standard OHLCV names
_HL_COL_MAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


# ── Hyperliquid API ──────────────────────────────────────────────────


def fetch_meta(client: httpx.Client) -> dict[str, Any]:
    """Fetch asset metadata from Hyperliquid meta endpoint."""
    resp = retry_request(client, "POST", HYPERLIQUID_API_URL, json={"type": "meta"})
    data: dict[str, Any] = resp.json()
    return data


def validate_coin_metadata(meta: dict[str, Any], coins: list[str]) -> None:
    """Warn if fetched metadata differs from COIN_DEFAULTS."""
    universe = meta.get("universe", [])
    by_name = {item["name"]: item for item in universe}
    for coin in coins:
        if coin not in by_name:
            print(f"WARNING: {coin} not found in HL meta universe", file=sys.stderr)
            continue
        info = by_name[coin]
        defaults = COIN_DEFAULTS.get(coin)
        if defaults:
            _, sz_dec, max_lev = defaults
            if info["szDecimals"] != sz_dec:
                print(
                    f"WARNING: {coin} szDecimals mismatch: "
                    f"expected {sz_dec}, got {info['szDecimals']}",
                    file=sys.stderr,
                )
            if info["maxLeverage"] != max_lev:
                print(
                    f"WARNING: {coin} maxLeverage mismatch: "
                    f"expected {max_lev}, got {info['maxLeverage']}",
                    file=sys.stderr,
                )


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

    for coin in args.coins:
        if coin not in COIN_DEFAULTS:
            print(f"ERROR: No defaults for '{coin}'. Add to COIN_DEFAULTS.", file=sys.stderr)
            sys.exit(1)

    now = datetime.now(UTC)
    start_dt = now - timedelta(days=args.days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    with httpx.Client(timeout=30.0) as client:
        meta = fetch_meta(client)
        validate_coin_metadata(meta, args.coins)

        # Build instruments once per coin, write to catalog before bar data.
        instruments = {}
        for coin in args.coins:
            price_prec, size_prec, max_lev = COIN_DEFAULTS[coin]
            inst = make_hyperliquid_perp(coin, price_prec, size_prec, max_lev)
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
