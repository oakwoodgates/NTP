"""Unit tests for notebook helper functions in ``notebooks/utils.py``.

Focuses on the testable pieces — the ones with computation logic.
The pure-print helpers (``print_setup_summary``, etc.) are smoke-tested
to ensure they don't error on representative inputs.
"""

from __future__ import annotations

import io
import math
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

# notebooks/ is not a package — same trick the test files use.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from utils import (  # type: ignore[import-not-found] # noqa: E402
    baselines_for_strategy,
    print_baselines_verdict,
    print_liquidation_resolution,
    print_sweep_liquidation_diagnostics,
    save_notebook_snapshot,
)

# ── Mock objects ─────────────────────────────────────────────────────────────


NS_PER_DAY = 86_400_000_000_000
BASE_NS = 1_672_531_200_000_000_000  # 2023-01-01 UTC


@dataclass
class _MockClose:
    _v: float
    def as_double(self) -> float:
        return self._v


@dataclass
class _MockBar:
    ts_event: int
    close: _MockClose


@dataclass
class _MockMoney:
    _v: Decimal
    def as_decimal(self) -> Decimal:
        return self._v


class _MockPosition:
    def __init__(self, ts_open: int, ts_close: int, pnl: float = 0.0):
        self.is_closed = True
        self.ts_opened = ts_open
        self.ts_closed = ts_close
        self.realized_pnl = _MockMoney(Decimal(str(pnl)))


def _make_bars(n: int) -> list[_MockBar]:
    return [
        _MockBar(BASE_NS + i * NS_PER_DAY, _MockClose(50000.0 + i * 100))
        for i in range(n)
    ]


# ── baselines_for_strategy ───────────────────────────────────────────────────


class TestBaselinesForStrategyShape:
    def test_returns_required_keys(self) -> None:
        positions = [_MockPosition(BASE_NS, BASE_NS + 5 * NS_PER_DAY, 100.0)]
        bars = _make_bars(30)
        out = baselines_for_strategy(
            positions, bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0,
            fee_rate=0.0005,
            leverage=20.0,
            n_simulations=10, random_seed=1,
        )
        expected = {
            "buy_and_hold", "buy_and_hold_leveraged", "random_entry",
            "n_trades", "avg_duration_bars",
        }
        assert expected.issubset(out.keys())

    def test_n_trades_extracted(self) -> None:
        positions = [
            _MockPosition(BASE_NS + i * NS_PER_DAY,
                          BASE_NS + (i + 5) * NS_PER_DAY, 50.0)
            for i in range(3)
        ]
        bars = _make_bars(30)
        out = baselines_for_strategy(
            positions, bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
            n_simulations=5, random_seed=1,
        )
        assert out["n_trades"] == 3

    def test_avg_duration_in_bars(self) -> None:
        # Two trades, each 5 bars long.
        positions = [
            _MockPosition(BASE_NS, BASE_NS + 5 * NS_PER_DAY, 0.0),
            _MockPosition(BASE_NS + 10 * NS_PER_DAY,
                          BASE_NS + 15 * NS_PER_DAY, 0.0),
        ]
        bars = _make_bars(30)
        out = baselines_for_strategy(
            positions, bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
            n_simulations=5, random_seed=1,
        )
        assert out["avg_duration_bars"] == pytest.approx(5.0)

    def test_random_entry_present_with_trades(self) -> None:
        positions = [_MockPosition(BASE_NS, BASE_NS + 5 * NS_PER_DAY, 50.0)]
        bars = _make_bars(30)
        out = baselines_for_strategy(
            positions, bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
            n_simulations=20, random_seed=1,
        )
        assert out["random_entry"] is not None
        assert "median_pnl" in out["random_entry"]


class TestBaselinesForStrategyEdgeCases:
    def test_no_positions_returns_none_random(self) -> None:
        bars = _make_bars(30)
        out = baselines_for_strategy(
            [], bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
        )
        assert out["random_entry"] is None
        assert out["n_trades"] == 0
        assert math.isnan(out["avg_duration_bars"])
        # B&H still computed
        assert "pnl" in out["buy_and_hold"]

    def test_single_bar_returns_none_random(self) -> None:
        positions = [_MockPosition(BASE_NS, BASE_NS, 0.0)]
        bars = _make_bars(1)
        out = baselines_for_strategy(
            positions, bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
        )
        assert out["random_entry"] is None

    def test_leverage_doubles_lev_pnl_relative_to_spot(self) -> None:
        bars = _make_bars(30)
        out = baselines_for_strategy(
            [], bars,
            starting_capital=10_000.0,
            notional_per_trade=2000.0, fee_rate=0.0,
            leverage=2.0,
        )
        assert out["buy_and_hold_leveraged"]["pnl"] == pytest.approx(
            2.0 * out["buy_and_hold"]["pnl"],
        )


# ── print_baselines_verdict ──────────────────────────────────────────────────


class TestPrintBaselinesVerdict:
    def test_runs_without_error(self) -> None:
        baselines = {
            "buy_and_hold": {
                "pnl": 1000.0, "pnl_pct": 10.0,
                "max_drawdown_pct": 0.20, "cagr": 0.15,
                "years_in_sample": 1.5,
            },
            "buy_and_hold_leveraged": {
                "pnl": 20000.0, "max_drawdown_pct": 4.0,
            },
            "random_entry": {
                "median_pnl": 500.0, "pct_5": -200.0, "pct_95": 1500.0,
            },
            "n_trades": 25, "avg_duration_bars": 5.0,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_baselines_verdict(
                baselines, strategy_pnl=2000.0, leverage=20, currency="USDC",
            )
        output = buf.getvalue()
        assert "Buy & Hold" in output
        assert "Strategy" in output
        assert "BEATS" in output  # 2000 > 1000
        assert "Random entry" in output

    def test_loses_to_when_strategy_underperforms(self) -> None:
        baselines = {
            "buy_and_hold": {
                "pnl": 5000.0, "pnl_pct": 50.0,
                "max_drawdown_pct": 0.20, "cagr": 0.30,
                "years_in_sample": 1.0,
            },
            "buy_and_hold_leveraged": {"pnl": 100000, "max_drawdown_pct": 5.0},
            "random_entry": None,
            "n_trades": 0, "avg_duration_bars": float("nan"),
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_baselines_verdict(
                baselines, strategy_pnl=1000.0, leverage=20, currency="USDC",
            )
        assert "LOSES TO" in buf.getvalue()


# ── print_liquidation_resolution ─────────────────────────────────────────────


class TestPrintLiquidationResolution:
    def test_disabled_message(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_liquidation_resolution(None, leverage=20)
        assert "disabled" in buf.getvalue()

    def test_enabled_block(self) -> None:
        @dataclass
        class _Liq:
            enabled: bool = True
            mm_rate: Decimal = Decimal("0.005")
            fee_rate: Decimal = Decimal("0.00035")
            min_trade_notional: Decimal = Decimal("2000")
            alive_trades_buffer: int = 1
            halt_on_account_liquidation: bool = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_liquidation_resolution(_Liq(), leverage=20)
        out = buf.getvalue()
        assert "ENABLED" in out
        assert "0.005" in out
        assert "alive threshold" in out


# ── print_sweep_liquidation_diagnostics ──────────────────────────────────────


def _make_sweep_df(
    n_combos: int = 6, *, with_liq: int = 2,
) -> pd.DataFrame:
    """Build a synthetic sweep DataFrame with a mix of healthy + liquidated rows."""
    rows: list[dict[str, object]] = []
    # Healthy combos
    for i in range(n_combos - with_liq):
        rows.append({
            "fast": 5 + i, "slow": 20 + i,
            "total_pnl": 1000.0 + i * 100,
            "num_positions": 50 + i * 5,
            "min_balance": 800.0,
            "final_balance": 11000.0,
            "total_fees": 70.0,
            "liquidated_positions": 0,
            "liquidated_account": False,
            "liquidated_at_ts": None,
            "denied_post_halt": 0,
            "liq_slippage_avg_pct": float("nan"),
            "liq_slippage_max_pct": float("nan"),
        })
    # Liquidated combos
    for i in range(with_liq):
        rows.append({
            "fast": 40 + i, "slow": 50 + i,
            "total_pnl": -990.0,
            "num_positions": 3,
            "min_balance": -50.0,
            "final_balance": -50.0,
            "total_fees": 4.0,
            "liquidated_positions": 1,
            "liquidated_account": True,
            "liquidated_at_ts": "2023-06-01T00:00:00",
            "denied_post_halt": 5,
            "liq_slippage_avg_pct": 0.0,
            "liq_slippage_max_pct": 0.0,
        })
    return pd.DataFrame(rows)


@dataclass
class _LiqCfg:
    enabled: bool = True
    fee_rate: Decimal = Decimal("0.00035")


class TestSweepLiquidationDiagnostics:
    def test_disabled_short_circuits(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sweep_liquidation_diagnostics(
                pd.DataFrame(),
                liq_resolved=None,
                trade_notional=2000,
            )
        assert "Liquidation simulation off" in buf.getvalue()

    def test_summary_counts(self) -> None:
        df = _make_sweep_df(n_combos=6, with_liq=2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sweep_liquidation_diagnostics(
                df, liq_resolved=_LiqCfg(), trade_notional=2000,
            )
        out = buf.getvalue()
        assert "Total combos          : 6" in out
        assert "With position liq     : 2" in out
        assert "With account liq      : 2" in out
        assert "With denied post-halt : 2" in out

    def test_consistency_check_passes_for_clean_data(self) -> None:
        df = _make_sweep_df()
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sweep_liquidation_diagnostics(
                df, liq_resolved=_LiqCfg(), trade_notional=2000,
            )
        out = buf.getvalue()
        assert "min_balance / liquidated_account consistent" in out

    def test_inconsistency_flagged(self) -> None:
        df = _make_sweep_df()
        # Add a row that breaks consistency — min_balance < 0 but
        # liquidated_account=False
        df = pd.concat([df, pd.DataFrame([{
            **df.iloc[0].to_dict(),
            "min_balance": -100.0,
            "liquidated_account": False,
            "liquidated_positions": 0,
        }])], ignore_index=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sweep_liquidation_diagnostics(
                df, liq_resolved=_LiqCfg(), trade_notional=2000,
            )
        out = buf.getvalue()
        assert "actor missed equity breach" in out

    def test_fee_cross_check_runs(self) -> None:
        df = _make_sweep_df()
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_sweep_liquidation_diagnostics(
                df, liq_resolved=_LiqCfg(), trade_notional=2000,
            )
        out = buf.getvalue()
        assert "Fee model cross-check" in out
        assert "Ratio actual/expected" in out


# ── save_notebook_snapshot ──────────────────────────────────────────────────


def _stub_savers(monkeypatch: pytest.MonkeyPatch, return_path: Path) -> dict[str, bool]:
    """Replace save_notebook + save_notebook_html with no-op stubs.

    Returns a dict tracking whether each was called.  Both stubs return
    *return_path* (callers usually pass the test's nb_path).
    """
    import utils as _utils
    called: dict[str, bool] = {"save": False, "html": False}

    def stub_save(*_args: object, **_kwargs: object) -> Path:
        called["save"] = True
        return return_path

    def stub_html(*_args: object, **_kwargs: object) -> Path:
        called["html"] = True
        return return_path

    monkeypatch.setattr(_utils, "save_notebook", stub_save)
    monkeypatch.setattr(_utils, "save_notebook_html", stub_html)
    return called


class TestSaveNotebookSnapshotMissingFile:
    def test_returns_none_with_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = save_notebook_snapshot(
            str(tmp_path / "does_not_exist.ipynb"),
            "test_result",
            save_on_run_all=True,
        )
        assert result is None
        captured = capsys.readouterr()
        assert "not found" in captured.out


class TestSaveNotebookSnapshotActiveWait:
    """Verifies the active wait exits as soon as the file mtime changes."""

    def test_wait_breaks_on_mtime_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Background thread updates the file mtime; wait should exit early."""
        import os
        import threading
        import time as _time

        nb_path = tmp_path / "fake.ipynb"
        nb_path.write_text("{}", encoding="utf-8")
        os.utime(nb_path, None)  # set mtime to NOW so freshness passes

        called = _stub_savers(monkeypatch, nb_path)

        # Bump the mtime after a short delay (simulates editor autosave).
        def bump() -> None:
            _time.sleep(0.4)
            os.utime(nb_path, None)

        t = threading.Thread(target=bump)
        t.start()

        start = _time.time()
        result = save_notebook_snapshot(
            str(nb_path), "test_result",
            save_on_run_all=True, autosave_wait_secs=3.0,
        )
        elapsed = _time.time() - start
        t.join()

        assert result is not None
        # Should have exited the wait shortly after the mtime bump.
        assert 0.3 < elapsed < 1.5
        assert called["save"] and called["html"]


class TestSaveNotebookSnapshotFreshFile:
    def test_fresh_file_saves(
        self, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nb_path = tmp_path / "fresh.ipynb"
        nb_path.write_text("{}", encoding="utf-8")
        called = _stub_savers(monkeypatch, nb_path)

        result = save_notebook_snapshot(
            str(nb_path), "test_result",
            save_on_run_all=True, autosave_wait_secs=0.3,
        )
        assert result is not None
        assert called["save"]
        assert called["html"]
        captured = capsys.readouterr()
        assert "stale" not in captured.out  # fresh → no warning


class TestSaveNotebookSnapshotStaleFile:
    def _make_stale(self, tmp_path: Path) -> Path:
        import os
        import time as _time
        nb_path = tmp_path / "stale.ipynb"
        nb_path.write_text("{}", encoding="utf-8")
        old = _time.time() - 60  # 60s ago — past the 30s freshness threshold
        os.utime(nb_path, (old, old))
        return nb_path

    def test_stale_with_flag_true_saves_and_warns(
        self, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nb_path = self._make_stale(tmp_path)
        called = _stub_savers(monkeypatch, nb_path)

        result = save_notebook_snapshot(
            str(nb_path), "test_result",
            save_on_run_all=True, autosave_wait_secs=0.3,
        )
        assert result is not None
        assert called["save"] and called["html"]
        captured = capsys.readouterr()
        assert "stale" in captured.out

    def test_stale_with_flag_false_skips(
        self, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nb_path = self._make_stale(tmp_path)
        called = _stub_savers(monkeypatch, nb_path)

        result = save_notebook_snapshot(
            str(nb_path), "test_result",
            save_on_run_all=False, autosave_wait_secs=0.3,
        )
        assert result is None
        assert not called["save"]
        assert not called["html"]
        captured = capsys.readouterr()
        assert "skipped" in captured.out.lower()
        assert "Ctrl+S" in captured.out
