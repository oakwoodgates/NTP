"""Unit tests for the LiquidationAware strategy mixin.

These tests verify the mixin's standalone behavior without spinning up a
full NT engine.  They cover:

- ``_init_liquidation`` state initialization.
- ``_liq_enabled`` guard logic (disabled when config is None / disabled).
- ``_resolve_mm_rate`` reads the override-only field on a fully-resolved
  ``LiquidationConfig``.
- ``_liq_already_past`` short / long edge cases.
- ``_liq_close_side`` sign mapping.
- ``on_reset`` clears the per-position bookkeeping.

Integration tests with NT's BacktestEngine live elsewhere — those run the
full path including order submission, matching, and PositionLiquidated
event publication.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.core.rust.model import OrderSide, PositionSide

from src.core.liquidation import LiquidationConfig
from src.core.liquidation_mixin import LiquidationAware


def _resolved(mm_rate: Decimal = Decimal("0.005")) -> LiquidationConfig:
    """Build a fully-resolved LiquidationConfig (as ``make_engine`` would)."""
    return LiquidationConfig(
        enabled=True,
        mm_rate=mm_rate,
        fee_rate=Decimal("0.0005"),
        min_trade_notional=Decimal("100"),
        alive_trades_buffer=1,
        halt_on_account_liquidation=True,
    )


# ── Mixin-only test harness ────────────────────────────────────────────────


class _ChainEnd:
    """End-of-MRO sentinel — provides no-op stubs so ``super().on_save()`` /
    ``super().on_load()`` from the mixin don't hit ``object`` and AttributeError.

    In production the chain ends at NT's ``Strategy`` / ``Actor`` which
    have concrete no-op stubs.  Here we use this lightweight sentinel.
    """

    def on_save(self) -> dict[str, bytes]:
        return {}

    def on_load(self, state: dict[str, bytes]) -> None: ...


class _MixinHarness(LiquidationAware, _ChainEnd):
    """Concrete subclass to test mixin methods in isolation.

    Skipping ``Strategy`` here is deliberate — these tests don't need NT's
    state machinery, and the mixin's internal helpers (``_liq_enabled``,
    ``_resolve_mm_rate``, etc.) don't touch ``self.cache`` or
    ``self.order_factory``.
    """


class TestInitLiquidation:
    def test_creates_state(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(_resolved())
        assert harness._liq_config is not None
        assert harness._liq_order_ids == {}
        assert harness._liq_count == 0

    def test_accepts_none(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(None)
        assert harness._liq_config is None
        assert harness._liq_order_ids == {}
        assert harness._liq_count == 0


class TestEnabledGuard:
    def test_enabled_when_config_set_and_enabled(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(_resolved())
        assert harness._liq_enabled() is True

    def test_disabled_when_config_is_none(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(None)
        assert harness._liq_enabled() is False

    def test_disabled_when_config_enabled_false(self) -> None:
        cfg = LiquidationConfig(
            enabled=False,
            mm_rate=Decimal("0.005"),
            fee_rate=Decimal("0.0005"),
            min_trade_notional=Decimal("100"),
        )
        harness = _MixinHarness()
        harness._init_liquidation(cfg)
        assert harness._liq_enabled() is False

    def test_disabled_when_init_forgotten(self) -> None:
        """Forgetting _init_liquidation must NOT crash — silently no-op."""
        harness = _MixinHarness()
        # Note: no _init_liquidation() call.
        assert harness._liq_enabled() is False


class TestResolveMmRate:
    def test_reads_resolved_value(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(_resolved(Decimal("0.004")))
        assert harness._liq_mm_rate() == Decimal("0.004")

    def test_raises_when_unresolved(self) -> None:
        """Hand-constructed config with mm_rate=None should raise.

        ``make_engine`` is responsible for resolving mm_rate from
        VenueConfig before passing the config to a strategy. If the
        mixin sees a None mm_rate at runtime, that's a configuration bug.
        """
        cfg = LiquidationConfig(
            enabled=True,
            mm_rate=None,
            fee_rate=Decimal("0.0005"),
            min_trade_notional=Decimal("100"),
        )
        harness = _MixinHarness()
        harness._init_liquidation(cfg)
        with pytest.raises(ValueError, match="mm_rate is None"):
            harness._liq_mm_rate()


class TestLiqShouldSkipStop:
    """Two reasons to skip submitting a liquidation stop:

    1. Already past liquidation — liq_price is on the wrong side of entry.
    2. Over-collateralised — liq_distance >= 1, so liquidation is unreachable
       (LONG: liq_price ≤ 0; SHORT: liq_price ≥ 2 × entry).
    """

    # ── Healthy / submit case ─────────────────────────────────────────────

    def test_long_healthy(self) -> None:
        """Long: liq_price below entry, above 0 → submit (don't skip)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            liq_price=Decimal("50"),
        ) is False

    def test_short_healthy(self) -> None:
        """Short: entry < liq_price < 2 × entry → submit (don't skip)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            liq_price=Decimal("150"),
        ) is False

    # ── Already past liquidation ──────────────────────────────────────────

    def test_long_past(self) -> None:
        """Long: liq_price at or above entry → skip (already past)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            liq_price=Decimal("100"),
        ) is True
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            liq_price=Decimal("105"),
        ) is True

    def test_short_past(self) -> None:
        """Short: liq_price at or below entry → skip (already past)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            liq_price=Decimal("100"),
        ) is True
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            liq_price=Decimal("95"),
        ) is True

    # ── Over-collateralised (liq_distance ≥ 1) ────────────────────────────

    def test_long_over_collateralised_negative(self) -> None:
        """Long: liq_price negative (equity >> notional) → skip.

        Real scenario: $1000 equity, $100 notional, mm_rate=0.005 →
        liq_distance ≈ 9.995 → liq_price = entry × −8.995 (negative).
        NT would reject the stop submission. We catch it earlier.
        """
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("90000"),
            liq_price=Decimal("-810000"),  # 90000 × −9
        ) is True

    def test_long_over_collateralised_zero(self) -> None:
        """Long: liq_price exactly 0 → skip (boundary)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            liq_price=Decimal("0"),
        ) is True

    def test_long_just_above_zero_submits(self) -> None:
        """Long: liq_price slightly above 0 → submit (very close to floor)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            liq_price=Decimal("0.01"),
        ) is False

    def test_short_over_collateralised_extreme(self) -> None:
        """Short: liq_price ≥ 2 × entry → skip.

        Real scenario: $1000 equity, $100 notional → liq_price ≈ 11 × entry.
        NT accepts but the trigger is unreachable in practice; the order
        sits on the book pointlessly until the position closes.
        """
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("90000"),
            liq_price=Decimal("990000"),  # 11 × entry
        ) is True

    def test_short_at_2x_entry_skips(self) -> None:
        """Short: liq_price exactly 2 × entry → skip (boundary, equivalent
        to liq_distance = 1)."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            liq_price=Decimal("200"),
        ) is True

    def test_short_just_below_2x_submits(self) -> None:
        """Short: liq_price just below 2 × entry → submit."""
        harness = _MixinHarness()
        assert harness._liq_should_skip_stop(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            liq_price=Decimal("199.99"),
        ) is False


class TestLiqCloseSide:
    def test_long_closes_with_sell(self) -> None:
        harness = _MixinHarness()
        assert harness._liq_close_side(PositionSide.LONG) == OrderSide.SELL

    def test_short_closes_with_buy(self) -> None:
        harness = _MixinHarness()
        assert harness._liq_close_side(PositionSide.SHORT) == OrderSide.BUY


class TestOnReset:
    def test_clears_state(self) -> None:
        harness = _MixinHarness()
        harness._init_liquidation(_resolved())
        # Simulate accumulated state from a prior sweep iteration.
        # Use string-keyed entries; we're testing that on_reset clears the
        # dict, not that real PositionId/ClientOrderId types round-trip.
        harness._liq_order_ids = {"position-1": "client-order-1"}
        harness._liq_count = 5

        harness.on_reset()

        assert harness._liq_order_ids == {}
        assert harness._liq_count == 0


# ── MRO sanity check (the inheritance-order footgun) ─────────────────────


# ── State persistence (on_save / on_load round-trip) ───────────────────────


class TestSaveLoadRoundTrip:
    """``on_save`` / ``on_load`` round-trip the ``position_id → order_id``
    mapping and the liq-fill counter across a restart.

    After a graceful shutdown + restart with reconciliation, the mixin
    still knows which open reduce-only stop belongs to which position,
    so ``on_position_closed`` can cancel it cleanly instead of leaving
    an orphan stop in NT's order cache.
    """

    def test_empty_state_round_trips(self) -> None:
        h1 = _MixinHarness()
        h1._init_liquidation(_resolved())
        state = h1.on_save()
        h2 = _MixinHarness()
        h2._init_liquidation(_resolved())
        h2.on_load(state)
        assert h2._liq_order_ids == {}
        assert h2._liq_count == 0

    def test_populated_state_round_trips(self) -> None:
        from nautilus_trader.model.identifiers import ClientOrderId, PositionId

        h1 = _MixinHarness()
        h1._init_liquidation(_resolved())
        h1._liq_order_ids = {
            PositionId("P-001"): ClientOrderId("LIQ-001"),
            PositionId("P-002"): ClientOrderId("LIQ-002"),
        }
        h1._liq_count = 2
        state = h1.on_save()

        h2 = _MixinHarness()
        h2._init_liquidation(_resolved())
        h2.on_load(state)
        as_strings = {
            pid.value: oid.value for pid, oid in h2._liq_order_ids.items()
        }
        assert as_strings == {"P-001": "LIQ-001", "P-002": "LIQ-002"}
        assert h2._liq_count == 2

    def test_state_values_are_bytes(self) -> None:
        h = _MixinHarness()
        h._init_liquidation(_resolved())
        h._liq_count = 4
        state = h.on_save()
        for key, value in state.items():
            assert isinstance(value, bytes), (
                f"on_save key {key!r} returned {type(value).__name__}, expected bytes"
            )

    def test_load_with_missing_keys_keeps_defaults(self) -> None:
        h = _MixinHarness()
        h._init_liquidation(_resolved())
        h.on_load({})
        assert h._liq_order_ids == {}
        assert h._liq_count == 0

    def test_load_with_malformed_json_resets(self) -> None:
        h = _MixinHarness()
        h._init_liquidation(_resolved())
        h.log = type("L", (), {"warning": lambda self, msg: None})()  # type: ignore[attr-defined]
        h.on_load({"liq_order_ids": b"not json"})
        assert h._liq_order_ids == {}


class TestInheritanceOrderDocumented:
    """Sanity check that the mixin works as a first-position base class.

    The full footgun (mixin-second order silently shadowed by ``Strategy``'s
    no-op stubs) requires NT's ``Strategy`` in the MRO and is exercised by
    the integration test, not here. This test just confirms the mixin can
    be subclassed cleanly when listed first.
    """

    def test_mixin_first_inheritance(self) -> None:
        # Synthetic stand-in for ``Strategy`` — defines the typed handler
        # as a no-op stub (matching ``Strategy.on_position_opened`` shape).
        class StrategyStub:
            def on_position_opened(self, event: object) -> None:
                # Sentinel for the test: this is the version that should be
                # shadowed when LiquidationAware comes first in MRO.
                self._stub_called = True

        # Mixin first → the mixin's typed handlers win MRO over the stub's.
        class MixinFirst(LiquidationAware, StrategyStub):
            pass

        instance = MixinFirst()
        # The mixin's on_position_opened is what gets resolved, not the stub's.
        assert MixinFirst.on_position_opened is LiquidationAware.on_position_opened
        # Sanity: stub method is shadowed.
        assert instance.__class__.__mro__.index(LiquidationAware) < \
               instance.__class__.__mro__.index(StrategyStub)
