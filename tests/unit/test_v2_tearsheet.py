"""Unit tests for ``charts.generate_v2_tearsheet``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

# notebooks/ is not a package — add to sys.path like real notebook code does.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

# Use a non-interactive matplotlib backend before any chart helpers run.
import matplotlib  # noqa: E402

matplotlib.use("Agg")

from charts import generate_v2_tearsheet  # type: ignore[import-not-found] # noqa: E402

NS_PER_DAY = 86_400_000_000_000
BASE_NS = 1_672_531_200_000_000_000  # 2023-01-01 UTC


# ── Mock NT-position-like objects ──────────────────────────────────────────


@dataclass
class _MockMoney:
    _v: Decimal
    currency: str = "USDC"

    def as_decimal(self) -> Decimal:
        return self._v


@dataclass
class _MockSide:
    name: str


@dataclass
class _MockClose:
    _v: float

    def as_double(self) -> float:
        return self._v


@dataclass
class _MockBar:
    ts_event: int
    close: _MockClose


class _MockPosition:
    def __init__(self, pnl: float, ts_open: int, ts_close: int, side: str = "BUY"):
        self.is_closed = True
        self.realized_pnl = _MockMoney(Decimal(str(pnl)))
        self.ts_opened = ts_open
        self.ts_closed = ts_close
        self.entry = _MockSide(side)

    def commissions(self) -> list[_MockMoney]:
        return [_MockMoney(Decimal("0.70"))]


def _make_positions() -> list[_MockPosition]:
    """5 trades, mix of L/S, profitable overall."""
    return [
        _MockPosition(100.0, BASE_NS + 0 * NS_PER_DAY, BASE_NS + 5 * NS_PER_DAY, "BUY"),
        _MockPosition(-30.0, BASE_NS + 10 * NS_PER_DAY, BASE_NS + 15 * NS_PER_DAY, "SELL"),
        _MockPosition(250.0, BASE_NS + 20 * NS_PER_DAY, BASE_NS + 30 * NS_PER_DAY, "BUY"),
        _MockPosition(-50.0, BASE_NS + 35 * NS_PER_DAY, BASE_NS + 40 * NS_PER_DAY, "SELL"),
        _MockPosition(150.0, BASE_NS + 45 * NS_PER_DAY, BASE_NS + 55 * NS_PER_DAY, "BUY"),
    ]


def _make_bars(n: int = 90) -> list[_MockBar]:
    return [_MockBar(BASE_NS + i * NS_PER_DAY, _MockClose(50000.0 + i * 100)) for i in range(n)]


def _make_account_report() -> pd.DataFrame:
    idx = pd.to_datetime(
        [BASE_NS + i * NS_PER_DAY for i in range(0, 60, 5)],
        unit="ns", utc=True,
    )
    balances = [10000, 10100, 10070, 10070, 10320, 10320,
                10270, 10270, 10420, 10420, 10420, 10420]
    return pd.DataFrame({"total": balances}, index=idx)


@pytest.fixture
def tearsheet_html(tmp_path: Path) -> str:
    out = generate_v2_tearsheet(
        positions=_make_positions(),
        account_report=_make_account_report(),
        bars=_make_bars(),
        starting_capital=10000,
        currency="USDC",
        instrument_label="BTC-USD-PERP",
        bar_interval="1d",
        strategy_label="EMACross(10/40)",
        leverage=20,
        output_dir=tmp_path,
        filename="test",
    )
    return str(out.read_text(encoding="utf-8"))


# ── Structural ───────────────────────────────────────────────────────────────


class TestStructure:
    def test_has_html_skeleton(self, tearsheet_html: str) -> None:
        assert "<!DOCTYPE html>" in tearsheet_html
        assert "<title>" in tearsheet_html
        assert "</body>" in tearsheet_html

    def test_strategy_and_instrument_in_header(self, tearsheet_html: str) -> None:
        assert "EMACross(10/40)" in tearsheet_html
        assert "BTC-USD-PERP" in tearsheet_html
        assert "1d" in tearsheet_html

    def test_has_metrics_grid(self, tearsheet_html: str) -> None:
        assert "card-label" in tearsheet_html
        assert "Total PnL" in tearsheet_html
        assert "Trades" in tearsheet_html
        assert "Profit Factor" in tearsheet_html
        assert "Max DD %" in tearsheet_html

    def test_has_caveat_footer(self, tearsheet_html: str) -> None:
        assert "ANALYZER_RETURNS_CAVEAT" in tearsheet_html

    def test_charts_embedded_as_base64(self, tearsheet_html: str) -> None:
        # At minimum the equity curve and trade-distribution PNGs.
        assert tearsheet_html.count("data:image/png;base64") >= 2


# ── Trustworthy-only enforcement ─────────────────────────────────────────────


class TestNoForbiddenStats:
    """The whole point of v2 — make sure none of the broken stats appear."""

    @pytest.mark.parametrize("forbidden", [
        "Sharpe Ratio (252",
        "Sortino Ratio",
        "Returns Volatility",
        "Risk Return Ratio",
        "rolling_sharpe",
        "Monthly Returns",
        "Returns Distribution",
    ])
    def test_no_unreliable_stat_in_card(
        self, tearsheet_html: str, forbidden: str,
    ) -> None:
        # The footer mentions these names in the disclaimer; the rest of
        # the document must not show them as live metric values.
        # Locate the footer position and check anything before it.
        footer_start = tearsheet_html.find("<footer")
        assert footer_start > 0
        body_before_footer = tearsheet_html[:footer_start]
        assert forbidden not in body_before_footer


# ── Liquidation banner ───────────────────────────────────────────────────────


class TestLiquidationBanner:
    def test_no_banner_when_not_liquidated(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path, filename="ok",
        )
        text = out.read_text(encoding="utf-8")
        assert "ACCOUNT LIQUIDATED" not in text

    def test_banner_when_liquidated(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            liquidated=True,
            liquidated_at="2023-02-15T12:00:00",
            output_dir=tmp_path, filename="liq",
        )
        text = out.read_text(encoding="utf-8")
        assert "ACCOUNT LIQUIDATED" in text
        assert "2023-02-15T12:00:00" in text


# ── Optional sections ────────────────────────────────────────────────────────


class TestOptionalSections:
    def test_yearly_section_appears_when_provided(self, tmp_path: Path) -> None:
        yearly = pd.DataFrame({
            "pnl": [420.0], "pnl_pct": [4.2], "num_positions": [5],
            "win_rate": [0.6], "avg_winner": [166.67], "avg_loser": [-40.0],
            "profit_factor": [5.0], "avg_duration_hours": [120.0],
            "largest_win": [250.0], "largest_loss": [-50.0],
        }, index=[2023])
        yearly.index.name = "year"
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            yearly_df=yearly,
            output_dir=tmp_path, filename="yr",
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>Year</th>" in text
        assert "2023" in text

    def test_yearly_section_absent_when_omitted(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path, filename="noyr",
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>Year</th>" not in text

    def test_regime_section_when_provided(self, tmp_path: Path) -> None:
        regime = pd.DataFrame({
            "regime": ["TRENDING", "RANGING"],
            "num_positions": [3, 2], "pnl": [500.0, -80.0],
            "win_rate": [0.67, 0.5], "profit_factor": [3.0, 0.5],
            "avg_winner": [200.0, 100.0], "avg_loser": [-50.0, -30.0],
            "avg_duration": [10.0, 5.0],
        })
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            regime_df=regime,
            output_dir=tmp_path, filename="rg",
        )
        text = out.read_text(encoding="utf-8")
        assert "<th>Regime</th>" in text
        assert "TRENDING" in text

    def test_baselines_section_when_provided(self, tmp_path: Path) -> None:
        baselines = {
            "buy_and_hold": {
                "pnl": 800.0, "max_drawdown_pct": 0.10, "cagr": 0.30,
            },
            "random_entry": {
                "median_pnl": 250.0, "pct_5": -100.0, "pct_95": 700.0,
                "n_simulations": 1000,
            },
        }
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            baselines=baselines,
            output_dir=tmp_path, filename="bl",
        )
        text = out.read_text(encoding="utf-8")
        assert "Buy & Hold" in text
        assert "Random entry" in text


# ── Empty inputs ─────────────────────────────────────────────────────────────


class TestEmptyInputs:
    def test_no_positions_renders(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=[],
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path, filename="nopos",
        )
        text = out.read_text(encoding="utf-8")
        # Should still produce a document, just with em-dashes for no-trades.
        assert "<!DOCTYPE html>" in text
        assert "Total PnL" in text

    def test_no_account_report_renders(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=pd.DataFrame(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path, filename="noacct",
        )
        text = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in text
        # The equity chart placeholder should appear when no balance data
        assert "No balance data" in text


# ── Filename behavior ────────────────────────────────────────────────────────


class TestFilenameBehavior:
    def test_default_filename_includes_strategy(self, tmp_path: Path) -> None:
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            strategy_label="EMACross(10/40)",
            output_dir=tmp_path,
        )
        # Slashes get sanitized
        assert "EMACross" in out.name
        assert "/" not in out.name

    def test_custom_stem_appends_timestamp(self, tmp_path: Path) -> None:
        # Stem (no .html) → snapshot mode: appends "_{ts}.html".
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path,
            filename="my_custom_name",
        )
        # File name should start with "my_custom_name_" and end ".html".
        assert out.name.startswith("my_custom_name_")
        assert out.name.endswith(".html")
        # Roughly the timestamp shape "YYYYMMDD_HHMMSS"
        ts_part = out.name[len("my_custom_name_") : -len(".html")]
        assert len(ts_part) == 15
        assert ts_part[8] == "_"

    def test_custom_full_filename_used_verbatim(self, tmp_path: Path) -> None:
        # Full filename ending in .html → deterministic mode (overwrites).
        out = generate_v2_tearsheet(
            positions=_make_positions(),
            account_report=_make_account_report(),
            bars=_make_bars(),
            starting_capital=10000,
            output_dir=tmp_path,
            filename="my_exact_name.html",
        )
        assert out.name == "my_exact_name.html"
