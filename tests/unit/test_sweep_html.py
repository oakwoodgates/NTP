"""Unit tests for ``charts.generate_sweep_html``.

The function is in the notebooks/ tree (notebook helpers) but is
self-contained enough to test from CI.  We construct a synthetic sweep
DataFrame, generate the HTML, and assert presence of the structural and
formatting elements that consumers rely on.
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

from charts import generate_sweep_html  # type: ignore[import-not-found] # noqa: E402


def _make_sweep_df() -> pd.DataFrame:
    """Build a 3-row synthetic sweep DataFrame: healthy + liquidated + spotlight."""
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
        {
            **base_meta, "fast": 9, "slow": 18,
            "total_pnl": 8200.0, "total_pnl_pct": 820.0, "num_positions": 65,
            "win_rate": 0.40, "avg_pnl_per_trade": 126.15,
            "pnl_profit_factor": 4.5, "expectancy": 126.15, "payoff_ratio": 3.5,
            "max_drawdown_pct": 0.22, "max_drawdown_abs": 250.0,
            "mar_ratio": 1.55, "recovery_factor": 32.8, "cagr": 0.82,
            "max_consec_losers": 5, "bars_in_market_pct": 0.70,
            "largest_win": 1500.0, "largest_loss": -300.0,
            "long_pnl": 4100.0, "short_pnl": 4100.0, "total_fees": 90.0,
            "fee_pct_of_pnl": 0.011, "min_balance": 750.0,
            "liquidated": False, "error": "", "_kind": "spotlight",
        },
    ]
    return pd.DataFrame(rows)


@pytest.fixture
def sweep_html(tmp_path: Path) -> str:
    df = _make_sweep_df()
    out = generate_sweep_html(df, output_dir=tmp_path, filename="test_sweep")
    return str(out.read_text(encoding="utf-8"))


class TestStructuralElements:
    def test_contains_datatables_cdn(self, sweep_html: str) -> None:
        assert "datatables.net" in sweep_html

    def test_contains_table_id(self, sweep_html: str) -> None:
        assert 'id="sweepTable"' in sweep_html

    def test_contains_csv_button(self, sweep_html: str) -> None:
        assert "extend: 'csv'" in sweep_html

    def test_contains_stats_bar(self, sweep_html: str) -> None:
        assert "stats-bar" in sweep_html
        assert "Best PnL" in sweep_html
        assert "Liquidated" in sweep_html


class TestRowClassification:
    def test_liquidated_row_has_class(self, sweep_html: str) -> None:
        assert '<tr class="liquidated">' in sweep_html

    def test_spotlight_row_has_class(self, sweep_html: str) -> None:
        assert '<tr class="spotlight">' in sweep_html

    def test_spotlight_badge_present(self, sweep_html: str) -> None:
        assert "badge-spotlight" in sweep_html
        assert "SPOT" in sweep_html

    def test_liquidated_badge_present(self, sweep_html: str) -> None:
        assert "badge-liquidated" in sweep_html
        assert ">LIQ<" in sweep_html


class TestFormatting:
    def test_money_thousands_separator(self, sweep_html: str) -> None:
        # 9510.62 → "9,510.62"
        assert "9,510.62" in sweep_html

    def test_win_rate_as_percent(self, sweep_html: str) -> None:
        # 0.386 → "38.60%"
        assert "38.60%" in sweep_html

    def test_drawdown_as_percent(self, sweep_html: str) -> None:
        # 0.18 → "18.00%"
        assert "18.00%" in sweep_html

    def test_cagr_fractional_to_percent(self, sweep_html: str) -> None:
        # CAGR 0.95 (fractional) → 95.00% (after pct_signed heuristic)
        assert "95.00%" in sweep_html

    def test_pnl_pct_already_pct(self, sweep_html: str) -> None:
        # total_pnl_pct=951.06 should stay as 951.06% (already-percent units)
        assert "951.06%" in sweep_html

    def test_nan_renders_as_em_dash(self, sweep_html: str) -> None:
        # The liquidated row has NaN payoff_ratio and NaN largest_win.
        # Em-dash should appear at least once in the table body.
        assert "—" in sweep_html

    def test_infinite_renders_as_infinity(self) -> None:
        df = _make_sweep_df()
        df.loc[0, "pnl_profit_factor"] = float("inf")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = generate_sweep_html(df, output_dir=tmp, filename="inf")
            text = out.read_text(encoding="utf-8")
            assert "∞" in text


class TestDefaultSort:
    def test_sort_targets_total_pnl(self, sweep_html: str) -> None:
        # The DataTables config should include 'order: [[ N, "desc" ]]'
        # where N points at the total_pnl column.
        assert "order: [[" in sweep_html
        assert ", 'desc' ]]" in sweep_html


class TestParameterColumns:
    def test_strategy_params_appear_as_headers(self, sweep_html: str) -> None:
        # fast/slow are strategy params; they should each appear as <th>.
        assert "<th>fast</th>" in sweep_html
        assert "<th>slow</th>" in sweep_html


class TestEmptyDataFrame:
    def test_empty_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            generate_sweep_html(pd.DataFrame(), output_dir=tmp_path)


class TestStatsBar:
    def test_combo_count(self, sweep_html: str) -> None:
        # 3 combos in our test data — should appear in the stats bar.
        assert ">3</span>" in sweep_html

    def test_liquidated_count(self, sweep_html: str) -> None:
        # 1 liquidated row.
        assert '<span class="stat-value">1</span>' in sweep_html


class TestExtraColumns:
    def test_extra_columns_included(self, tmp_path: Path) -> None:
        df = _make_sweep_df()
        df["custom_metric"] = [1.5, 2.5, 3.5]
        out = generate_sweep_html(
            df, output_dir=tmp_path, filename="extra",
            extra_columns=["custom_metric"],
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>custom_metric</th>" in text
