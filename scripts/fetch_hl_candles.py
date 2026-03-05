"""Fetch OHLCV candles from Hyperliquid API and write to ParquetDataCatalog."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from src.core.constants import (
    CANDLE_LIMIT,
    HYPERLIQUID_API_URL,
    INTERVAL_TO_BAR_SPEC,
    TS_INIT_DELTAS,
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

CATALOG_PATH = Path("data/catalog")


# ── Hyperliquid API ──────────────────────────────────────────────────


def fetch_meta(client: httpx.Client) -> dict:
    """Fetch asset metadata from Hyperliquid meta endpoint."""
    resp = client.post(HYPERLIQUID_API_URL, json={"type": "meta"})
    resp.raise_for_status()
    return resp.json()


def validate_coin_metadata(meta: dict, coins: list[str]) -> None:
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
) -> list[dict]:
    """Fetch all candles for a coin/interval range, paginating as needed."""
    all_candles: list[dict] = []
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
        resp = client.post(HYPERLIQUID_API_URL, json=payload)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        all_candles.extend(batch)

        # Advance cursor past the last candle's close time
        last_close_ms = batch[-1]["T"]
        if last_close_ms <= cursor_ms:
            break  # Safety: avoid infinite loop
        cursor_ms = last_close_ms + 1

        if len(batch) < CANDLE_LIMIT:
            break  # No more data available

        time.sleep(0.5)

    return all_candles


# ── Data transformation ──────────────────────────────────────────────


def candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert HL candle response to the DataFrame format BarDataWrangler expects.

    Required: columns ['open', 'high', 'low', 'close', 'volume']
              with UTC DatetimeIndex named 'timestamp'.
    """
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)
    return df


def validate_dataframe(df: pd.DataFrame, coin: str, interval: str) -> None:
    """Validate the DataFrame, warn on issues."""
    if not df.index.is_monotonic_increasing:
        print(f"WARNING: {coin} {interval} timestamps not monotonic!", file=sys.stderr)

    zero_vol = (df["volume"] == 0).sum()
    if zero_vol > 0:
        print(
            f"WARNING: {coin} {interval} has {zero_vol} zero-volume bars",
            file=sys.stderr,
        )

    # Check for gaps > 2x interval
    step, agg = INTERVAL_TO_BAR_SPEC[interval]
    if agg == "HOUR":
        expected_delta = pd.Timedelta(hours=step)
    elif agg == "MINUTE":
        expected_delta = pd.Timedelta(minutes=step)
    elif agg == "DAY":
        expected_delta = pd.Timedelta(days=step)
    else:
        return

    diffs = df.index.to_series().diff().dropna()
    gaps = diffs[diffs > expected_delta * 2]
    if len(gaps) > 0:
        print(
            f"WARNING: {coin} {interval} has {len(gaps)} gaps > 2x interval:",
            file=sys.stderr,
        )
        for ts, gap in gaps.items():
            print(f"  {ts}: gap = {gap}", file=sys.stderr)


# ── Catalog write ────────────────────────────────────────────────────


def clean_catalog_data(bar_type_str: str) -> None:
    """Delete existing catalog data for this bar type to allow clean rewrite.

    ParquetDataCatalog.write_data() with overlapping time ranges produces
    duplicate bars. Wipe the relevant parquet directory before writing.

    NT's catalog directory naming has changed across versions, so we glob
    for any directory containing the bar type string to be safe.
    """
    data_dir = CATALOG_PATH / "data"
    if not data_dir.exists():
        return

    # NT may use "bar-{bar_type_str}" or "bar_{bar_type_str}" or other
    # separators depending on version. Glob broadly, match narrowly.
    for path in data_dir.iterdir():
        if path.is_dir() and bar_type_str in path.name:
            shutil.rmtree(path)
            print(f"  Cleaned: {path.name}")


def wrangle_and_write(
    df: pd.DataFrame,
    instrument,  # CryptoPerpetual — avoid importing the type in a script
    interval: str,
    bar_type: BarType,
    catalog: ParquetDataCatalog,
) -> int:
    """Wrangle DataFrame to NT Bars and write to catalog. Returns bar count."""
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)

    ts_init_delta = TS_INIT_DELTAS[interval]
    bars = wrangler.process(df, ts_init_delta=ts_init_delta)

    # Spot check ts_event vs ts_init on first bar
    if bars:
        b = bars[0]
        ts_event = pd.Timestamp(b.ts_event, unit="ns", tz="UTC")
        ts_init = pd.Timestamp(b.ts_init, unit="ns", tz="UTC")
        print(f"  Spot check: ts_event={ts_event}, ts_init={ts_init}, delta={ts_init - ts_event}")

    # Wipe existing data to avoid duplicate bars, then write fresh
    clean_catalog_data(str(bar_type))
    catalog.write_data(bars)

    return len(bars)


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
        # The old version wrote the instrument inside wrangle_and_write(),
        # causing it to be written N times (once per interval per coin).
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

                df = candles_to_dataframe(candles)
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
