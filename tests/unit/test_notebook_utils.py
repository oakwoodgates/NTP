"""Unit tests for notebook helper functions in ``notebooks/utils.py``.

Focuses on the testable pieces — the ones with computation logic.
The pure-print helpers (``print_setup_summary``, etc.) are smoke-tested
to ensure they don't error on representative inputs.
"""

from __future__ import annotations

import io
import math
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

# Type alias for the ``stub_load_sweeps`` fixture's return value: a
# factory that takes a sweep dict and returns the captured-args dict.
StubLoadSweeps = Callable[[dict[str, pd.DataFrame]], dict[str, Any]]

# notebooks/ is not a package — same trick the test files use.
_NOTEBOOKS_DIR = Path(__file__).resolve().parents[2] / "notebooks"
if str(_NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOTEBOOKS_DIR))

from utils import (  # type: ignore[import-not-found] # noqa: E402
    baselines_for_strategy,
    build_verdict_matrix,
    join_signal_streams,
    load_sweeps_filtered,
    load_verdict_jsons,
    print_baselines_verdict,
    print_liquidation_resolution,
    print_sweep_liquidation_diagnostics,
    print_validation_verdict,
    run_backtest_signal_stream,
    save_notebook_snapshot,
    wilson_score_interval,
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


# ── wilson_score_interval ──────────────────────────────────────────────────


class TestWilsonScoreInterval:
    def test_zero_n_returns_nan_pair(self) -> None:
        lo, hi = wilson_score_interval(0, 0)
        assert math.isnan(lo) and math.isnan(hi)

    def test_bounds_in_unit_interval(self) -> None:
        for w, n in [(0, 5), (5, 5), (3, 10), (50, 100), (1, 1000)]:
            lo, hi = wilson_score_interval(w, n)
            assert 0.0 <= lo <= hi <= 1.0

    def test_small_sample_wider_than_large_sample(self) -> None:
        # 50% point estimate at n=4 should be much wider than at n=100.
        lo_small, hi_small = wilson_score_interval(2, 4)
        lo_big,   hi_big   = wilson_score_interval(50, 100)
        assert (hi_small - lo_small) > (hi_big - lo_big)

    def test_all_wins_upper_bound_is_one(self) -> None:
        lo, hi = wilson_score_interval(5, 5)
        assert hi == pytest.approx(1.0)
        assert lo > 0.0  # but lower is meaningfully > 0

    def test_all_losses_lower_bound_is_zero(self) -> None:
        lo, hi = wilson_score_interval(0, 5)
        assert lo == pytest.approx(0.0)
        assert hi < 1.0

    def test_known_4_50pct_interval(self) -> None:
        # n=4, w=2 (50%) should give roughly [0.15, 0.85] at 95%
        lo, hi = wilson_score_interval(2, 4)
        assert 0.10 < lo < 0.20
        assert 0.80 < hi < 0.90


# ── load_sweeps_filtered ────────────────────────────────────────────────────


def _make_v2_sweep_df(
    *,
    n_healthy: int = 2,
    n_liq: int = 1,
    n_spot: int = 1,
) -> pd.DataFrame:
    """Build a v2-schema sweep DataFrame with healthy / liquidated / spotlight rows."""
    base_meta = {
        "_strategy": "MACross-EMA",
        "_instrument_id": "BTC-USD-PERP.HYPERLIQUID",
        "_bar_interval": "1h",
        "_swept_at": "2025-01-01T00:00:00+00:00",
        "_schema_version": 2,
    }
    rows: list[dict[str, object]] = []
    for i in range(n_healthy):
        rows.append({
            **base_meta, "fast": 5 + i, "slow": 20 + i,
            "total_pnl": 1000.0 + i * 100,
            "liquidated": False, "error": "", "_kind": None,
        })
    for i in range(n_liq):
        rows.append({
            **base_meta, "fast": 40 + i, "slow": 50 + i,
            "total_pnl": -990.0,
            "liquidated": True, "error": "liquidated", "_kind": None,
        })
    for i in range(n_spot):
        rows.append({
            **base_meta, "fast": 9 + i, "slow": 18 + i,
            "total_pnl": 500.0,
            "liquidated": False, "error": "", "_kind": "spotlight",
        })
    return pd.DataFrame(rows)


def _make_v1_sweep_df() -> pd.DataFrame:
    """Build a v1-schema sweep DataFrame (no `liquidated` bool column)."""
    return pd.DataFrame([
        {
            "_strategy": "MACross-EMA",
            "_instrument_id": "BTC-USD-PERP.HYPERLIQUID",
            "_bar_interval": "1h",
            "fast": 5, "slow": 20, "total_pnl": 1000.0,
            "error": "",
        },
        {
            "_strategy": "MACross-EMA",
            "_instrument_id": "BTC-USD-PERP.HYPERLIQUID",
            "_bar_interval": "1h",
            "fast": 40, "slow": 50, "total_pnl": -990.0,
            "error": "liquidated",
        },
    ])


@pytest.fixture
def stub_load_sweeps(monkeypatch: pytest.MonkeyPatch) -> StubLoadSweeps:
    """Replace ``src.backtesting.engine.load_sweeps`` with a controllable stub."""
    import src.backtesting.engine as _engine
    captured: dict[str, Any] = {}

    def _factory(return_value: dict[str, pd.DataFrame]) -> dict[str, Any]:
        def _stub(*args: Any, **kwargs: Any) -> dict[str, pd.DataFrame]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return return_value
        monkeypatch.setattr(_engine, "load_sweeps", _stub)
        return captured

    return _factory


class TestLoadSweepsFilteredV2:
    def test_drops_liquidated_rows(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=3, n_liq=2, n_spot=0)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered()
        assert len(result["sweep_a"]) == 3
        assert not result["sweep_a"]["liquidated"].any()

    def test_drops_spotlight_rows(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=3, n_liq=0, n_spot=2)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered()
        assert len(result["sweep_a"]) == 3
        assert (result["sweep_a"]["_kind"] != "spotlight").all()

    def test_drops_both_when_default(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=2, n_liq=1, n_spot=1)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered()
        assert len(result["sweep_a"]) == 2

    def test_keeps_liquidated_when_disabled(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=2, n_liq=2, n_spot=0)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered(filter_liquidated=False)
        assert len(result["sweep_a"]) == 4

    def test_keeps_spotlight_when_disabled(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=2, n_liq=0, n_spot=2)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered(filter_spotlight=False)
        assert len(result["sweep_a"]) == 4

    def test_keeps_all_when_both_disabled(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=2, n_liq=2, n_spot=2)
        stub_load_sweeps({"sweep_a": df})
        result = load_sweeps_filtered(
            filter_liquidated=False, filter_spotlight=False,
        )
        assert len(result["sweep_a"]) == 6


class TestLoadSweepsFilteredV1Compat:
    def test_v1_falls_back_to_error_column(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v1_sweep_df()  # no `liquidated` column
        stub_load_sweeps({"old_sweep": df})
        result = load_sweeps_filtered()
        assert len(result["old_sweep"]) == 1
        assert (result["old_sweep"]["error"] != "liquidated").all()

    def test_v1_skips_liquidated_filter_when_disabled(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        df = _make_v1_sweep_df()
        stub_load_sweeps({"old_sweep": df})
        result = load_sweeps_filtered(filter_liquidated=False)
        assert len(result["old_sweep"]) == 2


class TestLoadSweepsFilteredEmpty:
    def test_empty_mapping_returned_as_is(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        stub_load_sweeps({})
        result = load_sweeps_filtered()
        assert result == {}


class TestLoadSweepsFilteredForwarding:
    def test_filter_kwargs_forwarded(
        self, stub_load_sweeps: StubLoadSweeps,
    ) -> None:
        captured = stub_load_sweeps({})
        load_sweeps_filtered(
            strategy="MACross-EMA",
            instrument_id="BTC-USD-PERP.HYPERLIQUID",
            bar_interval="1h",
        )
        kwargs = captured["kwargs"]
        assert kwargs["strategy"] == "MACross-EMA"
        assert kwargs["instrument_id"] == "BTC-USD-PERP.HYPERLIQUID"
        assert kwargs["bar_interval"] == "1h"

    def test_sweep_dir_forwarded_positionally(
        self, stub_load_sweeps: StubLoadSweeps, tmp_path: Path,
    ) -> None:
        captured = stub_load_sweeps({})
        load_sweeps_filtered(sweep_dir=tmp_path)
        # When sweep_dir is provided, it goes as the first positional arg.
        assert captured["args"] == (tmp_path,)


class TestLoadSweepsFilteredOutput:
    def test_prints_filter_counts(
        self, stub_load_sweeps: StubLoadSweeps,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        df = _make_v2_sweep_df(n_healthy=2, n_liq=1, n_spot=1)
        stub_load_sweeps({"my_sweep": df})
        load_sweeps_filtered()
        captured = capsys.readouterr()
        assert "my_sweep" in captured.out
        assert "1 liquidated" in captured.out
        assert "1 spotlight" in captured.out
        assert "4 → 2" in captured.out


# ── print_validation_verdict ────────────────────────────────────────────────


def _make_walkforward_df(*, profitable: int, total: int) -> pd.DataFrame:
    """Build a walk-forward results DataFrame.

    First ``profitable`` folds get +1000 OOS PnL, the rest get -500.
    """
    rows = []
    for i in range(profitable):
        rows.append({"fold": i, "oos_pnl": 1000.0})
    for i in range(profitable, total):
        rows.append({"fold": i, "oos_pnl": -500.0})
    return pd.DataFrame(rows)


def _make_walkforward_df_with_params(
    *, picks: list[tuple[int, int]],
) -> pd.DataFrame:
    """Build a walk-forward DataFrame including ``best_fast`` / ``best_slow``.

    Each entry in ``picks`` becomes one fold's chosen combo.  All folds
    are marked profitable so the OOS-PnL check passes (default green).
    """
    rows = []
    for i, (fast, slow) in enumerate(picks):
        rows.append({
            "fold": i, "oos_pnl": 1000.0,
            "best_fast": fast, "best_slow": slow,
        })
    return pd.DataFrame(rows)


def _make_rolling_df(*, profitable: int, total: int) -> pd.DataFrame:
    rows = []
    for i in range(profitable):
        rows.append({"window": i, "pnl": 100.0})
    for i in range(profitable, total):
        rows.append({"window": i, "pnl": -50.0})
    return pd.DataFrame(rows)


def _make_rolling_df_with_inactive(
    *, profitable: int, losing: int, inactive: int,
) -> pd.DataFrame:
    """Rolling DF that includes zero-PnL ('no trade') windows."""
    rows: list[dict[str, Any]] = []
    i = 0
    for _ in range(profitable):
        rows.append({"window": i, "pnl": 100.0})
        i += 1
    for _ in range(losing):
        rows.append({"window": i, "pnl": -50.0})
        i += 1
    for _ in range(inactive):
        rows.append({"window": i, "pnl": 0.0})
        i += 1
    return pd.DataFrame(rows)


def _make_yearly_df(year_pnls: dict[int, float]) -> pd.DataFrame:
    """Build a per-year DataFrame indexed by year (matches performance_by_year)."""
    return pd.DataFrame(
        [{"pnl": pnl, "num_positions": 5, "win_rate": 0.5}
         for pnl in year_pnls.values()],
        index=list(year_pnls.keys()),
    )


def _make_fee_df(*, max_breakeven_bps: float | None) -> pd.DataFrame:
    """Build a fee-sweep DataFrame.  ``max_breakeven_bps=None`` means none break even."""
    rows = []
    for bps in [0.0, 2.5, 5.0, 7.5, 10.0]:
        breakeven = (
            max_breakeven_bps is not None and bps <= max_breakeven_bps
        )
        rows.append({"fee_bps": bps, "breakeven": breakeven})
    return pd.DataFrame(rows)


def _make_regime_df(*, trending_pnl: float, ranging_pnl: float) -> pd.DataFrame:
    return pd.DataFrame([
        {"regime": "TRENDING", "pnl": trending_pnl},
        {"regime": "RANGING", "pnl": ranging_pnl},
    ])


class TestValidationVerdictAllGreen:
    def test_all_checks_pass(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL",
                bar_interval="1h",
                params={"fast": 10, "slow": 20},
                plateau_score=0.85,
                walkforward_results=_make_walkforward_df(profitable=4, total=4),
                bootstrap_prob_positive=92.0,
                bootstrap_p5=200.0,
                bootstrap_p95=2000.0,
                n_trades=50,
                rolling_results=_make_rolling_df(profitable=8, total=10),
                fee_results=_make_fee_df(max_breakeven_bps=10.0),
                regime_results=_make_regime_df(
                    trending_pnl=1500.0, ranging_pnl=-100.0,
                ),
            )
        out = buf.getvalue()
        assert "VALIDATION SUMMARY" in out
        assert "READY for paper trading" in out
        assert "🚩" not in out.split("VERDICT:")[1]
        assert "⚠️" not in out.split("VERDICT:")[1].split("Remember")[0]


class TestValidationVerdictRedFlag:
    def test_failed_checks_trigger_do_not_trade(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL",
                bar_interval="1h",
                params={"fast": 10, "slow": 20},
                plateau_score=0.30,  # 🚩
                walkforward_results=_make_walkforward_df(
                    profitable=1, total=4,
                ),  # 🚩
                bootstrap_prob_positive=40.0,  # 🚩
                n_trades=50,
            )
        out = buf.getvalue()
        assert "DO NOT paper trade yet" in out
        assert "🚩" in out


class TestValidationVerdictWarn:
    def test_partial_passes_yields_proceed_with_caution(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL",
                bar_interval="1h",
                params={"fast": 10, "slow": 20},
                plateau_score=0.65,  # ⚠️
                walkforward_results=_make_walkforward_df(
                    profitable=4, total=4,
                ),  # ✅
            )
        out = buf.getvalue()
        assert "PROCEED WITH CAUTION" in out


class TestValidationVerdictPlateauThresholds:
    def test_plateau_high_is_green(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h",
                params={}, plateau_score=0.8,
            )
        out = buf.getvalue()
        assert "✅ Plateau" in out
        assert "robust region" in out

    def test_plateau_mid_is_warn(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h",
                params={}, plateau_score=0.5,
            )
        out = buf.getvalue()
        assert "⚠️ Plateau" in out
        assert "ridge" in out

    def test_plateau_low_is_red(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h",
                params={}, plateau_score=0.49,
            )
        out = buf.getvalue()
        assert "🚩 Plateau" in out
        assert "isolated spike" in out


class TestValidationVerdictBootstrap:
    def test_too_few_trades_warns(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=95.0, n_trades=3,
            )
        out = buf.getvalue()
        assert "⚠️ Bootstrap" in out
        assert "Only 3 trades" in out

    def test_high_prob_positive_is_green(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=92.0, n_trades=50,
                bootstrap_p5=100.0, bootstrap_p95=2000.0,
            )
        out = buf.getvalue()
        assert "✅ Bootstrap" in out
        assert "P(profit)=92%" in out
        # CI is rendered with thousands separator and zero decimals.
        assert "[100, 2,000]" in out


class TestValidationVerdictBootstrapCapitalThreshold:
    """Capital-relative tail check: ✅ requires pct_5 ≥ 10% of capital."""

    def test_high_prob_with_weak_tail_downgrades_to_warn(self) -> None:
        # P(profit)=95% AND pct_5=$50 — but capital is $1000, so
        # pct_5 < 10% threshold ($100) → ⚠️ not ✅
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=95.0, n_trades=23,
                bootstrap_p5=50.0, bootstrap_p95=26000.0,
                starting_capital=1000.0,
            )
        out = buf.getvalue()
        assert "⚠️ Bootstrap" in out
        assert "pct_5 < 100" in out  # threshold = 10% × 1000 = 100
        assert "10% of capital" in out

    def test_high_prob_with_strong_tail_stays_green(self) -> None:
        # P(profit)=95% AND pct_5=$200 — above 10% × $1000 = $100 → ✅
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=95.0, n_trades=23,
                bootstrap_p5=200.0, bootstrap_p95=26000.0,
                starting_capital=1000.0,
            )
        out = buf.getvalue()
        assert "✅ Bootstrap" in out
        assert "pct_5 <" not in out  # no weak-tail annotation

    def test_no_capital_falls_back_to_legacy_behaviour(self) -> None:
        # starting_capital=None — capital-relative check is skipped,
        # so ✅ is determined by P(profit) alone.
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=95.0, n_trades=23,
                bootstrap_p5=50.0, bootstrap_p95=26000.0,
                # starting_capital not provided
            )
        out = buf.getvalue()
        assert "✅ Bootstrap" in out  # legacy behavior
        assert "10% of capital" not in out

    def test_low_prob_stays_red_regardless_of_capital(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                bootstrap_prob_positive=50.0, n_trades=23,
                bootstrap_p5=10000.0, bootstrap_p95=20000.0,
                starting_capital=1000.0,
            )
        out = buf.getvalue()
        assert "🚩 Bootstrap" in out


class TestValidationVerdictReturnAndPersist:
    def test_returns_dict_with_required_keys(self) -> None:
        result = print_validation_verdict(
            instrument_id="BTC.HL", bar_interval="1h",
            params={"fast": 10, "slow": 20},
        )
        assert result is not None
        for key in (
            "_schema_version", "instrument_id", "bar_interval", "params",
            "checks", "counts", "verdict", "timestamp",
        ):
            assert key in result, f"Missing key: {key}"

    def test_persists_json_when_path_provided(self, tmp_path: Path) -> None:
        target = tmp_path / "verdict.json"
        result = print_validation_verdict(
            instrument_id="BTC.HL", bar_interval="1h",
            params={"fast": 10, "slow": 20},
            plateau_score=1.00,
            verdict_path=target,
        )
        assert target.exists()
        import json
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded["instrument_id"] == "BTC.HL"
        assert loaded["params"] == {"fast": 10, "slow": 20}
        assert loaded["_schema_version"] == 1
        # Returned dict matches written JSON
        assert loaded["instrument_id"] == result["instrument_id"]

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "verdict.json"
        print_validation_verdict(
            instrument_id="BTC.HL", bar_interval="1h", params={},
            verdict_path=target,
        )
        assert target.exists()

    def test_no_path_no_file(self, tmp_path: Path) -> None:
        # Sanity: when verdict_path is None, no file is created
        # anywhere in tmp_path (even though the function still returns
        # the dict).
        result = print_validation_verdict(
            instrument_id="BTC.HL", bar_interval="1h", params={},
        )
        assert result is not None
        assert list(tmp_path.iterdir()) == []

    def test_check_outcomes_are_normalised(self) -> None:
        result = print_validation_verdict(
            instrument_id="BTC.HL", bar_interval="1h", params={},
            plateau_score=1.00,
            walkforward_results=_make_walkforward_df(profitable=1, total=4),
            yearly_results=_make_yearly_df({2020: 800.0, 2021: 200.0}),
        )
        # Each check has an "outcome" string in {"pass", "warn", "fail"}
        for check in result["checks"]:
            assert check["outcome"] in {"pass", "warn", "fail"}
        # And the verdict's outcome string is one of the same set
        assert result["verdict"]["outcome"] in {"pass", "warn", "fail"}


class TestValidationVerdictFee:
    def test_no_breakeven_is_red(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                fee_results=_make_fee_df(max_breakeven_bps=None),
            )
        out = buf.getvalue()
        assert "🚩 Fee sensitivity" in out
        assert "Not profitable at any fee level" in out

    def test_high_breakeven_is_green(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                fee_results=_make_fee_df(max_breakeven_bps=10.0),
            )
        out = buf.getvalue()
        assert "✅ Fee sensitivity" in out
        assert "10.0 bps" in out


class TestValidationVerdictRegime:
    def test_trending_dominates_is_green(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                regime_results=_make_regime_df(
                    trending_pnl=2000.0, ranging_pnl=-100.0,
                ),
            )
        out = buf.getvalue()
        assert "✅ Regime" in out

    def test_no_edge_is_red(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                regime_results=_make_regime_df(
                    trending_pnl=-200.0, ranging_pnl=100.0,
                ),
            )
        out = buf.getvalue()
        assert "🚩 Regime" in out
        assert "no clear edge" in out


class TestValidationVerdictParamStability:
    def test_all_folds_pick_same_combo_is_green(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                walkforward_results=_make_walkforward_df_with_params(
                    picks=[(20, 75), (20, 75), (20, 75), (20, 75)],
                ),
            )
        out = buf.getvalue()
        assert "✅ Param stability" in out
        assert "All 4 folds picked fast=20, slow=75" in out

    def test_three_of_four_folds_same_is_warn(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                walkforward_results=_make_walkforward_df_with_params(
                    picks=[(20, 75), (20, 75), (20, 75), (10, 30)],
                ),
            )
        out = buf.getvalue()
        assert "⚠️ Param stability" in out
        assert "3/4 folds picked fast=20, slow=75" in out

    def test_two_of_four_folds_same_is_drifting_warn(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                walkforward_results=_make_walkforward_df_with_params(
                    picks=[(20, 75), (20, 75), (10, 30), (5, 100)],
                ),
            )
        out = buf.getvalue()
        assert "⚠️ Param stability" in out
        assert "drifting" in out

    def test_all_folds_different_is_red(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                walkforward_results=_make_walkforward_df_with_params(
                    picks=[(5, 30), (10, 75), (20, 100), (75, 200)],
                ),
            )
        out = buf.getvalue()
        assert "🚩 Param stability" in out
        assert "fitting noise" in out
        # And the overall verdict should now flag.
        assert "DO NOT paper trade yet" in out

    def test_no_best_columns_skips_param_check(self) -> None:
        # Plain walkforward DF (no best_* cols) — only the OOS check
        # fires; no Param-stability line appears.
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                walkforward_results=_make_walkforward_df(
                    profitable=4, total=4,
                ),
            )
        out = buf.getvalue()
        assert "Walk-forward" in out
        assert "Param stability" not in out


class TestValidationVerdictRollingActiveWindows:
    def test_active_windows_used_as_denominator(self) -> None:
        # 5 profitable + 3 losing + 6 inactive (zero-PnL).  Old logic
        # would say 5/14 = 36% (would flag 🚩).  New logic says
        # 5/8 active = 63% (✅).
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                rolling_results=_make_rolling_df_with_inactive(
                    profitable=5, losing=3, inactive=6,
                ),
            )
        out = buf.getvalue()
        assert "5/8 active windows profitable (62%)" in out or \
               "5/8 active windows profitable (63%)" in out
        assert "6 no-trade windows excluded" in out
        # Above 60% → ✅
        assert "✅ Rolling" in out

    def test_inactive_count_only_appended_when_present(self) -> None:
        # All windows active → no "no-trade windows excluded" annotation
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                rolling_results=_make_rolling_df_with_inactive(
                    profitable=8, losing=2, inactive=0,
                ),
            )
        out = buf.getvalue()
        assert "no-trade windows excluded" not in out

    def test_zero_active_windows_warns(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                rolling_results=_make_rolling_df_with_inactive(
                    profitable=0, losing=0, inactive=10,
                ),
            )
        out = buf.getvalue()
        assert "⚠️ Rolling" in out
        assert "No active windows" in out


class TestValidationVerdictYearlyConcentration:
    def test_high_concentration_one_year_is_red(self) -> None:
        # 80% of total in 2021 → 🚩
        yearly = _make_yearly_df({2020: 100.0, 2021: 800.0, 2022: 100.0})
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "🚩 Yearly concentration" in out
        assert "2021" in out
        assert "80%" in out
        assert "one-trick pony" in out

    def test_moderate_concentration_is_warn(self) -> None:
        # 60% in one year → ⚠️
        yearly = _make_yearly_df({2020: 200.0, 2021: 600.0, 2022: 200.0})
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "⚠️ Yearly concentration" in out
        assert "heavy single-year skew" in out

    def test_low_concentration_is_green(self) -> None:
        # 40% in top year → ✅
        yearly = _make_yearly_df({2020: 400.0, 2021: 300.0, 2022: 300.0})
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "✅ Yearly concentration" in out

    def test_single_year_warns(self) -> None:
        yearly = _make_yearly_df({2025: 1000.0})
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "⚠️ Yearly concentration" in out
        assert "Only 1 year(s)" in out

    def test_zero_total_pnl_warns(self) -> None:
        # Total PnL == 0 → can't compute share → warn
        yearly = _make_yearly_df({2020: 500.0, 2021: -500.0})
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "⚠️ Yearly concentration" in out
        assert "insufficient" in out

    def test_empty_yearly_df_warns(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h", params={},
                yearly_results=pd.DataFrame(),
            )
        out = buf.getvalue()
        assert "⚠️ Yearly concentration" in out
        assert "No per-year data" in out

    def test_btc_real_run_2021_dominates(self) -> None:
        # Replicate the real BTC validate run: 76% of $11,086 in 2021.
        yearly = _make_yearly_df({
            2020:  -714.0,
            2021:  8490.0,
            2022:  -155.0,
            2023:   755.0,
            2024:  1463.0,
            2025:   474.0,
            2026:   773.0,
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTCUSDT-PERP.BINANCE", bar_interval="1d",
                params={"fast": 20, "slow": 75},
                yearly_results=yearly,
            )
        out = buf.getvalue()
        assert "🚩 Yearly concentration" in out
        assert "2021" in out
        assert "DO NOT paper trade yet" in out  # the verdict triggers


class TestValidationVerdictNoneSkipsCheck:
    def test_all_none_yields_only_header_and_green_verdict(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="BTC.HL", bar_interval="1h",
                params={"fast": 10},
            )
        out = buf.getvalue()
        # Header is always printed.
        assert "VALIDATION SUMMARY" in out
        assert "fast=10" in out
        # No checks → no flags → ready verdict.
        assert "READY for paper trading" in out

    def test_header_includes_instrument_and_interval(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validation_verdict(
                instrument_id="ETH-USD-PERP.HYPERLIQUID",
                bar_interval="4h",
                params={"fast": 5, "slow": 30},
            )
        out = buf.getvalue()
        assert "ETH-USD-PERP.HYPERLIQUID" in out
        assert "4h" in out
        assert "fast=5, slow=30" in out


# ── load_verdict_jsons + build_verdict_matrix ───────────────────────────────


def _write_fake_verdict(
    path: Path, *,
    instrument: str = "BTC.HL",
    interval: str = "1d",
    params: dict[str, Any] | None = None,
    is_override: bool = False,
    timestamp: str = "2026-05-07T22:55:00+00:00",
    verdict_icon: str = "🚩",
) -> None:
    import json
    params = params or {"fast": 10, "slow": 20}
    data = {
        "_schema_version": 1,
        "instrument_id": instrument,
        "bar_interval": interval,
        "params": params,
        # New v1-extended field — explicit override marker.  Keep it
        # present (None or dict) on every fake verdict so the matrix's
        # primary "auto vs override" path is exercised, not the legacy
        # filename-suffix fallback.
        "override_params": dict(params) if is_override else None,
        "starting_capital": 1000,
        "checks": [
            {"icon": "✅", "name": "Plateau", "detail": "x", "outcome": "pass"},
            {"icon": "⚠️", "name": "Walk-forward", "detail": "x", "outcome": "warn"},
            {"icon": "🚩", "name": "Param stability", "detail": "x", "outcome": "fail"},
        ],
        "counts": {"pass": 1, "warn": 1, "fail": 1},
        "verdict": {"icon": verdict_icon, "outcome": "fail", "summary": "x"},
        "timestamp": timestamp,
    }
    suffix = (
        "_" + "_".join(f"{k}{v}" for k, v in params.items())
        if is_override else ""
    )
    fn = f"validate_x_{instrument}_{interval}{suffix}_verdict.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Rename to canonical filename so build_verdict_matrix's
    # filename-suffix heuristic picks up the override tag.
    (path.parent / fn).write_text(
        json.dumps(data, indent=2), encoding="utf-8",
    )


class TestLoadVerdictJsons:
    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_verdict_jsons(tmp_path) == []

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_verdict_jsons(tmp_path / "doesntexist") == []

    def test_loads_and_sorts_by_timestamp_desc(self, tmp_path: Path) -> None:
        for i, ts in enumerate([
            "2026-05-07T10:00:00+00:00",
            "2026-05-07T15:00:00+00:00",
            "2026-05-07T12:00:00+00:00",
        ]):
            _write_fake_verdict(
                tmp_path / f"v{i}_verdict.json", timestamp=ts,
            )
        verdicts = load_verdict_jsons(tmp_path)
        timestamps = [v["timestamp"] for v in verdicts]
        # Newest first — but we wrote duplicates via _write_fake_verdict
        # (which writes BOTH the requested file AND the canonical
        # filename), so dedupe before checking order.
        unique = sorted(set(timestamps), reverse=True)
        assert unique == [
            "2026-05-07T15:00:00+00:00",
            "2026-05-07T12:00:00+00:00",
            "2026-05-07T10:00:00+00:00",
        ]

    def test_skips_malformed_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "broken_verdict.json").write_text(
            "{not valid json", encoding="utf-8",
        )
        _write_fake_verdict(tmp_path / "good_verdict.json")
        verdicts = load_verdict_jsons(tmp_path)
        # Good file loaded, broken file skipped with a warning
        assert len(verdicts) >= 1
        assert "Skipping broken_verdict" in capsys.readouterr().out

    def test_tags_source_filename(self, tmp_path: Path) -> None:
        _write_fake_verdict(tmp_path / "test_verdict.json")
        verdicts = load_verdict_jsons(tmp_path)
        assert all("_source" in v for v in verdicts)


class TestBuildVerdictMatrix:
    def test_empty_returns_empty(self) -> None:
        df = build_verdict_matrix([])
        assert df.empty

    def test_columns_in_expected_order(self, tmp_path: Path) -> None:
        _write_fake_verdict(tmp_path / "v_verdict.json")
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        cols = list(df.columns)
        # First 3 + last 2 columns are stable
        assert cols[0] == "instrument"
        assert cols[1] == "interval"
        assert cols[2] == "pick"
        assert cols[-2] == "verdict"
        assert cols[-1] == "timestamp"
        # Check columns sit between
        assert "Plateau" in cols
        assert "Walk-forward" in cols
        assert "Param stability" in cols

    def test_distinguishes_auto_from_override(self, tmp_path: Path) -> None:
        # Two runs: one auto (no override suffix), one override
        _write_fake_verdict(
            tmp_path / "validate_x_BTC.HL_1d_verdict.json",
            params={"fast": 20, "slow": 75}, is_override=False,
            timestamp="2026-05-07T22:00:00+00:00",
        )
        _write_fake_verdict(
            tmp_path / "validate_x_BTC.HL_1d_fast10_slow20_verdict.json",
            params={"fast": 10, "slow": 20}, is_override=True,
            timestamp="2026-05-07T22:30:00+00:00",
        )
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        picks = set(df["pick"].tolist())
        assert "auto" in picks
        assert any("fast=10" in p for p in picks)

    def test_check_columns_carry_icons(self, tmp_path: Path) -> None:
        _write_fake_verdict(tmp_path / "v_verdict.json")
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        # Every cell in a check column is an icon (✅/⚠️/🚩) or empty
        for cell in df["Plateau"].dropna().tolist():
            assert cell in {"✅", "⚠️", "🚩", ""}

    def test_uses_override_params_field_over_filename(
        self, tmp_path: Path,
    ) -> None:
        # Filename has no override suffix, but override_params field
        # is set — should still mark as override (new schema path).
        import json
        data = {
            "_schema_version": 1,
            "instrument_id": "BTC.HL", "bar_interval": "1d",
            "params": {"fast": 10, "slow": 20},
            "override_params": {"fast": 10, "slow": 20},
            "starting_capital": 1000,
            "checks": [
                {"icon": "✅", "name": "Plateau", "detail": "x", "outcome": "pass"},
            ],
            "counts": {"pass": 1, "warn": 0, "fail": 0},
            "verdict": {"icon": "✅", "outcome": "pass", "summary": "x"},
            "timestamp": "2026-05-07T22:00:00+00:00",
        }
        # Note: filename is the AUTO-pick form (no override suffix)
        (tmp_path / "validate_x_BTC.HL_1d_verdict.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8",
        )
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        # Should be marked as override despite the auto-style filename
        assert any("fast=10" in p for p in df["pick"].tolist())

    def test_legacy_filename_fallback_for_pre_field_jsons(
        self, tmp_path: Path,
    ) -> None:
        # Old-format JSON without override_params field — falls back
        # to filename-suffix detection.
        import json
        data = {
            "_schema_version": 1,
            "instrument_id": "BTC.HL", "bar_interval": "1d",
            "params": {"fast": 10, "slow": 20},
            # NO override_params field — pre-schema
            "starting_capital": 1000,
            "checks": [
                {"icon": "✅", "name": "Plateau", "detail": "x", "outcome": "pass"},
            ],
            "counts": {"pass": 1, "warn": 0, "fail": 0},
            "verdict": {"icon": "✅", "outcome": "pass", "summary": "x"},
            "timestamp": "2026-05-07T22:00:00+00:00",
        }
        # Filename has the override suffix → fallback heuristic picks up
        (tmp_path / "validate_x_BTC.HL_1d_fast10_slow20_verdict.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8",
        )
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        # Legacy path should detect override via filename
        assert any("fast=10" in p for p in df["pick"].tolist())

    def test_legacy_filename_fallback_with_short_tag(
        self, tmp_path: Path,
    ) -> None:
        # New short-tag filenames (_f10_s20 instead of _fast10_slow20)
        # also need to be picked up by the legacy fallback path for
        # pre-field JSONs.
        import json
        data = {
            "_schema_version": 1,
            "instrument_id": "BTC.HL", "bar_interval": "1d",
            "params": {"fast": 10, "slow": 20},
            "starting_capital": 1000,
            "checks": [
                {"icon": "✅", "name": "Plateau", "detail": "x", "outcome": "pass"},
            ],
            "counts": {"pass": 1, "warn": 0, "fail": 0},
            "verdict": {"icon": "✅", "outcome": "pass", "summary": "x"},
            "timestamp": "2026-05-07T22:00:00+00:00",
        }
        (tmp_path / "validate_x_BTC.HL_1d_f10_s20_verdict.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8",
        )
        verdicts = load_verdict_jsons(tmp_path)
        df = build_verdict_matrix(verdicts)
        assert any("fast=10" in p for p in df["pick"].tolist())


# ── Phase 2.5 signal-stream helpers ──────────────────────────────────────────


def _build_synthetic_bars_and_instrument() -> tuple[Any, list[Any]]:
    """Build the same synthetic bar series used by the cross-gate tests.

    120 daily bars over a price-oscillation pattern (down → up → down)
    that guarantees a small finite number of MA crosses inside the run
    window. Used by every test below — keep the bar list small and
    deterministic so the engine.run() inside ``run_backtest_signal_stream``
    completes in a second or two.
    """
    from datetime import UTC as _UTC
    from datetime import datetime

    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    from src.core import bar_type_str

    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type_string = bar_type_str(str(instrument.id), "1d")
    bar_type = BarType.from_str(bar_type_string)

    px_prec = int(instrument.price_precision)
    qty_prec = int(instrument.size_precision)
    px_fmt = f"{{:.{px_prec}f}}"
    qty_fmt = f"{{:.{qty_prec}f}}"

    closes = (
        [100.0 - i for i in range(30)]
        + [70.0 + i for i in range(60)]
        + [130.0 - i for i in range(30)]
    )
    base_ns = int(datetime(2024, 1, 1, tzinfo=_UTC).timestamp() * 1e9)
    one_day_ns = 86_400 * 10**9
    bars = []
    for i, c in enumerate(closes):
        ts = base_ns + i * one_day_ns
        bars.append(Bar(
            bar_type,
            Price.from_str(px_fmt.format(c - 0.5)),
            Price.from_str(px_fmt.format(c + 1.0)),
            Price.from_str(px_fmt.format(c - 1.0)),
            Price.from_str(px_fmt.format(c)),
            Quantity.from_str(qty_fmt.format(1.0)),
            ts, ts,
        ))
    return instrument, bars


class TestRunBacktestSignalStream:
    """``run_backtest_signal_stream`` should produce one row per bar after warmup."""

    def test_produces_dense_per_bar_stream(self) -> None:
        from src.core import get_venue_config

        instrument, bars = _build_synthetic_bars_and_instrument()
        venue_config = get_venue_config("HYPERLIQUID_PERP")

        df = run_backtest_signal_stream(
            instrument=instrument,
            bars=bars,
            venue_config=venue_config,
            fast_period=3,
            slow_period=5,
            ma_type="EMA",
            leverage=1,
        )

        # Schema contract — same columns as load_signal_events.
        assert list(df.columns) == [
            "ts", "signal", "fast_value", "slow_value", "acted", "bootstrap",
        ]
        # 120 bars, slow=5 → expect ~100+ emits after warmup.
        assert len(df) >= 100, f"got {len(df)} rows, expected ≥100 after warmup"
        # All signals are +1 or -1 (never 0 — that's the initial state).
        assert set(df["signal"].unique()).issubset({1, -1})
        # acted=True bars are a small fraction (one per cross transition).
        assert df["acted"].sum() < 25, (
            f"got {df['acted'].sum()} acted bars — gate may be mis-applied"
        )

    def test_empty_bars_returns_empty_frame_with_schema(self) -> None:
        from nautilus_trader.test_kit.providers import TestInstrumentProvider

        from src.core import get_venue_config

        instrument = TestInstrumentProvider.btcusdt_perp_binance()
        venue_config = get_venue_config("HYPERLIQUID_PERP")

        df = run_backtest_signal_stream(
            instrument=instrument,
            bars=[],
            venue_config=venue_config,
            fast_period=3,
            slow_period=5,
        )
        # Empty result must still have the right columns so downstream
        # joins don't blow up on KeyError.
        assert list(df.columns) == [
            "ts", "signal", "fast_value", "slow_value", "acted", "bootstrap",
        ]
        assert df.empty


class TestJoinSignalStreams:
    """Dense-join paper × backtest by bar timestamp."""

    def _make_stream(
        self,
        signals: list[tuple[str, int, float, float, bool]],
    ) -> pd.DataFrame:
        """Build a tiny signal stream from a list of (ts, sig, fast, slow, acted)."""
        return pd.DataFrame([
            {
                "ts": pd.Timestamp(ts, tz="UTC"),
                "signal": sig,
                "fast_value": Decimal(str(fast)),
                "slow_value": Decimal(str(slow)),
                "acted": acted,
                "bootstrap": False,
            }
            for ts, sig, fast, slow, acted in signals
        ])

    def test_perfect_alignment_zero_divergent(self) -> None:
        """When paper and backtest agree on every bar, divergent count = 0."""
        signals = [
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.1, 99.5, False),
            ("2024-01-01T08:00", -1, 99.0, 100.0, True),
        ]
        paper = self._make_stream(signals)
        backtest = self._make_stream(signals)

        joined = join_signal_streams(paper, backtest)

        assert len(joined) == 3
        assert joined["divergent"].sum() == 0
        assert joined.attrs["paper_only_bars"] == 0
        assert joined.attrs["bt_only_bars"] == 0
        assert list(joined.columns) == [
            "ts",
            "paper_signal", "paper_acted", "paper_fast", "paper_slow",
            "bt_signal", "bt_acted", "bt_fast", "bt_slow",
            "divergent",
        ] or set(joined.columns) >= {
            "ts", "paper_signal", "bt_signal", "paper_fast", "bt_fast",
            "paper_slow", "bt_slow", "paper_acted", "bt_acted", "divergent",
        }

    def test_disagreement_marks_divergent(self) -> None:
        """A single bar where paper and backtest disagree flips ``divergent``."""
        paper = self._make_stream([
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.1, 99.5, False),  # paper says LONG
        ])
        backtest = self._make_stream([
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", -1, 99.5, 100.1, True),  # backtest says SHORT
        ])

        joined = join_signal_streams(paper, backtest)

        assert len(joined) == 2
        assert joined.loc[0, "divergent"] is False or joined.loc[0, "divergent"] == False  # noqa: E712
        assert joined.loc[1, "divergent"] is True or joined.loc[1, "divergent"] == True  # noqa: E712

    def test_unmatched_bars_surface_on_attrs(self) -> None:
        """Bars present in only one stream show up as paper_only / bt_only counts."""
        paper = self._make_stream([
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.1, 99.5, False),
            ("2024-01-01T08:00", 1, 100.2, 99.7, False),  # only in paper
        ])
        backtest = self._make_stream([
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.1, 99.5, False),
            ("2024-01-01T12:00", -1, 99.0, 100.0, True),  # only in bt
        ])

        joined = join_signal_streams(paper, backtest)

        assert len(joined) == 2  # only common ts kept
        assert joined.attrs["paper_only_bars"] == 1
        assert joined.attrs["bt_only_bars"] == 1

    def test_one_bar_lag_pattern_detected(self) -> None:
        """The "paper lags backtest by 1 bar" pattern produces a known divergent count.

        Simulates Phase 2.5 finding "paper sees the cross 1 bar later than
        backtest" — paper's bar-N signal == backtest's bar-(N-1) signal.
        """
        bt_signals = [
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.0, 99.0, False),
            ("2024-01-01T08:00", -1, 99.0, 100.0, True),   # backtest flips here
            ("2024-01-01T12:00", -1, 99.0, 100.0, False),
        ]
        # Paper is 1 bar late on the flip — still long at 08:00.
        paper_signals = [
            ("2024-01-01T00:00", 1, 100.0, 99.0, True),
            ("2024-01-01T04:00", 1, 100.0, 99.0, False),
            ("2024-01-01T08:00", 1, 100.0, 99.5, False),    # still long
            ("2024-01-01T12:00", -1, 99.0, 100.0, True),    # flips one bar later
        ]
        joined = join_signal_streams(
            self._make_stream(paper_signals),
            self._make_stream(bt_signals),
        )
        assert len(joined) == 4
        assert joined["divergent"].sum() == 1
        # The single divergence is at 08:00 — the bar where backtest already
        # flipped but paper hasn't seen it yet.
        divergent_ts = joined[joined["divergent"]]["ts"].iloc[0]
        assert divergent_ts == pd.Timestamp("2024-01-01T08:00", tz="UTC")
