"""Unit tests for ``charts.generate_cross_sweep_html``.

Sister test of ``test_sweep_html.py`` — exercises the multi-sweep
combined-table renderer.  We construct synthetic sweeps with different
parameter shapes and assert the structural and formatting elements
that consumers rely on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# notebooks/ is not a proper package; add it to sys.path so the test
# can import charts.py just like a notebook would.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from charts import generate_cross_sweep_html  # type: ignore[import-not-found] # noqa: E402


def _make_ema_sweep() -> pd.DataFrame:
    """EMA cross sweep with fast/slow params."""
    base_meta = {
        "_strategy": "MACross-EMA",
        "_instrument_id": "BTC-USD-PERP.HYPERLIQUID",
        "_bar_interval": "1d",
        "_swept_at": "2025-01-01T00:00:00+00:00",
        "_schema_version": 2,
    }
    rows = [
        {
            **base_meta, "fast": 10, "slow": 20,
            "total_pnl": 9510.62, "total_pnl_pct": 951.06, "num_positions": 57,
            "win_rate": 0.386, "avg_pnl_per_trade": 166.85,
            "pnl_profit_factor": 5.52, "expectancy": 166.85, "payoff_ratio": 4.16,
            "max_drawdown_pct": 0.18, "max_drawdown_abs": 200.0,
            "mar_ratio": 1.83, "recovery_factor": 47.5, "cagr": 0.95,
            "max_consec_losers": 4, "bars_in_market_pct": 0.62,
            "largest_win": 7249.12, "largest_loss": -467.15,
            "long_pnl": 4500.0, "short_pnl": 5010.62,
            "total_fees": 80.0, "fee_pct_of_pnl": 0.008,
            "min_balance": 463.04, "liquidated": False, "error": "",
            "_kind": None,
        },
        {
            **base_meta, "fast": 40, "slow": 50,
            "total_pnl": -990.79, "total_pnl_pct": -99.08, "num_positions": 3,
            "win_rate": 0.0, "avg_pnl_per_trade": -330.26,
            "pnl_profit_factor": 0.0, "expectancy": -330.26,
            "payoff_ratio": float("nan"),
            "max_drawdown_pct": 0.99, "max_drawdown_abs": 990.0,
            "mar_ratio": float("nan"), "recovery_factor": -1.0,
            "cagr": float("nan"), "max_consec_losers": 3,
            "bars_in_market_pct": 0.05, "largest_win": float("nan"),
            "largest_loss": -467.0, "long_pnl": -300.0, "short_pnl": -690.0,
            "total_fees": 4.0, "fee_pct_of_pnl": 0.004, "min_balance": -50.0,
            "liquidated": True, "error": "liquidated", "_kind": None,
        },
    ]
    return pd.DataFrame(rows)


def _make_bb_sweep() -> pd.DataFrame:
    """Bollinger-band sweep with different params (length/mult).

    Includes a spotlight row so the badge logic is exercised.
    """
    base_meta = {
        "_strategy": "BBMeanRev",
        "_instrument_id": "ETH-USD-PERP.HYPERLIQUID",
        "_bar_interval": "4h",
        "_swept_at": "2025-01-02T00:00:00+00:00",
        "_schema_version": 2,
    }
    rows = [
        {
            **base_meta, "length": 14, "mult": 2.0,
            "total_pnl": 4200.0, "total_pnl_pct": 42.0, "num_positions": 88,
            "win_rate": 0.55, "avg_pnl_per_trade": 47.7,
            "pnl_profit_factor": 1.85, "expectancy": 47.7, "payoff_ratio": 1.5,
            "max_drawdown_pct": 0.12, "max_drawdown_abs": 320.0,
            "mar_ratio": 1.05, "recovery_factor": 13.1, "cagr": 0.42,
            "max_consec_losers": 6, "bars_in_market_pct": 0.78,
            "largest_win": 1200.0, "largest_loss": -380.0,
            "long_pnl": 2200.0, "short_pnl": 2000.0,
            "total_fees": 130.0, "fee_pct_of_pnl": 0.031,
            "min_balance": 9650.0, "liquidated": False, "error": "",
            "_kind": None,
        },
        {
            **base_meta, "length": 20, "mult": 2.5,
            "total_pnl": 1200.0, "total_pnl_pct": 12.0, "num_positions": 30,
            "win_rate": 0.50, "avg_pnl_per_trade": 40.0,
            "pnl_profit_factor": 1.30, "expectancy": 40.0, "payoff_ratio": 1.2,
            "max_drawdown_pct": 0.18, "max_drawdown_abs": 540.0,
            "mar_ratio": 0.55, "recovery_factor": 2.2, "cagr": 0.12,
            "max_consec_losers": 4, "bars_in_market_pct": 0.55,
            "largest_win": 600.0, "largest_loss": -250.0,
            "long_pnl": 700.0, "short_pnl": 500.0,
            "total_fees": 50.0, "fee_pct_of_pnl": 0.041,
            "min_balance": 9200.0, "liquidated": False, "error": "",
            "_kind": "spotlight",
        },
    ]
    return pd.DataFrame(rows)


@pytest.fixture
def cross_sweep_html(tmp_path: Path) -> str:
    sweeps = {
        "ema_BTC_1d": _make_ema_sweep(),
        "bb_ETH_4h": _make_bb_sweep(),
    }
    out = generate_cross_sweep_html(
        sweeps, output_dir=tmp_path, filename="test_cross",
    )
    return str(out.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Structural elements
# ─────────────────────────────────────────────────────────────────────────────


class TestStructuralElements:
    def test_contains_datatables_cdn(self, cross_sweep_html: str) -> None:
        assert "datatables.net" in cross_sweep_html

    def test_contains_table_id(self, cross_sweep_html: str) -> None:
        assert 'id="sweepTable"' in cross_sweep_html

    def test_contains_csv_button(self, cross_sweep_html: str) -> None:
        assert "extend: 'csv'" in cross_sweep_html

    def test_contains_stats_bar(self, cross_sweep_html: str) -> None:
        assert "stats-bar" in cross_sweep_html
        assert "Best PnL" in cross_sweep_html
        assert "Liquidated" in cross_sweep_html
        assert "Sweeps" in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-label column
# ─────────────────────────────────────────────────────────────────────────────


class TestSweepLabelColumn:
    def test_sweep_header_present(self, cross_sweep_html: str) -> None:
        assert "<th>Sweep</th>" in cross_sweep_html

    def test_sweep_labels_appear_in_body(self, cross_sweep_html: str) -> None:
        assert "ema_BTC_1d" in cross_sweep_html
        assert "bb_ETH_4h" in cross_sweep_html

    def test_sweep_column_is_first(self, cross_sweep_html: str) -> None:
        # The Sweep column must precede any param/metric headers.
        sweep_idx = cross_sweep_html.find("<th>Sweep</th>")
        fast_idx = cross_sweep_html.find("<th>fast</th>")
        pnl_idx = cross_sweep_html.find("<th>PnL ($)</th>")
        assert sweep_idx > 0
        assert sweep_idx < fast_idx
        assert sweep_idx < pnl_idx


# ─────────────────────────────────────────────────────────────────────────────
# Param column union (different params in each sweep)
# ─────────────────────────────────────────────────────────────────────────────


class TestParamColumnUnion:
    def test_ema_params_appear(self, cross_sweep_html: str) -> None:
        assert "<th>fast</th>" in cross_sweep_html
        assert "<th>slow</th>" in cross_sweep_html

    def test_bb_params_appear(self, cross_sweep_html: str) -> None:
        assert "<th>length</th>" in cross_sweep_html
        assert "<th>mult</th>" in cross_sweep_html

    def test_metadata_columns_skipped(self, cross_sweep_html: str) -> None:
        # Underscore-prefixed columns are metadata; they should not be
        # rendered as headers.  We look for the literal header tags here
        # (the sweep labels can still contain underscores in the body).
        assert "<th>_strategy</th>" not in cross_sweep_html
        assert "<th>_kind</th>" not in cross_sweep_html
        assert "<th>_schema_version</th>" not in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Row classification (liquidated / spotlight badges)
# ─────────────────────────────────────────────────────────────────────────────


class TestRowClassification:
    def test_liquidated_row_has_class(self, cross_sweep_html: str) -> None:
        # The EMA fast=40 row is liquidated.
        assert "liquidated" in cross_sweep_html
        assert ">LIQ<" in cross_sweep_html

    def test_spotlight_row_has_class(self, cross_sweep_html: str) -> None:
        # The BB length=20 row is spotlight.
        assert "spotlight" in cross_sweep_html

    def test_kind_column_appears_when_spotlight_present(
        self, cross_sweep_html: str,
    ) -> None:
        assert "<th>Kind</th>" in cross_sweep_html
        assert "badge-spotlight" in cross_sweep_html
        assert "SPOT" in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────


class TestFormatting:
    def test_money_thousands_separator(self, cross_sweep_html: str) -> None:
        # 9510.62 → "9,510.62"
        assert "9,510.62" in cross_sweep_html

    def test_win_rate_as_percent(self, cross_sweep_html: str) -> None:
        # 0.55 → "55.00%"
        assert "55.00%" in cross_sweep_html

    def test_drawdown_as_percent(self, cross_sweep_html: str) -> None:
        # 0.12 → "12.00%"
        assert "12.00%" in cross_sweep_html

    def test_nan_renders_as_em_dash(self, cross_sweep_html: str) -> None:
        # The liquidated EMA row has NaN payoff_ratio / NaN largest_win.
        assert "—" in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Schema-version handling
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaVersion:
    def test_uniform_schema_v2(self, cross_sweep_html: str) -> None:
        # Both inputs are v2 → header should say "Schema v2".
        assert "Schema v2" in cross_sweep_html

    def test_mixed_schemas_label_as_mixed(self, tmp_path: Path) -> None:
        v1 = _make_ema_sweep()
        v1["_schema_version"] = 1
        v2 = _make_bb_sweep()
        out = generate_cross_sweep_html(
            {"v1_old": v1, "v2_new": v2},
            output_dir=tmp_path, filename="mixed",
        )
        text = out.read_text(encoding="utf-8")
        assert "Schema vMixed" in text

    def test_missing_schema_column_label_as_unknown(
        self, tmp_path: Path,
    ) -> None:
        df = _make_ema_sweep().drop(columns=["_schema_version"])
        out = generate_cross_sweep_html(
            {"unknown": df}, output_dir=tmp_path, filename="unk",
        )
        text = out.read_text(encoding="utf-8")
        assert "Schema v?" in text


# ─────────────────────────────────────────────────────────────────────────────
# Stats bar
# ─────────────────────────────────────────────────────────────────────────────


class TestStatsBar:
    def test_sweep_count_two(self, cross_sweep_html: str) -> None:
        # 2 sweeps → "2" appears as the stat value next to "Sweeps:".
        assert ">2</span>" in cross_sweep_html

    def test_combo_count_four(self, cross_sweep_html: str) -> None:
        # 2 EMA rows + 2 BB rows = 4 combos.
        assert ">4</span>" in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Default sort
# ─────────────────────────────────────────────────────────────────────────────


class TestDefaultSort:
    def test_sort_targets_total_pnl_desc(self, cross_sweep_html: str) -> None:
        assert "order: [[" in cross_sweep_html
        assert ", 'desc' ]]" in cross_sweep_html

    def test_sort_index_skips_sweep_kind_and_param_columns(
        self, cross_sweep_html: str,
    ) -> None:
        # Sort target is total_pnl, which is the first metric.
        # offset = 1 (Sweep) + 1 (Kind, since a spotlight row exists)
        #        + 4 param cols (fast, slow, length, mult) = 6.
        assert "order: [[ 6, 'desc' ]]" in cross_sweep_html


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_mapping_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            generate_cross_sweep_html({}, output_dir=tmp_path)

    def test_all_empty_dfs_raise(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            generate_cross_sweep_html(
                {"a": pd.DataFrame(), "b": pd.DataFrame()},
                output_dir=tmp_path, filename="all_empty",
            )

    def test_one_empty_df_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = generate_cross_sweep_html(
            {"empty": pd.DataFrame(), "real": _make_ema_sweep()},
            output_dir=tmp_path, filename="one_empty",
        )
        captured = capsys.readouterr()
        assert "skipping empty sweep 'empty'" in captured.out
        text = out.read_text(encoding="utf-8")
        # Header should reflect the single non-empty sweep.
        assert ">1</span>" in text  # Sweeps: 1

    def test_default_filename(self, tmp_path: Path) -> None:
        out = generate_cross_sweep_html(
            {"a": _make_ema_sweep()}, output_dir=tmp_path,
        )
        assert out.name == "cross_sweep.html"

    def test_filename_without_extension_gets_html_suffix(
        self, tmp_path: Path,
    ) -> None:
        out = generate_cross_sweep_html(
            {"a": _make_ema_sweep()},
            output_dir=tmp_path, filename="no_ext",
        )
        assert out.name == "no_ext.html"

    def test_kind_column_omitted_when_no_spotlight(
        self, tmp_path: Path,
    ) -> None:
        df = _make_ema_sweep()
        # No spotlight rows in either sweep.
        out = generate_cross_sweep_html(
            {"a": df}, output_dir=tmp_path, filename="no_spot",
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>Kind</th>" not in text
        # The class is defined in CSS regardless; check for actual usage.
        assert '<span class="badge badge-spotlight">' not in text
        assert "SPOT" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Extra columns
# ─────────────────────────────────────────────────────────────────────────────


class TestExtraColumns:
    def test_extra_columns_included(self, tmp_path: Path) -> None:
        df = _make_ema_sweep()
        df["custom_metric"] = [1.5, 2.5]
        out = generate_cross_sweep_html(
            {"a": df}, output_dir=tmp_path, filename="extra",
            extra_columns=["custom_metric"],
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>custom_metric</th>" in text


# ─────────────────────────────────────────────────────────────────────────────
# Custom title
# ─────────────────────────────────────────────────────────────────────────────


class TestCustomTitle:
    def test_default_title_mentions_sweep_count(
        self, cross_sweep_html: str,
    ) -> None:
        # Default title becomes "Cross-sweep — 2 sweeps" (template
        # prepends "Cross-sweep — ", default title is just the count).
        assert "Cross-sweep — 2 sweeps" in cross_sweep_html
        # And the title prefix should not be doubled.
        assert "Cross-sweep — Cross-sweep" not in cross_sweep_html

    def test_custom_title_used(self, tmp_path: Path) -> None:
        out = generate_cross_sweep_html(
            {"a": _make_ema_sweep()},
            output_dir=tmp_path, filename="custom",
            title="My Custom Comparison",
        )
        text = out.read_text(encoding="utf-8")
        assert "My Custom Comparison" in text
