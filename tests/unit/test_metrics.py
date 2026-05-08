"""Unit tests for ``src.backtesting.metrics``.

The metrics module is pure-Python — no NT engine spin-up here.  Tests
exercise the math directly by constructing ``TradeRecord`` lists and
``pd.Series`` balance curves with known properties.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.backtesting.metrics import (
    TradeRecord,
    bootstrap_max_drawdown,
    bootstrap_total_pnl,
    compute_activity_metrics,
    compute_all_metrics,
    compute_balance_metrics,
    compute_drawdown_periods,
    compute_trade_metrics,
)

# 1 day in nanoseconds — used as a canonical bar interval
NS_PER_DAY = 86_400_000_000_000


def _trade(pnl: float, side: str = "LONG", day_open: int = 0, day_close: int = 1) -> TradeRecord:
    """Build a TradeRecord from day offsets for terseness."""
    return TradeRecord(
        pnl=pnl,
        ts_opened_ns=day_open * NS_PER_DAY,
        ts_closed_ns=day_close * NS_PER_DAY,
        side=side,
    )


# ── compute_trade_metrics ─────────────────────────────────────────────────────


class TestTradeMetricsEmpty:
    def test_empty_returns_all_nan(self) -> None:
        out = compute_trade_metrics([])
        assert all(math.isnan(v) for v in out.values())
        # Schema check: all expected keys present.
        expected_keys = {
            "avg_pnl_per_trade", "win_rate", "loss_rate",
            "num_winners", "num_losers", "num_breakeven",
            "gross_wins", "gross_losses", "avg_win", "avg_loss",
            "largest_win", "largest_loss", "pnl_profit_factor",
            "expectancy", "payoff_ratio", "avg_trade_duration_secs",
            "max_consec_losers", "max_consec_winners",
            "num_long", "num_short", "long_pnl", "short_pnl",
        }
        assert set(out.keys()) == expected_keys


class TestTradeMetricsBasic:
    """Three winners + two losers, hand-checked numbers."""

    @pytest.fixture
    def out(self) -> dict[str, float]:
        trades = [
            _trade(100.0, "LONG"),
            _trade(50.0, "SHORT"),
            _trade(-30.0, "LONG"),
            _trade(200.0, "LONG"),
            _trade(-20.0, "SHORT"),
        ]
        return compute_trade_metrics(trades, bar_interval_ns=NS_PER_DAY)

    def test_avg_pnl(self, out: dict[str, float]) -> None:
        # Total = 100 + 50 - 30 + 200 - 20 = 300; / 5 = 60
        assert out["avg_pnl_per_trade"] == 60.0

    def test_win_loss_counts(self, out: dict[str, float]) -> None:
        assert out["num_winners"] == 3.0
        assert out["num_losers"] == 2.0
        assert out["num_breakeven"] == 0.0
        assert out["win_rate"] == 0.6
        assert out["loss_rate"] == 0.4

    def test_gross_amounts(self, out: dict[str, float]) -> None:
        assert out["gross_wins"] == 350.0  # 100 + 50 + 200
        assert out["gross_losses"] == -50.0  # -30 + -20
        # Profit factor: 350 / 50 = 7.0
        assert out["pnl_profit_factor"] == 7.0

    def test_average_win_loss(self, out: dict[str, float]) -> None:
        assert out["avg_win"] == pytest.approx(350.0 / 3)
        assert out["avg_loss"] == -25.0
        # Payoff = avg_win / abs(avg_loss) = (350/3) / 25
        assert out["payoff_ratio"] == pytest.approx((350.0 / 3) / 25)

    def test_extremes(self, out: dict[str, float]) -> None:
        assert out["largest_win"] == 200.0
        assert out["largest_loss"] == -30.0

    def test_expectancy_matches_avg_pnl(self, out: dict[str, float]) -> None:
        # By construction, expectancy = avg_pnl_per_trade.
        assert out["expectancy"] == pytest.approx(out["avg_pnl_per_trade"])

    def test_long_short_attribution(self, out: dict[str, float]) -> None:
        assert out["num_long"] == 3.0
        assert out["num_short"] == 2.0
        assert out["long_pnl"] == 270.0  # 100 - 30 + 200
        assert out["short_pnl"] == 30.0  # 50 - 20

    def test_avg_duration(self, out: dict[str, float]) -> None:
        # All trades are 1 bar — avg should be 1.
        assert out["avg_trade_duration_bars"] == pytest.approx(1.0)


class TestTradeMetricsConsecutive:
    def test_max_consecutive_losers(self) -> None:
        # Win, Loss, Loss, Loss, Win, Loss, Win — max losers = 3
        trades = [
            _trade(10.0, day_open=0, day_close=1),
            _trade(-5.0, day_open=1, day_close=2),
            _trade(-5.0, day_open=2, day_close=3),
            _trade(-5.0, day_open=3, day_close=4),
            _trade(10.0, day_open=4, day_close=5),
            _trade(-5.0, day_open=5, day_close=6),
            _trade(10.0, day_open=6, day_close=7),
        ]
        out = compute_trade_metrics(trades)
        assert out["max_consec_losers"] == 3.0
        assert out["max_consec_winners"] == 1.0

    def test_max_consecutive_winners(self) -> None:
        trades = [
            _trade(-1.0, day_open=0, day_close=1),
            _trade(1.0, day_open=1, day_close=2),
            _trade(2.0, day_open=2, day_close=3),
            _trade(3.0, day_open=3, day_close=4),
            _trade(4.0, day_open=4, day_close=5),
            _trade(-1.0, day_open=5, day_close=6),
        ]
        out = compute_trade_metrics(trades)
        assert out["max_consec_winners"] == 4.0
        assert out["max_consec_losers"] == 1.0


class TestTradeMetricsEdgeCases:
    def test_all_winners(self) -> None:
        trades = [_trade(10.0), _trade(20.0), _trade(30.0)]
        out = compute_trade_metrics(trades)
        assert out["num_losers"] == 0.0
        assert out["pnl_profit_factor"] == float("inf")
        assert math.isnan(out["payoff_ratio"])
        assert math.isnan(out["avg_loss"])

    def test_all_losers(self) -> None:
        trades = [_trade(-10.0), _trade(-5.0), _trade(-15.0)]
        out = compute_trade_metrics(trades)
        assert out["num_winners"] == 0.0
        # gross_wins=0, gross_losses=-30 → PF = 0/30 = 0.
        assert out["pnl_profit_factor"] == 0.0
        # No winners, so payoff is undefined.
        assert math.isnan(out["payoff_ratio"])

    def test_breakeven_only(self) -> None:
        trades = [_trade(0.0), _trade(0.0)]
        out = compute_trade_metrics(trades)
        assert out["num_winners"] == 0.0
        assert out["num_losers"] == 0.0
        assert out["num_breakeven"] == 2.0
        assert math.isnan(out["pnl_profit_factor"])  # 0 wins, 0 losses
        assert out["avg_pnl_per_trade"] == 0.0

    def test_duration_in_seconds_when_no_bar_interval(self) -> None:
        trades = [_trade(10.0, day_open=0, day_close=1)]
        out = compute_trade_metrics(trades, bar_interval_ns=None)
        # 1 day = 86400 secs
        assert out["avg_trade_duration_secs"] == pytest.approx(86400.0)


# ── compute_balance_metrics ───────────────────────────────────────────────────


class TestBalanceMetricsEmpty:
    def test_empty_returns_all_nan(self) -> None:
        s = pd.Series(dtype=float)
        out = compute_balance_metrics(s, 1000.0)
        assert all(math.isnan(v) for v in out.values())


class TestBalanceMetricsDrawdown:
    def test_simple_drawdown(self) -> None:
        # Balance: 1000 -> 1100 (peak) -> 900 (trough = -200, -18.18%) -> 1200
        idx = pd.to_datetime(
            ["2025-01-01", "2025-01-15", "2025-02-01", "2025-02-15"], utc=True,
        )
        s = pd.Series([1000.0, 1100.0, 900.0, 1200.0], index=idx)
        out = compute_balance_metrics(s, starting_capital=1000.0)
        assert out["max_drawdown_abs"] == pytest.approx(200.0)
        # 200 / 1100 = 0.1818...
        assert out["max_drawdown_pct"] == pytest.approx(200.0 / 1100.0)

    def test_no_drawdown_monotonic(self) -> None:
        idx = pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"], utc=True)
        s = pd.Series([1000.0, 1100.0, 1200.0], index=idx)
        out = compute_balance_metrics(s, starting_capital=1000.0)
        assert out["max_drawdown_abs"] == 0.0
        # Pct: drawdown/peak where peak > 0 -> all zeros
        assert out["max_drawdown_pct"] == 0.0

    def test_recovery_factor(self) -> None:
        idx = pd.to_datetime(["2025-01-01", "2025-06-01", "2025-12-01"], utc=True)
        s = pd.Series([1000.0, 800.0, 1300.0], index=idx)  # MaxDD=200, total_pnl=300
        out = compute_balance_metrics(s, starting_capital=1000.0)
        # 300 / 200 = 1.5
        assert out["recovery_factor"] == pytest.approx(1.5)


class TestBalanceMetricsCAGR:
    def test_cagr_one_year(self) -> None:
        # 1000 -> 1500 over exactly 1 year ≈ 50%
        idx = pd.to_datetime(["2025-01-01", "2026-01-01"], utc=True)
        s = pd.Series([1000.0, 1500.0], index=idx)
        out = compute_balance_metrics(s, starting_capital=1000.0)
        # Span is exactly 365 days, but years uses 365.25 → slightly different
        expected_cagr = (1500.0 / 1000.0) ** (365.25 / 365.0) - 1.0
        assert out["cagr"] == pytest.approx(expected_cagr, rel=1e-3)

    def test_mar_ratio(self) -> None:
        # 1000 -> 1100 -> 800 -> 1500 over 1 year
        idx = pd.to_datetime(
            ["2025-01-01", "2025-04-01", "2025-08-01", "2026-01-01"], utc=True,
        )
        s = pd.Series([1000.0, 1100.0, 800.0, 1500.0], index=idx)
        out = compute_balance_metrics(s, starting_capital=1000.0)
        # MaxDD%: (1100 - 800) / 1100 = 0.2727
        # CAGR ≈ 50%
        # MAR = 0.5 / 0.2727 ≈ 1.83
        assert out["mar_ratio"] == pytest.approx(out["cagr"] / out["max_drawdown_pct"])
        assert out["mar_ratio"] > 1.5  # sanity bound

    def test_cagr_negative_final_returns_nan(self) -> None:
        # Account blown up: final balance ≤ 0 means CAGR is undefined.
        idx = pd.to_datetime(["2025-01-01", "2026-01-01"], utc=True)
        s = pd.Series([1000.0, 0.0], index=idx)
        out = compute_balance_metrics(s, starting_capital=1000.0)
        assert math.isnan(out["cagr"])
        assert math.isnan(out["mar_ratio"])


class TestBalanceMetricsTimeUnderwater:
    def test_underwater_in_bars(self) -> None:
        # Spend 2 days underwater between days 1 and 3.
        idx = pd.to_datetime(
            ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"], utc=True,
        )
        s = pd.Series([1000.0, 1100.0, 900.0, 1200.0], index=idx)
        out = compute_balance_metrics(
            s, starting_capital=1000.0, bar_interval_ns=NS_PER_DAY,
        )
        # idx[1]=1100 (peak), idx[2]=900 (underwater), idx[3]=1200 (recovery, peak again)
        # Underwater point is idx[2]; gap from idx[1] to idx[2] is 1 day = 1 bar
        assert out["time_underwater_bars"] == pytest.approx(1.0)

    def test_no_underwater_when_monotonic(self) -> None:
        idx = pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"], utc=True)
        s = pd.Series([1000.0, 1100.0, 1200.0], index=idx)
        out = compute_balance_metrics(
            s, starting_capital=1000.0, bar_interval_ns=NS_PER_DAY,
        )
        assert out["time_underwater_bars"] == 0.0


# ── compute_activity_metrics ──────────────────────────────────────────────────


class TestActivityMetrics:
    def test_bars_in_market_with_total_bars(self) -> None:
        # 3 trades each lasting 10 bars, over a 100-bar period.
        trades = [
            _trade(10.0, day_open=0, day_close=10),
            _trade(20.0, day_open=20, day_close=30),
            _trade(-5.0, day_open=50, day_close=60),
        ]
        out = compute_activity_metrics(
            trades, total_bars=100, bar_interval_ns=NS_PER_DAY,
        )
        # 30 bars in market / 100 total = 0.30
        assert out["bars_in_market_pct"] == pytest.approx(0.30)

    def test_fee_pct_of_pnl(self) -> None:
        out = compute_activity_metrics(
            trades=[],
            total_fees=50.0,
            total_pnl=1000.0,
        )
        # 50 / 1000 = 0.05
        assert out["fee_pct_of_pnl"] == pytest.approx(0.05)

    def test_fee_pct_when_pnl_is_zero(self) -> None:
        out = compute_activity_metrics(
            trades=[], total_fees=50.0, total_pnl=0.0,
        )
        assert math.isnan(out["fee_pct_of_pnl"])

    def test_fee_killed_strategy(self) -> None:
        # Fees > total PnL means strategy is fee-killed.
        out = compute_activity_metrics(
            trades=[], total_fees=200.0, total_pnl=100.0,
        )
        assert out["fee_pct_of_pnl"] == 2.0

    def test_no_trades_returns_nan_in_market(self) -> None:
        out = compute_activity_metrics(
            trades=[], total_bars=100, bar_interval_ns=NS_PER_DAY,
        )
        assert math.isnan(out["bars_in_market_pct"])


# ── compute_all_metrics integration ───────────────────────────────────────────


class TestComputeAllMetrics:
    def test_all_keys_merge_without_collision(self) -> None:
        trades = [_trade(10.0), _trade(-5.0)]
        idx = pd.to_datetime(["2025-01-01", "2025-12-31"], utc=True)
        balance = pd.Series([1000.0, 1005.0], index=idx)

        out = compute_all_metrics(
            trades, balance,
            starting_capital=1000.0,
            total_bars=365,
            bar_interval_ns=NS_PER_DAY,
            total_fees=2.0,
            total_pnl=5.0,
        )

        # Spot-check that keys from all three groups landed.
        assert "avg_pnl_per_trade" in out
        assert "max_drawdown_abs" in out
        assert "fee_pct_of_pnl" in out
        # And trade count check
        assert out["num_winners"] == 1.0
        assert out["num_losers"] == 1.0


# ── bootstrap_total_pnl ─────────────────────────────────────────────────────


class TestBootstrapTotalPnL:
    def test_returns_required_keys(self) -> None:
        out = bootstrap_total_pnl(
            [10.0, -5.0, 20.0, -3.0, 15.0],
            n_iterations=200, seed=1,
        )
        expected = {
            "mean", "std", "pct_5", "pct_25", "median", "pct_75", "pct_95",
            "min", "max", "n_iterations", "n_trades", "actual_total",
        }
        assert expected.issubset(out.keys())

    def test_actual_total_matches_input_sum(self) -> None:
        pnls = [10.0, -5.0, 20.0, -3.0, 15.0]
        out = bootstrap_total_pnl(pnls, n_iterations=100, seed=1)
        assert out["actual_total"] == pytest.approx(sum(pnls))

    def test_n_trades_matches_input_length(self) -> None:
        pnls = [1.0, 2.0, 3.0, 4.0]
        out = bootstrap_total_pnl(pnls, n_iterations=100, seed=1)
        assert out["n_trades"] == 4

    def test_seed_determinism(self) -> None:
        pnls = [10.0, -5.0, 20.0, -3.0, 15.0, 7.0, -2.0]
        a = bootstrap_total_pnl(pnls, n_iterations=500, seed=42)
        b = bootstrap_total_pnl(pnls, n_iterations=500, seed=42)
        assert a["mean"] == b["mean"]
        assert a["pct_5"] == b["pct_5"]
        assert a["pct_95"] == b["pct_95"]

    def test_percentiles_ordered(self) -> None:
        pnls = [10.0, -5.0, 20.0, -3.0, 15.0, 7.0, -2.0, 8.0]
        out = bootstrap_total_pnl(pnls, n_iterations=2000, seed=1)
        assert out["pct_5"] <= out["pct_25"] <= out["median"]
        assert out["median"] <= out["pct_75"] <= out["pct_95"]

    def test_mean_converges_to_actual_total(self) -> None:
        # With many iterations, the bootstrap mean should approach the
        # actual total (since each resample's mean PnL ≈ true mean PnL,
        # times n trades).
        pnls = [10.0, -5.0, 20.0, -3.0, 15.0]
        out = bootstrap_total_pnl(pnls, n_iterations=10_000, seed=1)
        # Within 5% of actual total
        assert abs(out["mean"] - out["actual_total"]) / abs(out["actual_total"]) < 0.05

    def test_empty_returns_all_nan(self) -> None:
        out = bootstrap_total_pnl([], n_iterations=100)
        assert all(math.isnan(v) for v in out.values())


# ── bootstrap_max_drawdown ──────────────────────────────────────────────────


class TestBootstrapMaxDrawdown:
    def test_returns_required_keys(self) -> None:
        out = bootstrap_max_drawdown(
            [10.0, -5.0, 20.0, -3.0, 15.0],
            n_iterations=200, seed=1,
        )
        expected = {
            "mean", "std", "pct_5", "pct_25", "median", "pct_75", "pct_95",
            "min", "max", "n_iterations", "n_trades", "actual_max_drawdown",
        }
        assert expected.issubset(out.keys())

    def test_drawdowns_are_non_positive(self) -> None:
        out = bootstrap_max_drawdown(
            [10.0, -5.0, 20.0, -3.0, 15.0],
            n_iterations=500, seed=1,
        )
        # Every percentile + actual must be ≤ 0
        for k in ("mean", "pct_5", "pct_25", "median", "pct_75", "pct_95",
                  "min", "max", "actual_max_drawdown"):
            assert out[k] <= 0.0, f"{k} should be ≤ 0, got {out[k]}"

    def test_pct5_is_worst_pct95_is_least_bad(self) -> None:
        # Drawdowns are negative, so pct_5 (worst) < pct_95 (best)
        out = bootstrap_max_drawdown(
            [10.0, -5.0, 20.0, -3.0, 15.0, 7.0, -2.0, 8.0],
            n_iterations=2000, seed=1,
        )
        assert out["pct_5"] <= out["pct_25"] <= out["median"]
        assert out["median"] <= out["pct_75"] <= out["pct_95"]
        assert out["pct_5"] <= out["pct_95"]  # worst <= least-bad

    def test_actual_max_drawdown_matches_input_path(self) -> None:
        # 10 → -10 → 5 → 25 → 15 (cumulative starting from 0)
        # peaks: 0,10,10,15,40,40 → drawdowns: 0,0,0,-5,0,-25 → MDD = -25
        # Wait: cumsum([10,-20,15,20,-25]) = [10,-10,5,25,0]
        # equity_with_zero = [0,10,-10,5,25,0]
        # running peak    = [0,10,10,10,25,25]
        # drawdowns       = [0, 0,-20,-5, 0,-25]  → MDD = -25
        pnls = [10.0, -20.0, 15.0, 20.0, -25.0]
        out = bootstrap_max_drawdown(pnls, n_iterations=100, seed=1)
        assert out["actual_max_drawdown"] == pytest.approx(-25.0)

    def test_all_winners_zero_drawdown(self) -> None:
        # Every trade is positive — no drawdown ever, regardless of order
        out = bootstrap_max_drawdown(
            [10.0, 20.0, 5.0, 15.0],
            n_iterations=100, seed=1,
        )
        assert out["actual_max_drawdown"] == 0.0
        # Resampling can only produce positive trades → max DD = 0 in
        # every resample (peak from 0 always rises monotonically).
        assert out["pct_5"] == 0.0
        assert out["pct_95"] == 0.0

    def test_all_losers_max_drawdown_is_total(self) -> None:
        # All losses → equity is monotonically decreasing → MDD = total loss
        out = bootstrap_max_drawdown(
            [-10.0, -5.0, -20.0],
            n_iterations=100, seed=1,
        )
        assert out["actual_max_drawdown"] == pytest.approx(-35.0)

    def test_seed_determinism(self) -> None:
        pnls = [10.0, -5.0, 20.0, -3.0, 15.0, 7.0, -2.0]
        a = bootstrap_max_drawdown(pnls, n_iterations=500, seed=42)
        b = bootstrap_max_drawdown(pnls, n_iterations=500, seed=42)
        assert a["mean"] == b["mean"]
        assert a["pct_5"] == b["pct_5"]
        assert a["pct_95"] == b["pct_95"]

    def test_empty_returns_all_nan(self) -> None:
        out = bootstrap_max_drawdown([], n_iterations=100)
        assert all(math.isnan(v) for v in out.values())


# ── compute_drawdown_periods ────────────────────────────────────────────────


class TestComputeDrawdownPeriods:
    def test_no_drawdown_returns_empty(self) -> None:
        idx = pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"], utc=True)
        s = pd.Series([1000.0, 1100.0, 1200.0], index=idx)
        assert compute_drawdown_periods(s) == []

    def test_empty_series(self) -> None:
        assert compute_drawdown_periods(pd.Series(dtype=float)) == []

    def test_single_drawdown_recovered(self) -> None:
        # 1000 → 1100 (peak) → 900 (trough) → 1200 (recovery, new peak)
        idx = pd.to_datetime(
            ["2025-01-01", "2025-01-15", "2025-02-01", "2025-02-15"], utc=True,
        )
        s = pd.Series([1000.0, 1100.0, 900.0, 1200.0], index=idx)
        periods = compute_drawdown_periods(s)
        assert len(periods) == 1
        p = periods[0]
        assert p["start"] == idx[2]    # first underwater bar
        assert p["end"] == idx[3]      # recovery bar
        assert p["trough"] == idx[2]   # deepest
        assert p["recovered"] is True
        assert p["depth_pct"] == pytest.approx(200.0 / 1100.0)
        assert p["depth_abs"] == pytest.approx(200.0)

    def test_open_drawdown_at_end(self) -> None:
        # Peak then under, no recovery
        idx = pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"], utc=True)
        s = pd.Series([1000.0, 1100.0, 800.0], index=idx)
        periods = compute_drawdown_periods(s)
        assert len(periods) == 1
        assert periods[0]["recovered"] is False
        assert periods[0]["end"] == idx[-1]

    def test_multiple_drawdowns(self) -> None:
        # Two distinct drawdowns separated by recovery to new peak.
        idx = pd.to_datetime(
            ["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01",
             "2025-05-01", "2025-06-01"],
            utc=True,
        )
        # 1000 → peak 1500 → 1200 (DD1) → 2000 (recovery) → 1800 (DD2) → 2500 (recovery)
        s = pd.Series([1000.0, 1500.0, 1200.0, 2000.0, 1800.0, 2500.0], index=idx)
        periods = compute_drawdown_periods(s)
        assert len(periods) == 2
        assert all(p["recovered"] for p in periods)

    def test_trough_is_deepest_point(self) -> None:
        # 1000 → peak 2000 → 1500 → 1200 (trough) → 1700 → 2100 (recovery)
        idx = pd.to_datetime(
            ["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01",
             "2025-05-01", "2025-06-01"],
            utc=True,
        )
        s = pd.Series([1000.0, 2000.0, 1500.0, 1200.0, 1700.0, 2100.0], index=idx)
        periods = compute_drawdown_periods(s)
        assert len(periods) == 1
        assert periods[0]["trough"] == idx[3]
        assert periods[0]["depth_abs"] == pytest.approx(800.0)
        assert periods[0]["depth_pct"] == pytest.approx(800.0 / 2000.0)
