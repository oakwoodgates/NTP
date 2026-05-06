"""Unit tests for liquidation/sizing resolution logic in make_engine.

Covers the public helpers ``resolve_strategy_liquidation_config`` (used
by notebook callers to build a fully-resolved ``LiquidationConfig``) and
``liquidation_for_environment`` (used by live/sandbox runners to gate
the simulator per environment).

Does not spin up a NT engine — these tests cover the resolution rules
in isolation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.common import Environment

from src.backtesting.engine import (
    liquidation_for_environment,
    resolve_strategy_liquidation_config,
)
from src.core.liquidation import LiquidationConfig
from src.core.sizing import SizingConfig
from src.core.venues import VenueConfig

# ── Fixtures ───────────────────────────────────────────────────────────────


def _venue() -> VenueConfig:
    return VenueConfig(
        name="HYPERLIQUID_PERP",
        nt_venue="HYPERLIQUID",
        maker_fee=Decimal("0.00010"),
        taker_fee=Decimal("0.00035"),
        leverage=20,
        mm_rate=Decimal("0.005"),
        settlement_currency="USDC",
    )


class _FakeInstrument:
    """Stand-in for an NT Instrument with the attributes our resolvers read."""

    def __init__(
        self,
        taker_fee: Decimal | None = None,
        min_notional: Decimal | None = None,
    ) -> None:
        self.taker_fee = taker_fee
        self.min_notional = min_notional


# ── Disabled / None passthrough ────────────────────────────────────────────


class TestDisabledPassthrough:
    def test_none_returns_none(self) -> None:
        assert resolve_strategy_liquidation_config(
            user=None,
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        ) is None

    def test_disabled_returns_unchanged(self) -> None:
        user = LiquidationConfig(enabled=False)
        out = resolve_strategy_liquidation_config(
            user=user,
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        )
        # Same config back — no resolution happens for disabled.
        assert out is user


# ── mm_rate resolution ─────────────────────────────────────────────────────


class TestMmRateResolution:
    def test_explicit_override_wins(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.001"),
                fee_rate=Decimal("0.0005"),
                min_trade_notional=Decimal("100"),
            ),
            venue_config=_venue(),  # mm_rate=0.005
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        )
        assert out is not None
        assert out.mm_rate == Decimal("0.001")

    def test_falls_through_to_venue_config(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=None,  # no override → use VenueConfig
                fee_rate=Decimal("0.0005"),
                min_trade_notional=Decimal("100"),
            ),
            venue_config=_venue(),  # mm_rate=0.005
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        )
        assert out is not None
        assert out.mm_rate == Decimal("0.005")


# ── fee_rate resolution ────────────────────────────────────────────────────


class TestFeeRateResolution:
    def test_explicit_override_wins(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=Decimal("0.001"),
                min_trade_notional=Decimal("100"),
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(
                taker_fee=Decimal("0.0007"),
                min_notional=Decimal("10"),
            ),
            sizing=None,
        )
        assert out is not None
        assert out.fee_rate == Decimal("0.001")

    def test_falls_through_to_instrument_taker_fee(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=None,
                min_trade_notional=Decimal("100"),
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(
                taker_fee=Decimal("0.0007"),
                min_notional=Decimal("10"),
            ),
            sizing=None,
        )
        assert out is not None
        assert out.fee_rate == Decimal("0.0007")


# ── min_trade_notional resolution ──────────────────────────────────────────


class TestMinTradeNotionalResolution:
    def test_explicit_override_wins(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=Decimal("0.0005"),
                min_trade_notional=Decimal("250"),
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=SizingConfig(
                mode="equity_frac",
                risk_frac=Decimal("0.10"),
                stop_pct=Decimal("0.05"),
                min_notional=Decimal("50"),
            ),
        )
        assert out is not None
        assert out.min_trade_notional == Decimal("250")

    def test_falls_through_to_sizing_min_notional(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=Decimal("0.0005"),
                min_trade_notional=None,
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=SizingConfig(
                mode="equity_frac",
                risk_frac=Decimal("0.10"),
                stop_pct=Decimal("0.05"),
                min_notional=Decimal("50"),
            ),
        )
        assert out is not None
        assert out.min_trade_notional == Decimal("50")

    def test_falls_through_to_sizing_fixed_notional(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=Decimal("0.0005"),
                min_trade_notional=None,
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=SizingConfig(mode="fixed", fixed_notional=Decimal("500")),
        )
        assert out is not None
        assert out.min_trade_notional == Decimal("500")

    def test_falls_through_to_instrument_min_notional(self) -> None:
        out = resolve_strategy_liquidation_config(
            user=LiquidationConfig(
                enabled=True,
                mm_rate=Decimal("0.005"),
                fee_rate=Decimal("0.0005"),
                min_trade_notional=None,
            ),
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        )
        assert out is not None
        assert out.min_trade_notional == Decimal("10")


# ── End-to-end integrity ───────────────────────────────────────────────────


class TestPreservesOtherFields:
    def test_alive_buffer_and_halt_pass_through(self) -> None:
        user = LiquidationConfig(
            enabled=True,
            mm_rate=Decimal("0.005"),
            fee_rate=Decimal("0.0005"),
            min_trade_notional=Decimal("100"),
            alive_trades_buffer=3,
            halt_on_account_liquidation=False,
        )
        out = resolve_strategy_liquidation_config(
            user=user,
            venue_config=_venue(),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            sizing=None,
        )
        assert out is not None
        assert out.alive_trades_buffer == 3
        assert out.halt_on_account_liquidation is False
        assert out.enabled is True


# ── Per-environment gating ─────────────────────────────────────────────────


def _full_config() -> LiquidationConfig:
    return LiquidationConfig(
        enabled=True,
        mm_rate=Decimal("0.005"),
        fee_rate=Decimal("0.00035"),
        min_trade_notional=Decimal("100"),
        alive_trades_buffer=1,
        halt_on_account_liquidation=True,
    )


class TestLiquidationForEnvironment:
    def test_backtest_passthrough(self) -> None:
        cfg = _full_config()
        out = liquidation_for_environment(cfg, Environment.BACKTEST)
        # Same object returned — no copy needed for backtest.
        assert out is cfg

    def test_live_returns_none(self) -> None:
        cfg = _full_config()
        out = liquidation_for_environment(cfg, Environment.LIVE)
        assert out is None

    def test_live_with_none_input_stays_none(self) -> None:
        out = liquidation_for_environment(None, Environment.LIVE)
        assert out is None

    def test_sandbox_disables_halt(self) -> None:
        cfg = _full_config()
        assert cfg.halt_on_account_liquidation is True
        out = liquidation_for_environment(cfg, Environment.SANDBOX)
        assert out is not None
        assert out.halt_on_account_liquidation is False
        # Other fields preserved.
        assert out.enabled is True
        assert out.mm_rate == cfg.mm_rate
        assert out.min_trade_notional == cfg.min_trade_notional

    def test_sandbox_with_none_input(self) -> None:
        out = liquidation_for_environment(None, Environment.SANDBOX)
        assert out is None

    def test_sandbox_with_disabled_input_returns_unchanged(self) -> None:
        """If user already disabled, no-op — we don't flip enabled to True."""
        cfg = LiquidationConfig(enabled=False)
        out = liquidation_for_environment(cfg, Environment.SANDBOX)
        assert out is cfg

    def test_sandbox_does_not_mutate_original(self) -> None:
        """msgspec replace returns a new struct; original is frozen."""
        cfg = _full_config()
        liquidation_for_environment(cfg, Environment.SANDBOX)
        assert cfg.halt_on_account_liquidation is True   # unchanged


class TestMakeEngineRejectsNonBacktest:
    """make_engine returns a BacktestEngine; non-BACKTEST env is a config bug."""

    def test_live_raises(self) -> None:
        from src.backtesting.engine import make_engine

        # We pass minimal junk for everything else — the env check fires first.
        with pytest.raises(ValueError, match="BacktestEngine"):
            make_engine(
                venue=None,
                instrument=None,
                bars=[],
                starting_capital=1000,
                environment=Environment.LIVE,
            )

    def test_sandbox_raises(self) -> None:
        from src.backtesting.engine import make_engine

        with pytest.raises(ValueError, match="BacktestEngine"):
            make_engine(
                venue=None,
                instrument=None,
                bars=[],
                starting_capital=1000,
                environment=Environment.SANDBOX,
            )
