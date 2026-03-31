"""Unit tests for src.backtesting.analysis."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.backtesting.analysis import (
    _compute_window_stats,
    _positions_to_pnl_df,
    performance_by_regime,
    rolling_performance,
    run_fee_sweep,
    tag_regimes,
)

# ── Mock objects (avoid NT imports) ──────────────────────────────────────────

_NS_PER_HOUR = 3_600_000_000_000
_NS_PER_DAY = 86_400_000_000_000

# Base timestamp: 2023-01-01 00:00 UTC in nanoseconds
_BASE_NS = 1_672_531_200_000_000_000


@dataclass
class MockMoney:
    _value: Decimal

    def as_decimal(self) -> Decimal:
        return self._value


@dataclass
class MockPosition:
    ts_opened: int
    ts_closed: int
    realized_pnl: MockMoney
    duration_ns: int


@dataclass
class MockBar:
    ts_event: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _make_pos(day_opened: int, day_closed: int, pnl: str) -> MockPosition:
    """Create a MockPosition opened/closed on day offsets from _BASE_NS."""
    opened = _BASE_NS + day_opened * _NS_PER_DAY
    closed = _BASE_NS + day_closed * _NS_PER_DAY
    return MockPosition(
        ts_opened=opened,
        ts_closed=closed,
        realized_pnl=MockMoney(Decimal(pnl)),
        duration_ns=closed - opened,
    )


def _make_bars(n_days: int, base_close: float = 100.0) -> list[MockBar]:
    """Create n_days of mock bars starting from _BASE_NS."""
    bars = []
    for i in range(n_days):
        c = base_close + i * 0.5
        bars.append(MockBar(
            ts_event=_BASE_NS + i * _NS_PER_DAY,
            open=c - 1,
            high=c + 2,
            low=c - 2,
            close=c,
            volume=1000.0,
        ))
    return bars


# ── _positions_to_pnl_df ────────────────────────────────────────────────────


class TestPositionsToPnlDf:
    def test_empty_list_returns_empty_df(self) -> None:
        df = _positions_to_pnl_df([])
        assert df.empty
        assert list(df.columns) == ["ts_opened", "ts_closed", "pnl", "duration_ns"]

    def test_single_position(self) -> None:
        pos = _make_pos(0, 1, "100.50")
        df = _positions_to_pnl_df([pos])
        assert len(df) == 1
        assert df.iloc[0]["pnl"] == Decimal("100.50")

    def test_sorted_by_ts_opened(self) -> None:
        p1 = _make_pos(5, 6, "10")
        p2 = _make_pos(1, 2, "20")
        df = _positions_to_pnl_df([p1, p2])
        assert df.iloc[0]["pnl"] == Decimal("20")
        assert df.iloc[1]["pnl"] == Decimal("10")

    def test_timestamps_are_utc(self) -> None:
        pos = _make_pos(0, 1, "50")
        df = _positions_to_pnl_df([pos])
        assert str(df.iloc[0]["ts_opened"].tz) == "UTC"


# ── _compute_window_stats ────────────────────────────────────────────────────


class TestComputeWindowStats:
    def test_empty_pnls(self) -> None:
        stats = _compute_window_stats([], 10_000)
        assert stats["num_positions"] == 0
        assert stats["pnl"] == 0.0

    def test_all_winners(self) -> None:
        pnls = [Decimal("100"), Decimal("200")]
        stats = _compute_window_stats(pnls, 10_000)
        assert stats["win_rate"] == 1.0
        assert stats["avg_loser"] == 0.0
        assert np.isnan(stats["profit_factor"])  # no losers

    def test_all_losers(self) -> None:
        pnls = [Decimal("-50"), Decimal("-100")]
        stats = _compute_window_stats(pnls, 10_000)
        assert stats["win_rate"] == 0.0
        assert stats["avg_winner"] == 0.0

    def test_mixed(self) -> None:
        pnls = [Decimal("200"), Decimal("-100")]
        stats = _compute_window_stats(pnls, 10_000)
        assert stats["pnl"] == 100.0
        assert stats["pnl_pct"] == pytest.approx(1.0)
        assert stats["win_rate"] == 0.5
        assert stats["profit_factor"] == pytest.approx(2.0)


# ── rolling_performance ──────────────────────────────────────────────────────


class TestRollingPerformance:
    def test_empty_positions(self) -> None:
        bars = _make_bars(180)
        df = rolling_performance([], bars, window="90D")
        assert not df.empty
        assert (df["num_positions"] == 0).all()
        assert (df["pnl"] == 0.0).all()

    def test_window_columns(self) -> None:
        bars = _make_bars(180)
        positions = [_make_pos(10, 12, "50")]
        df = rolling_performance(positions, bars, window="90D")
        expected_cols = {
            "window_start", "window_end", "pnl", "pnl_pct",
            "num_positions", "win_rate", "avg_winner", "avg_loser",
            "profit_factor", "max_drawdown_pct",
        }
        assert set(df.columns) == expected_cols

    def test_position_counted_in_correct_window(self) -> None:
        bars = _make_bars(200)
        # Position opens on day 10 — should be in the first window
        positions = [_make_pos(10, 12, "100")]
        df = rolling_performance(positions, bars, window="90D", step="90D")
        # First window should have the position
        assert df.iloc[0]["num_positions"] == 1
        assert df.iloc[0]["pnl"] == 100.0

    def test_non_overlapping_no_double_count(self) -> None:
        bars = _make_bars(200)
        positions = [_make_pos(10, 12, "100")]
        df = rolling_performance(positions, bars, window="90D", step="90D")
        assert df["num_positions"].sum() == 1

    def test_max_drawdown_calculation(self) -> None:
        bars = _make_bars(100)
        # Win then lose: cumulative goes +200, then +100 → drawdown = 100
        positions = [
            _make_pos(5, 6, "200"),
            _make_pos(10, 11, "-100"),
        ]
        df = rolling_performance(positions, bars, window="90D", step="90D",
                                 starting_capital=10_000)
        # Drawdown = 100 / 10_000 * 100 = 1.0%
        assert df.iloc[0]["max_drawdown_pct"] == pytest.approx(1.0)

    def test_concentrated_pnl_detection(self) -> None:
        bars = _make_bars(365)
        # All profit in one quarter
        positions = [
            _make_pos(10, 12, "500"),
            _make_pos(20, 22, "500"),
            _make_pos(200, 202, "-50"),
        ]
        df = rolling_performance(positions, bars, window="90D", step="90D")
        # At least one window should have most of the PnL
        max_window_pnl = df["pnl"].max()
        total_pnl = df["pnl"].sum()
        assert max_window_pnl / total_pnl > 0.9  # concentrated


# ── tag_regimes ──────────────────────────────────────────────────────────────


class TestTagRegimes:
    def test_adx_returns_correct_columns(self) -> None:
        bars = _make_bars(100)
        df = tag_regimes(bars, method="adx")
        assert list(df.columns) == ["close", "indicator_value", "regime"]

    def test_adx_regime_labels(self) -> None:
        bars = _make_bars(100)
        df = tag_regimes(bars, method="adx")
        valid_labels = {"TRENDING", "RANGING", "TRANSITIONAL"}
        non_null = df["regime"].dropna().unique()
        assert set(non_null).issubset(valid_labels)

    def test_adx_warmup_is_nan(self) -> None:
        bars = _make_bars(100)
        df = tag_regimes(bars, method="adx", adx_period=14)
        # First ~adx_period bars should have NaN indicator and NaN regime
        assert df["indicator_value"].iloc[0] != df["indicator_value"].iloc[0]  # NaN check

    def test_volatility_returns_correct_columns(self) -> None:
        bars = _make_bars(100)
        df = tag_regimes(bars, method="volatility")
        assert list(df.columns) == ["close", "indicator_value", "regime"]

    def test_volatility_regime_labels(self) -> None:
        bars = _make_bars(100)
        df = tag_regimes(bars, method="volatility")
        valid_labels = {"HIGH_VOL", "LOW_VOL"}
        non_null = df["regime"].dropna().unique()
        assert set(non_null).issubset(valid_labels)

    def test_invalid_method_raises(self) -> None:
        bars = _make_bars(50)
        with pytest.raises(ValueError, match="Unknown method"):
            tag_regimes(bars, method="invalid")

    def test_index_is_datetime(self) -> None:
        bars = _make_bars(50)
        df = tag_regimes(bars, method="adx")
        assert isinstance(df.index, pd.DatetimeIndex)


# ── performance_by_regime ────────────────────────────────────────────────────


class TestPerformanceByRegime:
    def _make_regime_df(self) -> pd.DataFrame:
        """Create a simple regime DataFrame: first 50 days TRENDING, next 50 RANGING."""
        idx = pd.date_range(
            pd.Timestamp(_BASE_NS, unit="ns", tz="UTC"),
            periods=100, freq="D",
        )
        regime = ["TRENDING"] * 50 + ["RANGING"] * 50
        return pd.DataFrame(
            {"close": 100.0, "indicator_value": 30.0, "regime": regime},
            index=idx,
        )

    def test_empty_positions(self) -> None:
        regime_df = self._make_regime_df()
        df = performance_by_regime([], regime_df)
        assert df.empty

    def test_correct_regime_assignment(self) -> None:
        regime_df = self._make_regime_df()
        # Position in TRENDING period (day 10), position in RANGING period (day 60)
        positions = [
            _make_pos(10, 12, "100"),
            _make_pos(60, 62, "-50"),
        ]
        df = performance_by_regime(positions, regime_df)
        assert len(df) == 2

        trending = df[df["regime"] == "TRENDING"].iloc[0]
        assert trending["pnl"] == 100.0
        assert trending["num_positions"] == 1

        ranging = df[df["regime"] == "RANGING"].iloc[0]
        assert ranging["pnl"] == -50.0
        assert ranging["num_positions"] == 1

    def test_output_columns(self) -> None:
        regime_df = self._make_regime_df()
        positions = [_make_pos(10, 12, "100")]
        df = performance_by_regime(positions, regime_df)
        expected_cols = {
            "regime", "pnl", "pnl_pct", "num_positions", "win_rate",
            "avg_winner", "avg_loser", "profit_factor", "avg_duration",
        }
        assert set(df.columns) == expected_cols

    def test_sorted_by_num_positions(self) -> None:
        regime_df = self._make_regime_df()
        positions = [
            _make_pos(10, 12, "50"),
            _make_pos(60, 62, "50"),
            _make_pos(65, 67, "50"),
        ]
        df = performance_by_regime(positions, regime_df)
        assert df.iloc[0]["regime"] == "RANGING"  # 2 positions
        assert df.iloc[1]["regime"] == "TRENDING"  # 1 position


# ── run_fee_sweep ────────────────────────────────────────────────────────────


class TestRunFeeSweep:
    def _mock_backtest_result(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "total_pnl": 500.0,
            "total_pnl_pct": 5.0,
            "num_positions": 50,
            "final_balance": 10_500.0,
            "min_balance": 9_800.0,
            "error": "",
        }
        base.update(overrides)
        return base

    @patch("src.backtesting.engine.run_single_backtest")
    @patch("src.core.instruments.with_venue_config")
    def test_output_columns(
        self,
        mock_venue_config: Any,
        mock_backtest: Any,
    ) -> None:
        mock_venue_config.return_value = "mock_instrument"
        mock_backtest.return_value = self._mock_backtest_result()

        df = run_fee_sweep(
            venue="VENUE",
            instrument=_make_mock_instrument(),
            bars=[],
            starting_capital=10_000,
            params={"fast": 10},
            strategy_factory=lambda eng, p: None,
            verbose=False,
        )
        expected_cols = {
            "fee_bps", "fee_rate", "total_pnl", "total_pnl_pct",
            "num_positions", "final_balance", "pnl_per_trade", "breakeven",
        }
        assert set(df.columns) == expected_cols

    @patch("src.backtesting.engine.run_single_backtest")
    @patch("src.core.instruments.with_venue_config")
    def test_default_fee_levels(
        self,
        mock_venue_config: Any,
        mock_backtest: Any,
    ) -> None:
        mock_venue_config.return_value = "mock_instrument"
        mock_backtest.return_value = self._mock_backtest_result()

        df = run_fee_sweep(
            venue="VENUE",
            instrument=_make_mock_instrument(),
            bars=[],
            starting_capital=10_000,
            params={},
            strategy_factory=lambda eng, p: None,
            verbose=False,
        )
        assert len(df) == 8  # default [1, 2, 2.5, 3, 4, 5, 7.5, 10]

    @patch("src.backtesting.engine.run_single_backtest")
    @patch("src.core.instruments.with_venue_config")
    def test_fee_rate_computation(
        self,
        mock_venue_config: Any,
        mock_backtest: Any,
    ) -> None:
        mock_venue_config.return_value = "mock_instrument"
        mock_backtest.return_value = self._mock_backtest_result()

        df = run_fee_sweep(
            venue="VENUE",
            instrument=_make_mock_instrument(),
            bars=[],
            starting_capital=10_000,
            params={},
            strategy_factory=lambda eng, p: None,
            fee_levels_bps=[5.0],
            verbose=False,
        )
        assert df.iloc[0]["fee_rate"] == pytest.approx(0.0005)

    @patch("src.backtesting.engine.run_single_backtest")
    @patch("src.core.instruments.with_venue_config")
    def test_breakeven_flag(
        self,
        mock_venue_config: Any,
        mock_backtest: Any,
    ) -> None:
        mock_venue_config.return_value = "mock_instrument"
        # First level profitable, second not
        mock_backtest.side_effect = [
            self._mock_backtest_result(total_pnl=100.0),
            self._mock_backtest_result(total_pnl=-50.0),
        ]

        df = run_fee_sweep(
            venue="VENUE",
            instrument=_make_mock_instrument(),
            bars=[],
            starting_capital=10_000,
            params={},
            strategy_factory=lambda eng, p: None,
            fee_levels_bps=[2.0, 10.0],
            verbose=False,
        )
        assert df.iloc[0]["breakeven"] == True  # noqa: E712
        assert df.iloc[1]["breakeven"] == False  # noqa: E712

    @patch("src.backtesting.engine.run_single_backtest")
    @patch("src.core.instruments.with_venue_config")
    def test_pnl_per_trade(
        self,
        mock_venue_config: Any,
        mock_backtest: Any,
    ) -> None:
        mock_venue_config.return_value = "mock_instrument"
        mock_backtest.return_value = self._mock_backtest_result(
            total_pnl=500.0, num_positions=50,
        )

        df = run_fee_sweep(
            venue="VENUE",
            instrument=_make_mock_instrument(),
            bars=[],
            starting_capital=10_000,
            params={},
            strategy_factory=lambda eng, p: None,
            fee_levels_bps=[5.0],
            verbose=False,
        )
        assert df.iloc[0]["pnl_per_trade"] == pytest.approx(10.0)


# ── Helper for mock instrument ───────────────────────────────────────────────


@dataclass
class _MockInstrument:
    margin_init: Decimal = Decimal("0.05")  # 20x leverage


def _make_mock_instrument() -> Any:
    return _MockInstrument()
