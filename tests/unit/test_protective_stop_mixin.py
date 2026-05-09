"""Unit tests for the ProtectiveStopAware strategy mixin.

These tests verify the mixin's standalone behavior without spinning up a
full NT engine.  They cover:

- ``_init_protective_stop`` state initialization (None / valid / invalid).
- ``_protective_enabled`` guard logic.
- ``_protective_compute_stop_price`` math for LONG and SHORT.
- ``_protective_should_skip`` defensive guards.
- ``_protective_close_side`` sign mapping.
- ``on_reset`` clears the per-position bookkeeping.
- The ``PROTECTIVE_STOP_TAG`` constant is the documented tag string.

Integration tests with NT's BacktestEngine live elsewhere — those run the
full path including order submission, matching, and protective-stop fill
event handling.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.core.rust.model import OrderSide, PositionSide

from src.core.protective_stop_mixin import PROTECTIVE_STOP_TAG, ProtectiveStopAware

# ── Mixin-only test harness ────────────────────────────────────────────────


class _MixinHarness(ProtectiveStopAware):
    """Concrete subclass to test mixin methods in isolation.

    Skipping ``Strategy`` here is deliberate — these tests don't need NT's
    state machinery, and the mixin's internal helpers (``_protective_enabled``,
    ``_protective_compute_stop_price``, etc.) don't touch ``self.cache`` or
    ``self.order_factory``.
    """


# ── Init ────────────────────────────────────────────────────────────────────


class TestInitProtectiveStop:
    def test_creates_state_with_fraction(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_stop_pct == Decimal("0.05")
        assert harness._protective_order_ids == {}
        assert harness._protective_count == 0

    def test_accepts_decimal(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(Decimal("0.025"))
        assert harness._protective_stop_pct == Decimal("0.025")

    def test_accepts_none(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(None)
        assert harness._protective_stop_pct is None
        assert harness._protective_order_ids == {}
        assert harness._protective_count == 0

    def test_zero_disables(self) -> None:
        """``stop_pct=0`` disables silently (sane no-op rather than error)."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.0)
        assert harness._protective_stop_pct is None

    def test_negative_disables(self) -> None:
        """Negative stop_pct disables silently."""
        harness = _MixinHarness()
        harness._init_protective_stop(-0.05)
        assert harness._protective_stop_pct is None

    def test_geq_one_raises(self) -> None:
        """``stop_pct >= 1`` would put the stop at/past zero — almost
        certainly the user passed a percentage (5.0) instead of a
        fraction (0.05).  Loud failure prevents silent damage."""
        harness = _MixinHarness()
        with pytest.raises(ValueError, match="stop_pct=5.0 must be a fraction in"):
            harness._init_protective_stop(5.0)

    def test_geq_one_raises_at_exactly_one(self) -> None:
        harness = _MixinHarness()
        with pytest.raises(ValueError, match=r"stop_pct=1\.0 must be a fraction in"):
            harness._init_protective_stop(1.0)


# ── Enabled guard ──────────────────────────────────────────────────────────


class TestProtectiveEnabled:
    def test_enabled_when_pct_set(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_enabled() is True

    def test_disabled_when_pct_none(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(None)
        assert harness._protective_enabled() is False

    def test_disabled_when_init_forgotten(self) -> None:
        """Forgetting _init_protective_stop must NOT crash — silently no-op."""
        harness = _MixinHarness()
        # Note: no _init_protective_stop() call.
        assert harness._protective_enabled() is False

    def test_disabled_when_pct_zero(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.0)
        assert harness._protective_enabled() is False


# ── Stop-price math ────────────────────────────────────────────────────────


class TestComputeStopPrice:
    """For long: stop at entry × (1 - stop_pct).  For short: × (1 + stop_pct)."""

    def test_long_basic(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        # entry=$50,000, 5% stop → trigger at $47,500
        result = harness._protective_compute_stop_price(
            PositionSide.LONG,
            entry_price=Decimal("50000"),
        )
        assert result == Decimal("47500.00")

    def test_short_basic(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        # entry=$50,000, 5% stop → trigger at $52,500
        result = harness._protective_compute_stop_price(
            PositionSide.SHORT,
            entry_price=Decimal("50000"),
        )
        assert result == Decimal("52500.00")

    def test_isolated_margin_equivalence_at_20x(self) -> None:
        """The user's headline use case: at ``stop_pct = 1/leverage``,
        worst-case loss = IM = ``notional / leverage``.  Verify the math
        matches: $2000 notional × 5% stop = $100 risk = $2000/20× IM."""
        harness = _MixinHarness()
        harness._init_protective_stop(Decimal("0.05"))  # = 1/20
        entry = Decimal("100")
        stop = harness._protective_compute_stop_price(PositionSide.LONG, entry)
        # Loss at stop fill = (entry - stop) / entry × notional
        # = (100 - 95) / 100 × 2000 = $100
        notional = Decimal("2000")
        loss = (entry - stop) / entry * notional
        assert loss == Decimal("100.00")

    def test_long_tight_stop(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.025)
        result = harness._protective_compute_stop_price(
            PositionSide.LONG,
            entry_price=Decimal("100"),
        )
        assert result == Decimal("97.500")

    def test_long_loose_stop(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.20)
        result = harness._protective_compute_stop_price(
            PositionSide.LONG,
            entry_price=Decimal("100"),
        )
        assert result == Decimal("80.00")


# ── Should-skip guard ──────────────────────────────────────────────────────


class TestShouldSkip:
    def test_long_healthy(self) -> None:
        """Long: stop below entry → submit (don't skip)."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_should_skip(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        ) is False

    def test_short_healthy(self) -> None:
        """Short: stop above entry → submit (don't skip)."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_should_skip(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            stop_price=Decimal("105"),
        ) is False

    def test_long_wrong_side(self) -> None:
        """Long: stop at or above entry → skip (would fire immediately)."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_should_skip(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            stop_price=Decimal("100"),
        ) is True
        assert harness._protective_should_skip(
            PositionSide.LONG,
            entry_price=Decimal("100"),
            stop_price=Decimal("105"),
        ) is True

    def test_short_wrong_side(self) -> None:
        """Short: stop at or below entry → skip."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_should_skip(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            stop_price=Decimal("100"),
        ) is True
        assert harness._protective_should_skip(
            PositionSide.SHORT,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        ) is True

    def test_zero_entry(self) -> None:
        """Defensive: zero entry price (shouldn't happen) → skip."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        assert harness._protective_should_skip(
            PositionSide.LONG,
            entry_price=Decimal("0"),
            stop_price=Decimal("0"),
        ) is True


# ── Close-side mapping ─────────────────────────────────────────────────────


class TestCloseSide:
    def test_long_closes_with_sell(self) -> None:
        harness = _MixinHarness()
        assert harness._protective_close_side(PositionSide.LONG) == OrderSide.SELL

    def test_short_closes_with_buy(self) -> None:
        harness = _MixinHarness()
        assert harness._protective_close_side(PositionSide.SHORT) == OrderSide.BUY


# ── Reset ──────────────────────────────────────────────────────────────────


class TestReset:
    def test_clears_order_ids_and_counter(self) -> None:
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        # Simulate state from a prior iteration.
        harness._protective_order_ids = {"some-id": "some-order"}
        harness._protective_count = 7
        harness.on_reset()
        assert harness._protective_order_ids == {}
        assert harness._protective_count == 0

    def test_reset_preserves_stop_pct(self) -> None:
        """on_reset clears per-iteration state but does NOT clear the
        config (stop_pct was set in __init__ and survives sweep iterations)."""
        harness = _MixinHarness()
        harness._init_protective_stop(0.05)
        harness.on_reset()
        assert harness._protective_stop_pct == Decimal("0.05")
        assert harness._protective_enabled() is True


# ── Tag constant ───────────────────────────────────────────────────────────


class TestTagConstant:
    def test_tag_is_documented_string(self) -> None:
        """Notebooks rely on this exact tag string for close-cause analysis.
        If you rename it, update the v2 tearsheet + notebook code that
        filters orders by tag."""
        assert PROTECTIVE_STOP_TAG == "protective_stop"
