"""Unit tests for ``notebooks/_compare_helpers.py``."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# notebooks/ isn't a package; add it to sys.path so we can import.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from _compare_helpers import (  # type: ignore[import-not-found] # noqa: E402
    build_stability_df,
    short_sweep_label,
)

# ── short_sweep_label ──────────────────────────────────────────────────────


class TestShortSweepLabel:
    def test_drops_strategy_prefix(self) -> None:
        assert short_sweep_label(
            "MACross-EMA-HYPERLIQUID_PERP · BTCUSDT-PERP.BINANCE · 1d",
        ) == "BTCUSDT-PERP.BINANCE · 1d"

    def test_passes_through_when_two_parts(self) -> None:
        assert short_sweep_label("BTC · 1d") == "BTC · 1d"

    def test_passes_through_when_no_separator(self) -> None:
        assert short_sweep_label("just-a-label") == "just-a-label"


# ── build_stability_df ─────────────────────────────────────────────────────


def _make_sweep(
    label_data: list[tuple[int, int, float]], *, label: str = "x",
) -> pd.DataFrame:
    """Build a minimal sweep DataFrame from (fast, slow, pnl) tuples.

    total_pnl_pct is set proportional to total_pnl (10%/$100) so the
    aggregate columns are well-defined.
    """
    rows = []
    for fast, slow, pnl in label_data:
        rows.append({
            "fast": fast, "slow": slow,
            "total_pnl": pnl,
            "total_pnl_pct": pnl / 10.0,
        })
    return pd.DataFrame(rows)


class TestBuildStabilityDf:
    def test_empty_dict_returns_empty(self) -> None:
        out, n = build_stability_df({}, ["fast", "slow"])
        assert out.empty
        assert n == 0

    def test_single_sweep_aggregates(self) -> None:
        sweeps = {
            "btc": _make_sweep([(5, 20, 100.0), (10, 30, 200.0)]),
        }
        out, n = build_stability_df(sweeps, ["fast", "slow"])
        assert n == 1
        assert len(out) == 2
        # avg == min == max for a single-sweep aggregate
        for _, row in out.iterrows():
            assert row["avg_pnl_pct"] == row["min_pnl_pct"] == row["max_pnl_pct"]
            assert row["sweep_count"] == 1

    def test_two_sweeps_with_overlap(self) -> None:
        sweeps = {
            "btc": _make_sweep([(5, 20, 100.0), (10, 30, 200.0)]),
            "eth": _make_sweep([(5, 20, 300.0), (15, 40, 400.0)]),
        }
        out, n = build_stability_df(sweeps, ["fast", "slow"])
        assert n == 2
        # (5, 20) appears in both → sweep_count = 2
        five_twenty = out[(out["fast"] == 5) & (out["slow"] == 20)]
        assert five_twenty["sweep_count"].iloc[0] == 2
        # avg_pnl_pct = (100/10 + 300/10) / 2 = 20.0
        assert five_twenty["avg_pnl_pct"].iloc[0] == pytest.approx(20.0)
        # (10, 30) only in btc → count 1
        ten_thirty = out[(out["fast"] == 10) & (out["slow"] == 30)]
        assert ten_thirty["sweep_count"].iloc[0] == 1

    def test_skips_sweep_missing_param_cols(self) -> None:
        # Second sweep doesn't have 'slow' column — should be skipped
        sweeps = {
            "good": _make_sweep([(5, 20, 100.0)]),
            "bad":  pd.DataFrame([{"fast": 5, "total_pnl": 100, "total_pnl_pct": 10}]),
        }
        out, n = build_stability_df(sweeps, ["fast", "slow"])
        assert n == 1  # only "good" contributed
        assert len(out) == 1

    def test_skips_sweep_with_duplicate_param_pairs(self) -> None:
        # A sensitivity sweep has multiple rows for the same (fast, slow)
        sensitivity = pd.DataFrame([
            {"fast": 5, "slow": 20, "atr_sl": 1.0, "total_pnl": 100, "total_pnl_pct": 10},
            {"fast": 5, "slow": 20, "atr_sl": 2.0, "total_pnl": 200, "total_pnl_pct": 20},
        ])
        sweeps = {
            "main":        _make_sweep([(5, 20, 100.0)]),
            "sensitivity": sensitivity,
        }
        out, n = build_stability_df(sweeps, ["fast", "slow"])
        assert n == 1  # sensitivity sweep skipped

    def test_all_profitable_flag(self) -> None:
        sweeps = {
            "good":  _make_sweep([(5, 20, 100.0), (10, 30, 200.0)]),
            "mixed": _make_sweep([(5, 20, -50.0), (10, 30, 300.0)]),
        }
        out, _ = build_stability_df(sweeps, ["fast", "slow"])
        # (5, 20) had a loss in "mixed" → all_profitable False
        five_twenty = out[(out["fast"] == 5) & (out["slow"] == 20)]
        assert five_twenty["all_profitable"].iloc[0] is False or \
               five_twenty["all_profitable"].iloc[0] == False  # noqa: E712
        # (10, 30) profitable everywhere
        ten_thirty = out[(out["fast"] == 10) & (out["slow"] == 30)]
        assert ten_thirty["all_profitable"].iloc[0] is True or \
               ten_thirty["all_profitable"].iloc[0] == True  # noqa: E712

    def test_sorted_descending_by_avg_pnl_pct(self) -> None:
        sweeps = {
            "btc": _make_sweep([
                (5, 20, 100.0), (10, 30, 500.0), (15, 40, 200.0),
            ]),
        }
        out, _ = build_stability_df(sweeps, ["fast", "slow"])
        # avg_pnl_pct should descend
        assert list(out["avg_pnl_pct"]) == sorted(
            out["avg_pnl_pct"], reverse=True,
        )
