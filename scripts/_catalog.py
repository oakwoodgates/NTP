"""Shared utilities for candle-fetching scripts (catalog write, validation, retry)."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from src.core.constants import INTERVAL_TO_BAR_SPEC, TS_INIT_DELTAS

if TYPE_CHECKING:
    from nautilus_trader.model.data import BarType

CATALOG_PATH = Path("data/catalog")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


# ── HTTP retry ──────────────────────────────────────────────────────


def retry_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with exponential backoff on transient errors.

    Retries on connection errors, timeouts, and HTTP 429/5xx responses.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUS_CODES:
                resp.raise_for_status()
                return resp
            # Retryable HTTP error
            if attempt < max_retries:
                wait = 2**attempt
                print(
                    f"  Retry {attempt + 1}/{max_retries}: HTTP {resp.status_code}, "
                    f"waiting {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                print(
                    f"  Retry {attempt + 1}/{max_retries}: {type(exc).__name__}, "
                    f"waiting {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise
    # Should not reach here, but satisfy type checker
    raise last_exc  # type: ignore[misc]


# ── DataFrame helpers ───────────────────────────────────────────────


def candles_to_dataframe(
    records: list[dict[str, Any]],
    ts_col: str,
    col_map: dict[str, str],
    ts_unit: str = "ms",
) -> pd.DataFrame:
    """Convert raw candle records to the OHLCV DataFrame that BarDataWrangler expects.

    Parameters
    ----------
    records : list[dict]
        Raw candle data (list of dicts with exchange-specific keys).
    ts_col : str
        Column name (after DataFrame creation) containing the timestamp.
    col_map : dict
        Mapping from exchange column names to standard names
        (must map to 'open', 'high', 'low', 'close', 'volume').
    ts_unit : str
        Pandas timestamp unit for the ts_col values (default "ms").

    Returns
    -------
    pd.DataFrame
        Columns ['open', 'high', 'low', 'close', 'volume'] with UTC DatetimeIndex.

    """
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df[ts_col], unit=ts_unit, utc=True)
    df = df.set_index("timestamp")
    df = df.rename(columns=col_map)
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)
    return df


def validate_dataframe(df: pd.DataFrame, symbol: str, interval: str) -> None:
    """Validate the DataFrame, warn on issues."""
    if not df.index.is_monotonic_increasing:
        print(f"WARNING: {symbol} {interval} timestamps not monotonic!", file=sys.stderr)

    zero_vol = (df["volume"] == 0).sum()
    if zero_vol > 0:
        print(
            f"WARNING: {symbol} {interval} has {zero_vol} zero-volume bars",
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
            f"WARNING: {symbol} {interval} has {len(gaps)} gaps > 2x interval:",
            file=sys.stderr,
        )
        for ts, gap in gaps.items():
            print(f"  {ts}: gap = {gap}", file=sys.stderr)


# ── Catalog write ───────────────────────────────────────────────────


_STAGING_DIR_NAME = "_staging"


def _recover_catalog_dir(bar_type_str: str, catalog_path: Path) -> None:
    """Clean up artifacts from a previous interrupted write.

    Must run before any destructive operation to ensure a consistent state.
    """
    bar_dir = catalog_path / "data" / "bar"
    staging_root = catalog_path / _STAGING_DIR_NAME
    real_dir = bar_dir / bar_type_str
    backup_dir = bar_dir / (bar_type_str + ".backup")

    # Clean incomplete staging from previous crash
    if staging_root.exists():
        shutil.rmtree(staging_root)
        print("  Recovered: cleaned up incomplete staging dir", file=sys.stderr)

    if not backup_dir.exists():
        return

    if real_dir.exists():
        # Both exist: previous write completed but cleanup didn't. Delete backup.
        shutil.rmtree(backup_dir)
        print(f"  Recovered: cleaned up orphaned backup for {bar_type_str}", file=sys.stderr)
    else:
        # Backup exists but real doesn't: crash during swap. Restore old data.
        # New data is lost — re-run the fetch to retry.
        os.rename(str(backup_dir), str(real_dir))
        print(
            f"  Recovered: restored {bar_type_str} from backup "
            f"(write was interrupted — re-run fetch to retry)",
            file=sys.stderr,
        )


def _safe_catalog_swap(
    bars: list[Any],
    bar_type_str: str,
    catalog_path: Path,
) -> None:
    """Write bars to a staging catalog, then swap into the real location.

    At every crash point, existing data is either intact or recoverable
    via ``_recover_catalog_dir`` on the next run.
    """
    bar_dir = catalog_path / "data" / "bar"
    real_dir = bar_dir / bar_type_str
    backup_dir = bar_dir / (bar_type_str + ".backup")
    staging_root = catalog_path / _STAGING_DIR_NAME

    # Defensive: clean leftover staging (should already be handled by recovery)
    if staging_root.exists():
        shutil.rmtree(staging_root)

    # 1. Write new data to a staging catalog
    staging_catalog = ParquetDataCatalog(str(staging_root))
    staging_catalog.write_data(bars)

    # 2. Verify staging produced a parquet file
    staged_bar_dir = staging_root / "data" / "bar" / bar_type_str
    staged_files = list(staged_bar_dir.glob("*.parquet")) if staged_bar_dir.exists() else []
    if not staged_files:
        shutil.rmtree(staging_root, ignore_errors=True)
        msg = f"Staging write produced no parquet files for {bar_type_str}"
        raise RuntimeError(msg)

    # 3. Backup existing data (if any)
    if real_dir.exists():
        os.rename(str(real_dir), str(backup_dir))

    # 4. Move staged data to real location
    os.rename(str(staged_bar_dir), str(real_dir))

    # 5. Cleanup
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    shutil.rmtree(staging_root, ignore_errors=True)


def wrangle_and_write(
    df: pd.DataFrame,
    instrument: Any,
    interval: str,
    bar_type: BarType,
    catalog: ParquetDataCatalog,  # noqa: ARG001 — kept for backward compat
    catalog_path: Path = CATALOG_PATH,
) -> int:
    """Wrangle DataFrame to NT Bars and write to catalog (crash-safe).

    Writes to a staging directory first, then swaps into the real location.
    If the process crashes at any point, existing data remains intact or is
    recoverable on the next run.
    """
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)

    ts_init_delta = TS_INIT_DELTAS[interval]
    bars = wrangler.process(df, ts_init_delta=ts_init_delta)

    # Spot check ts_event vs ts_init on first bar
    if bars:
        b = bars[0]
        ts_event = pd.Timestamp(b.ts_event, unit="ns", tz="UTC")
        ts_init = pd.Timestamp(b.ts_init, unit="ns", tz="UTC")
        print(f"  Spot check: ts_event={ts_event}, ts_init={ts_init}, delta={ts_init - ts_event}")

    bar_type_str = str(bar_type)
    _recover_catalog_dir(bar_type_str, catalog_path)
    _safe_catalog_swap(bars, bar_type_str, catalog_path)

    return len(bars)


# ── Catalog read + merge helpers ───────────────────────────────────


def bars_to_dataframe(bars: list[Any]) -> pd.DataFrame:
    """Convert NT Bar objects back to the OHLCV DataFrame that BarDataWrangler expects.

    Returns
    -------
    pd.DataFrame
        Columns ['open', 'high', 'low', 'close', 'volume'] with UTC DatetimeIndex.

    """
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    ts_index = pd.to_datetime(
        [bar.ts_event for bar in bars], unit="ns", utc=True,
    )
    df = pd.DataFrame(
        {
            "open": [float(bar.open) for bar in bars],
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [float(bar.volume) for bar in bars],
        },
        index=ts_index,
    )
    df.index.name = "timestamp"
    return df


def get_catalog_range(
    catalog: ParquetDataCatalog,
    bar_type_str: str,
) -> tuple[int, int] | None:
    """Return (first_ts_ms, last_ts_ms) of existing catalog data, or None if empty."""
    bars = catalog.bars(bar_types=[bar_type_str])
    if not bars:
        return None
    first_ms = bars[0].ts_event // 1_000_000
    last_ms = bars[-1].ts_event // 1_000_000
    return first_ms, last_ms


def merge_and_write(
    new_df: pd.DataFrame,
    instrument: Any,
    interval: str,
    bar_type: BarType,
    bar_type_str: str,
    catalog: ParquetDataCatalog,
    catalog_path: Path = CATALOG_PATH,
) -> int:
    """Merge new OHLCV data with existing catalog data and write the result.

    1. Read existing bars from catalog.
    2. Convert to DataFrame via ``bars_to_dataframe()``.
    3. Concatenate, deduplicate (fresh exchange data wins), sort.
    4. Validate and write via ``wrangle_and_write()``.

    Returns the bar count of the merged result.
    """
    existing_bars = catalog.bars(bar_types=[bar_type_str])

    if not existing_bars:
        print("  No existing data — writing fresh")
        return wrangle_and_write(new_df, instrument, interval, bar_type, catalog, catalog_path)

    existing_df = bars_to_dataframe(existing_bars)
    n_existing = len(existing_df)
    n_new = len(new_df)

    merged = pd.concat([existing_df, new_df])
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.sort_index()

    # Extract symbol from instrument id for validation messages
    symbol = str(instrument.id).split("-")[0]
    validate_dataframe(merged, symbol, interval)

    bar_count = wrangle_and_write(merged, instrument, interval, bar_type, catalog, catalog_path)
    print(f"  Merged {n_existing:,} existing + {n_new:,} new -> {bar_count:,} total bars")
    return bar_count
