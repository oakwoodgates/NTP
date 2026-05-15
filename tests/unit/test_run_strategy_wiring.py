"""Wiring tests for ``scripts/run_sandbox.py`` and ``scripts/run_live.py``.

Verifies the ``_build_strategy`` helper threads risk-management knobs from
``Settings`` into the ``MACrossConfig`` it constructs. Without this wiring
the protective-stop mixin never arms a stop in paper/live, even if
``STOP_PCT`` is set in `.env`.

These are pure construction tests — no engine, no msgbus. The strategy
object's ``.config`` attribute exposes the frozen msgspec.Struct we built.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# scripts/ is not a package on disk; add it to sys.path so we can import
# `run_sandbox` and `run_live` as modules. Same pattern other notebook
# helper tests use.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402

from src.config.settings import Settings  # noqa: E402
from src.core import bar_type_str  # noqa: E402


def _build_settings(**overrides: Any) -> Settings:
    """Minimal Settings constructor with `.env` disabled.

    Forces every value to come from defaults + the override kwargs, so the
    test doesn't accidentally pick up the developer's local `.env`. The
    ``Any`` typed overrides let callers pass whatever Settings field
    they need to flip (`stop_pct=0.05`, `ma_fast=20`, etc.) without
    each call site re-typing the parameter union.
    """
    # `_env_file` is a runtime kwarg from pydantic-settings — not in the
    # typed signature, so mypy needs a single targeted suppression.
    return Settings(_env_file=None, postgres_password="test", **overrides)  # type: ignore[call-arg]


def _instrument_and_bar_type() -> tuple[InstrumentId, BarType]:
    instrument_id_str = "BTC-USD-PERP.HYPERLIQUID"
    instrument_id = InstrumentId.from_str(instrument_id_str)
    bar_type = BarType.from_str(bar_type_str(instrument_id_str, "4h"))
    return instrument_id, bar_type


# ── Tests against scripts/run_sandbox.py ───────────────────────────────────


class TestRunSandboxStopPctWiring:
    """``run_sandbox._build_strategy`` must thread ``settings.stop_pct``
    into ``MACrossConfig.stop_pct`` and into the persisted config dict.

    Without this, the protective-stop mixin defaults to ``stop_pct=None``
    and never arms a stop in sandbox mode, regardless of what's in `.env`.
    """

    def test_stop_pct_passed_into_macross_config(self) -> None:
        from run_sandbox import _build_strategy  # type: ignore[import-not-found]

        settings = _build_settings(stop_pct=0.05)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _strategy_id, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        # The frozen msgspec.Struct on the strategy carries stop_pct.
        assert strategy.config.stop_pct == 0.05
        # Persisted config dict (lands in strategy_runs.config JSONB) records it.
        assert config_dict["stop_pct"] == 0.05

    def test_stop_pct_none_passes_through(self) -> None:
        """If user disables the stop (`STOP_PCT=` left blank → None), the
        runner must propagate that. The mixin treats None as "disabled."
        """
        from run_sandbox import _build_strategy

        settings = _build_settings(stop_pct=None)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _strategy_id, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        assert strategy.config.stop_pct is None
        assert config_dict["stop_pct"] is None

    @pytest.mark.parametrize("stop_pct", [0.025, 0.05, 0.10])
    def test_various_stop_pcts(self, stop_pct: float) -> None:
        from run_sandbox import _build_strategy

        settings = _build_settings(stop_pct=stop_pct)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        assert strategy.config.stop_pct == stop_pct
        assert config_dict["stop_pct"] == stop_pct


# ── Tests against scripts/run_live.py ──────────────────────────────────────


class TestRunLiveStopPctWiring:
    """Mirrors the sandbox tests against the live runner.

    Same `_build_strategy` shape, but the live runner also disables the
    liquidation simulator (venue handles real liquidation). The stop_pct
    wiring is independent of that.
    """

    def test_stop_pct_passed_into_macross_config(self) -> None:
        from run_live import _build_strategy  # type: ignore[import-not-found]

        settings = _build_settings(stop_pct=0.05)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _strategy_id, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        assert strategy.config.stop_pct == 0.05
        assert config_dict["stop_pct"] == 0.05
        # Live runner also disables liquidation simulator — sanity-check.
        assert strategy.config.liquidation is None


# ── bootstrap_on_deploy wiring (both runners) ──────────────────────────────


class TestRunSandboxBootstrapOnDeployWiring:
    """``run_sandbox._build_strategy`` must thread ``settings.bootstrap_on_deploy``
    into ``MACrossConfig.bootstrap_on_deploy`` and into the persisted config dict.

    Without this, MACross defaults to ``bootstrap_on_deploy=False`` regardless
    of what `.env` says, and the strategy waits for a real MA cross instead
    of treating the first observed signal as a synthetic cross on deploy.
    """

    @pytest.mark.parametrize("bootstrap_on_deploy", [True, False])
    def test_bootstrap_passed_into_macross_config(
        self, bootstrap_on_deploy: bool,
    ) -> None:
        from run_sandbox import _build_strategy

        settings = _build_settings(bootstrap_on_deploy=bootstrap_on_deploy)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _strategy_id, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        assert strategy.config.bootstrap_on_deploy is bootstrap_on_deploy
        assert config_dict["bootstrap_on_deploy"] is bootstrap_on_deploy


class TestRunLiveBootstrapOnDeployWiring:
    """Mirror of the sandbox test against the live runner."""

    @pytest.mark.parametrize("bootstrap_on_deploy", [True, False])
    def test_bootstrap_passed_into_macross_config(
        self, bootstrap_on_deploy: bool,
    ) -> None:
        from run_live import _build_strategy

        settings = _build_settings(bootstrap_on_deploy=bootstrap_on_deploy)
        instrument_id, bar_type = _instrument_and_bar_type()

        strategy, _strategy_id, config_dict = _build_strategy(
            strategy_name="EMACross",
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=Decimal("2000"),
            settings=settings,
        )

        assert strategy.config.bootstrap_on_deploy is bootstrap_on_deploy
        assert config_dict["bootstrap_on_deploy"] is bootstrap_on_deploy
