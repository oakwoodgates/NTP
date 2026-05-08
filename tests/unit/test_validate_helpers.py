"""Unit tests for ``notebooks/_validate_helpers.py``.

The helpers there are extracted from validate_strategy.ipynb to keep
cells short — testing them gives the notebook some safety net (without
requiring a full backtest run) and documents the expected shapes.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

# notebooks/ isn't a package; add it to sys.path so we can import.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from _validate_helpers import (  # type: ignore[import-not-found] # noqa: E402
    STRATEGIES,
    collapse_to_grid,
    enrich_regime_with_wilson,
    get_param_grid,
    make_strategy_factory,
    parse_pnl,
    plateau_scores,
    short_param_key,
    short_params_tag,
)

# ── STRATEGIES registry ─────────────────────────────────────────────────────


class TestStrategiesRegistry:
    def test_has_at_least_15_strategies(self) -> None:
        assert len(STRATEGIES) >= 15

    def test_each_entry_is_4_tuple(self) -> None:
        for key, entry in STRATEGIES.items():
            assert len(entry) == 4, f"{key}: expected 4-tuple, got {len(entry)}"
            cls, cfg_cls, param_map, fixed = entry
            assert isinstance(param_map, dict), f"{key}: param_map not dict"
            assert isinstance(fixed, dict), f"{key}: fixed not dict"

    def test_ma_cross_emas_share_strategy_class(self) -> None:
        # All MACross-XX entries should use the same Strategy class
        ma_cross_keys = [k for k in STRATEGIES if k.startswith("MACross-")]
        assert len(ma_cross_keys) >= 6
        first_cls = STRATEGIES[ma_cross_keys[0]][0]
        for k in ma_cross_keys:
            assert STRATEGIES[k][0] is first_cls


# ── make_strategy_factory ──────────────────────────────────────────────────


class TestMakeStrategyFactory:
    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            make_strategy_factory(
                "DoesNotExist",
                "BTCUSDT-PERP.BINANCE",
                "BTCUSDT-PERP.BINANCE-1-DAY-LAST-INTERNAL",
                Decimal("2000"),
            )

    def test_returns_callable(self) -> None:
        factory = make_strategy_factory(
            "MACross-EMA",
            "BTCUSDT-PERP.BINANCE",
            "BTCUSDT-PERP.BINANCE-1-DAY-LAST-INTERNAL",
            Decimal("2000"),
        )
        assert callable(factory)


# ── get_param_grid ──────────────────────────────────────────────────────────


class TestGetParamGrid:
    def test_ema_cross_grid_shape(self) -> None:
        combos, row, col = get_param_grid("MACross-EMA")
        assert isinstance(combos, list)
        assert all(isinstance(c, dict) for c in combos)
        assert all("fast" in c and "slow" in c for c in combos)
        assert all(c["fast"] < c["slow"] for c in combos)
        assert row == "slow"
        assert col == "fast"

    def test_bb_meanrev_grid_shape(self) -> None:
        combos, row, col = get_param_grid("BBMeanRev")
        assert all("bb_period" in c and "bb_std" in c for c in combos)
        assert row == "bb_std"
        assert col == "bb_period"

    def test_donchian_grid_shape(self) -> None:
        combos, row, col = get_param_grid("DonchianBreakout")
        assert all("dc_period" in c for c in combos)
        # Single-param strategy → row == col by convention
        assert row == "dc_period" and col == "dc_period"

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="No param grid"):
            get_param_grid("DoesNotExist")

    def test_all_registry_strategies_have_grid(self) -> None:
        # Every registered strategy should have a grid defined.
        for key in STRATEGIES:
            combos, row, col = get_param_grid(key)
            assert len(combos) > 0, f"{key}: empty grid"
            assert row and col, f"{key}: missing row/col labels"


# ── collapse_to_grid ────────────────────────────────────────────────────────


class TestCollapseToGrid:
    def test_no_op_when_unique(self) -> None:
        df = pd.DataFrame({
            "fast": [5, 10, 15],
            "slow": [20, 30, 40],
            "total_pnl": [100, 200, 300],
        })
        out = collapse_to_grid(df, "slow", "fast")
        # Same row count — no collapsing needed
        assert len(out) == len(df)

    def test_picks_best_pnl_when_duplicates(self) -> None:
        # Two rows for (fast=5, slow=20) — should keep the higher PnL
        df = pd.DataFrame({
            "fast": [5, 5, 10],
            "slow": [20, 20, 30],
            "atr": [1.0, 2.0, 1.0],   # extra param dimension
            "total_pnl": [100, 500, 200],
        })
        out = collapse_to_grid(df, "slow", "fast")
        assert len(out) == 2
        # The (5, 20) entry should be the PnL=500 row
        five_twenty = out[(out["fast"] == 5) & (out["slow"] == 20)]
        assert len(five_twenty) == 1
        assert five_twenty["total_pnl"].iloc[0] == 500


# ── plateau_scores ──────────────────────────────────────────────────────────


def _make_grid(values: list[list[float]]) -> pd.DataFrame:
    """Build a sweep DataFrame from a 2-D PnL grid (rows = slow, cols = fast)."""
    rows = []
    for r, row in enumerate(values):
        for c, pnl in enumerate(row):
            rows.append({"fast": c + 1, "slow": r + 1, "total_pnl": pnl})
    return pd.DataFrame(rows)


class TestPlateauScores:
    def test_returns_required_columns(self) -> None:
        df = _make_grid([[100, 200], [150, 250]])
        out = plateau_scores(df, row_col="slow", col_col="fast")
        for col in ("profitable", "neighbour_score", "neighbour_avg"):
            assert col in out.columns

    def test_all_profitable_score_is_one(self) -> None:
        # 3×3 all-positive grid → every cell sees only profitable nbrs
        df = _make_grid([[100, 200, 300], [400, 500, 600], [700, 800, 900]])
        out = plateau_scores(df, row_col="slow", col_col="fast")
        assert (out["neighbour_score"] == 1.0).all()

    def test_all_losers_score_is_zero(self) -> None:
        df = _make_grid([[-100, -200, -300], [-400, -500, -600]])
        out = plateau_scores(df, row_col="slow", col_col="fast")
        assert (out["neighbour_score"] == 0.0).all()

    def test_isolated_winner_low_score(self) -> None:
        # One profitable cell surrounded by losers
        df = _make_grid([
            [-100, -100, -100],
            [-100,  500, -100],
            [-100, -100, -100],
        ])
        out = plateau_scores(df, row_col="slow", col_col="fast")
        # Center cell sees 1 profitable nbr (itself) out of 9 → 1/9 ≈ 0.11
        center = out[(out["fast"] == 2) & (out["slow"] == 2)]
        assert center["neighbour_score"].iloc[0] == pytest.approx(1 / 9)
        assert center["profitable"].iloc[0]

    def test_neighbour_avg_includes_self(self) -> None:
        df = _make_grid([[100, 200], [300, 400]])
        out = plateau_scores(df, row_col="slow", col_col="fast")
        # Top-left (1,1) has neighbours (1,1)=100, (1,2)=200, (2,1)=300, (2,2)=400
        # Mean = 1000/4 = 250
        tl = out[(out["fast"] == 1) & (out["slow"] == 1)]
        assert tl["neighbour_avg"].iloc[0] == pytest.approx(250.0)


# ── parse_pnl ───────────────────────────────────────────────────────────────


class TestParsePnL:
    def test_normal_string(self) -> None:
        assert parse_pnl("123.45 USDC") == 123.45

    def test_negative(self) -> None:
        assert parse_pnl("-50.00 USDC") == -50.0

    def test_no_currency_suffix(self) -> None:
        assert parse_pnl("100.0") == 100.0

    def test_none_returns_nan(self) -> None:
        import math
        assert math.isnan(parse_pnl(None))
        assert math.isnan(parse_pnl("None"))
        assert math.isnan(parse_pnl("nan"))
        assert math.isnan(parse_pnl("NaT"))

    def test_garbage_returns_nan(self) -> None:
        import math
        assert math.isnan(parse_pnl("not a number"))


# ── enrich_regime_with_wilson ───────────────────────────────────────────────


class TestEnrichRegimeWithWilson:
    def test_adds_wr_ci_column(self) -> None:
        df = pd.DataFrame([
            {"regime": "TRENDING", "num_positions": 15, "win_rate": 0.53},
            {"regime": "RANGING",  "num_positions":  4, "win_rate": 0.50},
        ])
        out = enrich_regime_with_wilson(df)
        assert "wr_ci" in out.columns
        assert len(out) == len(df)

    def test_zero_positions_renders_dash(self) -> None:
        df = pd.DataFrame([
            {"regime": "QUIET", "num_positions": 0, "win_rate": 0.0},
        ])
        out = enrich_regime_with_wilson(df)
        assert out["wr_ci"].iloc[0] == "—"

    def test_small_sample_wider_than_large(self) -> None:
        # The width of the Wilson interval should be wider for n=4 than n=100
        df = pd.DataFrame([
            {"regime": "SMALL", "num_positions":   4, "win_rate": 0.50},
            {"regime": "BIG",   "num_positions": 100, "win_rate": 0.50},
        ])
        out = enrich_regime_with_wilson(df)
        # Parse "[lo, hi]" strings back
        small_lo, small_hi = (
            float(x) for x in out.loc[0, "wr_ci"].strip("[]").split(", ")
        )
        big_lo, big_hi = (
            float(x) for x in out.loc[1, "wr_ci"].strip("[]").split(", ")
        )
        assert (small_hi - small_lo) > (big_hi - big_lo)

    def test_does_not_mutate_input(self) -> None:
        df = pd.DataFrame([
            {"regime": "TRENDING", "num_positions": 15, "win_rate": 0.53},
        ])
        cols_before = list(df.columns)
        enrich_regime_with_wilson(df)
        assert list(df.columns) == cols_before  # original DF untouched


# ── short_param_key + short_params_tag ──────────────────────────────────────


class TestShortParamKey:
    def test_single_word(self) -> None:
        assert short_param_key("fast") == "f"
        assert short_param_key("slow") == "s"

    def test_underscore_separated(self) -> None:
        assert short_param_key("bb_period") == "bp"
        assert short_param_key("bb_std") == "bs"
        assert short_param_key("dc_period") == "dp"
        assert short_param_key("atr_sl") == "as"
        assert short_param_key("atr_tp") == "at"
        assert short_param_key("trailing_mult") == "tm"

    def test_collisions_within_strategy(self) -> None:
        # Within each strategy's grid, the short keys must be unique
        # (otherwise a filename like "bb20_bb2.0" can't round-trip).
        for strategy in STRATEGIES:
            combos, _, _ = get_param_grid(strategy)
            keys = list(combos[0].keys())
            short = [short_param_key(k) for k in keys]
            assert len(short) == len(set(short)), (
                f"{strategy}: short-key collision in "
                f"{dict(zip(keys, short, strict=True))}"
            )

    def test_empty_string(self) -> None:
        assert short_param_key("") == ""


class TestShortParamsTag:
    def test_ma_cross_format(self) -> None:
        assert short_params_tag({"fast": 10, "slow": 20}) == "f10_s20"

    def test_bb_meanrev_format(self) -> None:
        assert short_params_tag(
            {"bb_period": 20, "bb_std": 2.0},
        ) == "bp20_bs2.0"

    def test_donchian_format(self) -> None:
        assert short_params_tag({"dc_period": 20}) == "dp20"

    def test_empty_dict(self) -> None:
        assert short_params_tag({}) == ""

    def test_preserves_dict_order(self) -> None:
        # Python 3.7+ dicts preserve insertion order; we rely on that
        # for stable filenames.  Verify reordering produces a different
        # tag (would be a problem if the function silently sorted).
        a = short_params_tag({"fast": 10, "slow": 20})
        b = short_params_tag({"slow": 20, "fast": 10})
        assert a == "f10_s20"
        assert b == "s20_f10"
