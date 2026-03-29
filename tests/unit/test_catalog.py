"""Unit tests for scripts._catalog helpers (bars_to_dataframe, get_catalog_range, merge logic)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pandas as pd

from scripts._catalog import (
    bars_to_dataframe,
    candles_to_dataframe,
    validate_dataframe,
)

if TYPE_CHECKING:
    import pytest


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
