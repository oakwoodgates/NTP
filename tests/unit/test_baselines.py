"""Unit tests for ``src.backtesting.baselines``."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from src.backtesting.baselines import buy_and_hold, random_entry_baseline

# ── Mock bar class to avoid NT imports ──────────────────────────────────────


@dataclass
class _MockClose:
    _v: float

    def as_double(self) -> float:
        return self._v


@dataclass
class _MockBar:
    ts_event: int
    close: _MockClose


_NS_PER_DAY = 86_400_000_000_000
_BASE_NS = 1_672_531_200_000_000_000  # 2023-01-01 UTC


def _make_bars(closes: list[float]) -> list[_MockBar]:
    """Daily bars with given close prices."""
    return [
        _MockBar(ts_event=_BASE_NS + i * _NS_PER_DAY, close=_MockClose(c))
        for i, c in enumerate(closes)
    ]


# ── buy_and_hold ─────────────────────────────────────────────────────────────


class TestBuyAndHoldBasic:
    def test_simple_50pct_gain_no_fees(self) -> None:
        # 1000 → 1500, frictionless, no leverage.
        bars = _make_bars([100.0] + [110.0] * 363 + [150.0])  # 365 bars total
        out = buy_and_hold(bars, starting_capital=1000.0)
        # Bought 10 @ 100, sold 10 @ 150 → +500
        assert out["pnl"] == pytest.approx(500.0)
        # 50% return on capital
        assert out["pnl_pct"] == pytest.approx(50.0)
        assert out["entry_price"] == 100.0
        assert out["exit_price"] == 150.0
        assert out["return_pct"] == pytest.approx(0.5)

    def test_50pct_loss(self) -> None:
        bars = _make_bars([100.0, 50.0])
        out = buy_and_hold(bars, starting_capital=1000.0)
        assert out["pnl"] == pytest.approx(-500.0)
        assert out["pnl_pct"] == pytest.approx(-50.0)

    def test_no_change(self) -> None:
        bars = _make_bars([100.0, 100.0, 100.0])
        out = buy_and_hold(bars, starting_capital=1000.0)
        assert out["pnl"] == pytest.approx(0.0)
        assert out["max_drawdown_pct"] == pytest.approx(0.0)


class TestBuyAndHoldFees:
    def test_fees_reduce_pnl(self) -> None:
        # 100 → 150 over 1 day; 0.05% taker each side
        bars = _make_bars([100.0, 150.0])
        out = buy_and_hold(bars, starting_capital=1000.0, fee_rate=0.0005)
        # qty=10. Fees: 0.0005 × (1000 + 1500) = 1.25
        # Gross pnl = 500. Net = 500 - 1.25 = 498.75
        assert out["pnl"] == pytest.approx(498.75)
        assert out["fees_paid"] == pytest.approx(1.25)


class TestBuyAndHoldLeverage:
    def test_2x_leverage_doubles_pnl(self) -> None:
        bars = _make_bars([100.0, 110.0])
        out_unlev = buy_and_hold(bars, starting_capital=1000.0)
        out_lev = buy_and_hold(bars, starting_capital=1000.0, leverage=2.0)
        assert out_lev["pnl"] == pytest.approx(2 * out_unlev["pnl"])


class TestBuyAndHoldCAGR:
    def test_one_year_50pct_gain(self) -> None:
        # Span ~1 year (365 daily bars); 100 → 150
        closes = [100.0 + i * 50.0 / 364 for i in range(365)]
        bars = _make_bars(closes)
        out = buy_and_hold(bars, starting_capital=1000.0)
        # CAGR ≈ 50% over ~1 year
        assert out["cagr"] == pytest.approx(0.5, rel=0.05)

    def test_negative_final_balance_returns_nan_cagr(self) -> None:
        # Bigger loss than starting capital with 10x leverage on a 50% drop
        bars = _make_bars([100.0, 50.0])
        out = buy_and_hold(bars, starting_capital=1000.0, leverage=10.0)
        # Final balance = 1000 - 5000 = -4000 → NaN CAGR
        assert math.isnan(out["cagr"])


class TestBuyAndHoldEdgeCases:
    def test_empty_bars_returns_nan(self) -> None:
        out = buy_and_hold([], starting_capital=1000.0)
        assert all(math.isnan(v) for v in out.values())

    def test_single_bar_returns_nan(self) -> None:
        bars = _make_bars([100.0])
        out = buy_and_hold(bars, starting_capital=1000.0)
        assert all(math.isnan(v) for v in out.values())


class TestBuyAndHoldDrawdown:
    def test_drawdown_tracks_underwater(self) -> None:
        # Up to 200, down to 80, back to 150
        bars = _make_bars([100.0, 200.0, 80.0, 150.0])
        out = buy_and_hold(bars, starting_capital=1000.0)
        # qty=10. Equity: 1000 → 2000 → 800 → 1500
        # Peak-to-trough: 2000 → 800 = 60% drawdown.
        assert out["max_drawdown_pct"] == pytest.approx(0.6, rel=0.01)


# ── random_entry_baseline ───────────────────────────────────────────────────


class TestRandomEntryBasic:
    def test_random_entry_runs(self) -> None:
        bars = _make_bars([100.0 + i for i in range(100)])
        out = random_entry_baseline(
            bars,
            n_trades=10,
            avg_duration_bars=5,
            starting_capital=1000.0,
            notional_per_trade=100.0,
            n_simulations=50,
            seed=1,
        )
        # On a monotonic uptrend, random long entries should mostly profit
        assert out["mean_pnl"] > 0

    def test_returns_required_keys(self) -> None:
        bars = _make_bars([100.0, 105.0, 110.0, 102.0])
        out = random_entry_baseline(
            bars, n_trades=2, avg_duration_bars=2,
            starting_capital=1000.0, notional_per_trade=100.0,
            n_simulations=10, seed=1,
        )
        expected = {
            "mean_pnl", "median_pnl", "std_pnl",
            "pct_5", "pct_25", "pct_75", "pct_95",
            "min_pnl", "max_pnl", "win_rate_mean",
            "n_simulations", "trades_per_sim",
        }
        assert expected.issubset(out.keys())

    def test_seed_determinism(self) -> None:
        bars = _make_bars([100.0 + i for i in range(50)])
        a = random_entry_baseline(
            bars, n_trades=5, avg_duration_bars=3,
            starting_capital=1000.0, notional_per_trade=100.0,
            n_simulations=30, seed=42,
        )
        b = random_entry_baseline(
            bars, n_trades=5, avg_duration_bars=3,
            starting_capital=1000.0, notional_per_trade=100.0,
            n_simulations=30, seed=42,
        )
        assert a["mean_pnl"] == b["mean_pnl"]
        assert a["pct_95"] == b["pct_95"]

    def test_percentiles_ordered(self) -> None:
        bars = _make_bars([100.0 + (i % 10) for i in range(50)])
        out = random_entry_baseline(
            bars, n_trades=5, avg_duration_bars=3,
            starting_capital=1000.0, notional_per_trade=100.0,
            n_simulations=200, seed=1,
        )
        assert out["pct_5"] <= out["pct_25"] <= out["median_pnl"]
        assert out["median_pnl"] <= out["pct_75"] <= out["pct_95"]


class TestRandomEntryEdgeCases:
    def test_empty_bars(self) -> None:
        out = random_entry_baseline(
            [], n_trades=10, avg_duration_bars=5,
            starting_capital=1000.0, notional_per_trade=100.0,
        )
        assert all(math.isnan(v) for v in out.values())

    def test_zero_trades(self) -> None:
        bars = _make_bars([100.0, 110.0])
        out = random_entry_baseline(
            bars, n_trades=0, avg_duration_bars=5,
            starting_capital=1000.0, notional_per_trade=100.0,
        )
        assert all(math.isnan(v) for v in out.values())


class TestRandomEntryFees:
    def test_fees_reduce_mean_pnl(self) -> None:
        bars = _make_bars([100.0 + i * 0.1 for i in range(50)])
        no_fee = random_entry_baseline(
            bars, n_trades=10, avg_duration_bars=5,
            starting_capital=1000.0, notional_per_trade=100.0,
            n_simulations=100, seed=1,
        )
        with_fee = random_entry_baseline(
            bars, n_trades=10, avg_duration_bars=5,
            starting_capital=1000.0, notional_per_trade=100.0,
            fee_rate=0.001,  # 10 bps round trip
            n_simulations=100, seed=1,
        )
        assert with_fee["mean_pnl"] < no_fee["mean_pnl"]
