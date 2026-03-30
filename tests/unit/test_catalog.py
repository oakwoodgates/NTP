"""Unit tests for scripts._catalog helpers (bars_to_dataframe, get_catalog_range, merge logic)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts._catalog import (
    _recover_catalog_dir,
    _safe_catalog_swap,
    bars_to_dataframe,
    candles_to_dataframe,
    validate_dataframe,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── Fake bar objects ─────────────────────────────────────────────────


def _make_fake_bar(
    ts_event_ns: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> MagicMock:
    """Create a mock NT Bar with the attributes that bars_to_dataframe reads."""
    bar = MagicMock()
    bar.ts_event = ts_event_ns
    bar.open = MagicMock(__float__=lambda _: open_)
    bar.high = MagicMock(__float__=lambda _: high)
    bar.low = MagicMock(__float__=lambda _: low)
    bar.close = MagicMock(__float__=lambda _: close)
    bar.volume = MagicMock(__float__=lambda _: volume)
    return bar


# ── bars_to_dataframe ────────────────────────────────────────────────


class TestBarsToDataframe:
    def test_empty_list_returns_empty_df(self) -> None:
        df = bars_to_dataframe([])
        assert df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_single_bar(self) -> None:
        # 2024-01-01 00:00:00 UTC in nanoseconds
        ts_ns = 1_704_067_200_000_000_000
        bars = [_make_fake_bar(ts_ns, 100.0, 105.0, 95.0, 102.0, 500.0)]
        df = bars_to_dataframe(bars)

        assert len(df) == 1
        assert df.index.name == "timestamp"
        assert df.iloc[0]["open"] == 100.0
        assert df.iloc[0]["high"] == 105.0
        assert df.iloc[0]["low"] == 95.0
        assert df.iloc[0]["close"] == 102.0
        assert df.iloc[0]["volume"] == 500.0

    def test_multiple_bars_preserves_order(self) -> None:
        ts1 = 1_704_067_200_000_000_000  # 2024-01-01 00:00
        ts2 = 1_704_070_800_000_000_000  # 2024-01-01 01:00
        ts3 = 1_704_074_400_000_000_000  # 2024-01-01 02:00
        bars = [
            _make_fake_bar(ts1, 100.0, 105.0, 95.0, 102.0, 500.0),
            _make_fake_bar(ts2, 102.0, 108.0, 100.0, 106.0, 600.0),
            _make_fake_bar(ts3, 106.0, 110.0, 104.0, 109.0, 700.0),
        ]
        df = bars_to_dataframe(bars)

        assert len(df) == 3
        assert df.iloc[0]["open"] == 100.0
        assert df.iloc[2]["close"] == 109.0

    def test_index_is_utc_datetime(self) -> None:
        ts_ns = 1_704_067_200_000_000_000
        bars = [_make_fake_bar(ts_ns, 100.0, 105.0, 95.0, 102.0, 500.0)]
        df = bars_to_dataframe(bars)

        assert df.index.tz is not None
        assert str(df.index.tz) == "UTC"

    def test_columns_are_float(self) -> None:
        ts_ns = 1_704_067_200_000_000_000
        bars = [_make_fake_bar(ts_ns, 100.0, 105.0, 95.0, 102.0, 500.0)]
        df = bars_to_dataframe(bars)

        for col in ["open", "high", "low", "close", "volume"]:
            assert df[col].dtype == float


# ── candles_to_dataframe ─────────────────────────────────────────────


class TestCandlesToDataframe:
    def test_basic_conversion(self) -> None:
        records = [
            {"t": 1_704_067_200_000, "o": "100", "h": "105", "l": "95", "c": "102", "v": "500"},
            {"t": 1_704_070_800_000, "o": "102", "h": "108", "l": "100", "c": "106", "v": "600"},
        ]
        col_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        df = candles_to_dataframe(records, ts_col="t", col_map=col_map)

        assert len(df) == 2
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.name == "timestamp"
        assert df.iloc[0]["open"] == 100.0

    def test_binance_style_columns(self) -> None:
        records = [
            {"open_time": 1_704_067_200_000, "open": "100", "high": "105",
             "low": "95", "close": "102", "volume": "500"},
        ]
        col_map = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
        df = candles_to_dataframe(records, ts_col="open_time", col_map=col_map)

        assert len(df) == 1
        assert df.iloc[0]["close"] == 102.0


# ── validate_dataframe ───────────────────────────────────────────────


class TestValidateDataframe:
    def _make_hourly_df(self, n: int = 5) -> pd.DataFrame:
        """Create a clean hourly OHLCV DataFrame."""
        idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        return pd.DataFrame(
            {
                "open": [100.0] * n,
                "high": [105.0] * n,
                "low": [95.0] * n,
                "close": [102.0] * n,
                "volume": [500.0] * n,
            },
            index=idx,
        )

    def test_clean_df_no_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        df = self._make_hourly_df()
        validate_dataframe(df, "BTC", "1h")
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err

    def test_warns_on_zero_volume(self, capsys: pytest.CaptureFixture[str]) -> None:
        df = self._make_hourly_df()
        df.iloc[2, df.columns.get_loc("volume")] = 0.0
        validate_dataframe(df, "BTC", "1h")
        captured = capsys.readouterr()
        assert "zero-volume" in captured.err

    def test_warns_on_gap(self, capsys: pytest.CaptureFixture[str]) -> None:
        idx = pd.to_datetime(
            ["2024-01-01 00:00", "2024-01-01 01:00", "2024-01-01 05:00"],
            utc=True,
        )
        df = pd.DataFrame(
            {
                "open": [100.0, 101.0, 102.0],
                "high": [105.0, 106.0, 107.0],
                "low": [95.0, 96.0, 97.0],
                "close": [102.0, 103.0, 104.0],
                "volume": [500.0, 600.0, 700.0],
            },
            index=idx,
        )
        validate_dataframe(df, "BTC", "1h")
        captured = capsys.readouterr()
        assert "gaps" in captured.err


# ── Crash-safe write helpers ────────────────────────────────────────

_BAR_TYPE = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"


def _make_catalog_dirs(catalog_path: Path) -> Path:
    """Create the data/bar directory structure and return bar_dir."""
    bar_dir = catalog_path / "data" / "bar"
    bar_dir.mkdir(parents=True, exist_ok=True)
    return bar_dir


def _populate_dir(d: Path) -> None:
    """Create a directory with a dummy parquet file inside."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "dummy.parquet").write_text("old data")


class TestRecoverCatalogDir:
    def test_cleans_orphaned_staging(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        staging = tmp_path / "_staging"
        staging.mkdir()
        (staging / "leftover").write_text("junk")

        # Real data should be untouched
        _populate_dir(bar_dir / _BAR_TYPE)

        _recover_catalog_dir(_BAR_TYPE, tmp_path)

        assert not staging.exists()
        assert (bar_dir / _BAR_TYPE).exists()

    def test_cleans_orphaned_backup(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        _populate_dir(bar_dir / _BAR_TYPE)
        _populate_dir(bar_dir / (_BAR_TYPE + ".backup"))

        _recover_catalog_dir(_BAR_TYPE, tmp_path)

        assert (bar_dir / _BAR_TYPE).exists()
        assert not (bar_dir / (_BAR_TYPE + ".backup")).exists()

    def test_restores_backup_when_real_missing(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        backup = bar_dir / (_BAR_TYPE + ".backup")
        _populate_dir(backup)

        _recover_catalog_dir(_BAR_TYPE, tmp_path)

        real_dir = bar_dir / _BAR_TYPE
        assert real_dir.exists()
        assert (real_dir / "dummy.parquet").read_text() == "old data"
        assert not backup.exists()

    def test_noop_when_no_artifacts(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        _populate_dir(bar_dir / _BAR_TYPE)

        _recover_catalog_dir(_BAR_TYPE, tmp_path)

        assert (bar_dir / _BAR_TYPE).exists()


def _mock_write_data(staging_root: Path, bar_type_str: str) -> Any:
    """Return a side_effect callable that creates a fake parquet in the staging catalog."""
    def side_effect(bars: list[Any]) -> None:  # noqa: ARG001
        staged = staging_root / "data" / "bar" / bar_type_str
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "2024-01-01_2024-02-01.parquet").write_text("new data")
    return side_effect


class TestSafeCatalogSwap:
    def test_fresh_write(self, tmp_path: Path) -> None:
        _make_catalog_dirs(tmp_path)
        staging_root = tmp_path / "_staging"

        with patch("scripts._catalog.ParquetDataCatalog") as mock_cls:
            mock_cls.return_value.write_data = MagicMock(
                side_effect=_mock_write_data(staging_root, _BAR_TYPE),
            )
            _safe_catalog_swap(["fake_bar"], _BAR_TYPE, tmp_path)

        real_dir = tmp_path / "data" / "bar" / _BAR_TYPE
        assert real_dir.exists()
        assert any(real_dir.glob("*.parquet"))
        # No artifacts left
        assert not staging_root.exists()
        assert not (tmp_path / "data" / "bar" / (_BAR_TYPE + ".backup")).exists()

    def test_replaces_existing(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        _populate_dir(bar_dir / _BAR_TYPE)
        staging_root = tmp_path / "_staging"

        with patch("scripts._catalog.ParquetDataCatalog") as mock_cls:
            mock_cls.return_value.write_data = MagicMock(
                side_effect=_mock_write_data(staging_root, _BAR_TYPE),
            )
            _safe_catalog_swap(["fake_bar"], _BAR_TYPE, tmp_path)

        real_dir = bar_dir / _BAR_TYPE
        assert real_dir.exists()
        # New data replaced old
        parquet_files = list(real_dir.glob("*.parquet"))
        assert len(parquet_files) == 1
        assert parquet_files[0].read_text() == "new data"
        # No artifacts
        assert not staging_root.exists()
        assert not (bar_dir / (_BAR_TYPE + ".backup")).exists()

    def test_aborts_on_failed_staging(self, tmp_path: Path) -> None:
        bar_dir = _make_catalog_dirs(tmp_path)
        _populate_dir(bar_dir / _BAR_TYPE)

        with patch("scripts._catalog.ParquetDataCatalog") as mock_cls:
            # write_data does nothing → no parquet produced
            mock_cls.return_value.write_data = MagicMock()
            with pytest.raises(RuntimeError, match="no parquet files"):
                _safe_catalog_swap(["fake_bar"], _BAR_TYPE, tmp_path)

        # Old data is intact
        real_dir = bar_dir / _BAR_TYPE
        assert real_dir.exists()
        assert (real_dir / "dummy.parquet").read_text() == "old data"
