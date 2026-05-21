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


class _ChainEnd:
    """No-op sentinel — sits at the end of the test MRO chain.

    ``ProtectiveStopAware`` calls ``super().on_*()`` in every event handler
    (the cooperative-super pattern that lets the mixin compose with
    ``LiquidationAware`` and other mixins downstream).  In production the
    chain ends at NT's ``Strategy`` which has concrete no-op stubs; in
    these standalone tests we use this lightweight sentinel so the super()
    calls don't ``AttributeError`` against ``object``.
    """

    def on_start(self) -> None: ...
    def on_position_opened(self, event: object) -> None: ...
    def on_position_changed(self, event: object) -> None: ...
    def on_position_closed(self, event: object) -> None: ...
    def on_order_filled(self, event: object) -> None: ...
    def on_reset(self) -> None: ...


class _MixinHarness(ProtectiveStopAware, _ChainEnd):
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


# ── Cooperative super() chain ─────────────────────────────────────────────


class _SpyParent:
    """Records every event-handler call for assertion in chain tests.

    Used as the parent class in ``(ProtectiveStopAware, _SpyParent)`` test
    fixtures so we can verify ``super().on_*()`` calls actually fire — and
    that they fire EVEN WHEN the mixin is disabled (``stop_pct=None``).

    This is the regression case for the bug where ``ProtectiveStopAware``
    would silently swallow events when disabled, never propagating them
    to downstream mixins like ``LiquidationAware``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def on_start(self) -> None:
        self.calls.append(("on_start", None))

    def on_position_opened(self, event: object) -> None:
        self.calls.append(("on_position_opened", event))

    def on_position_changed(self, event: object) -> None:
        self.calls.append(("on_position_changed", event))

    def on_position_closed(self, event: object) -> None:
        self.calls.append(("on_position_closed", event))

    def on_order_filled(self, event: object) -> None:
        self.calls.append(("on_order_filled", event))

    def on_reset(self) -> None:
        self.calls.append(("on_reset", None))


class _ChainHarness(ProtectiveStopAware, _SpyParent):
    """Test harness with a spy parent for verifying super() calls."""


class TestSuperChain:
    """Regression: ``ProtectiveStopAware`` MUST call super() in every event
    handler so downstream mixins (e.g. ``LiquidationAware``) still receive
    the event when this mixin is disabled.

    Without this, a strategy declared as
    ``(ProtectiveStopAware, LiquidationAware, Strategy)`` with
    ``stop_pct=None`` silently bypasses ``LiquidationAware`` — meaning no
    per-position cross-margin liq stop gets placed.  The
    ``AccountAliveMonitor`` actor still halts on equity breaches (it's a
    separate Actor, not affected by the strategy's MRO), but min_balance
    can go sub-zero before that happens because the per-position safety
    net is gone.
    """

    def test_disabled_propagates_on_start(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)
        h.on_start()
        assert h.calls == [("on_start", None)]

    def test_disabled_propagates_position_opened(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)  # disabled
        sentinel = object()
        h.on_position_opened(sentinel)
        assert h.calls == [("on_position_opened", sentinel)]

    def test_disabled_propagates_position_changed(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)
        sentinel = object()
        h.on_position_changed(sentinel)
        assert h.calls == [("on_position_changed", sentinel)]

    def test_disabled_propagates_position_closed(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)
        sentinel = object()
        h.on_position_closed(sentinel)
        assert h.calls == [("on_position_closed", sentinel)]

    def test_disabled_propagates_order_filled(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)
        sentinel = object()
        h.on_order_filled(sentinel)
        assert h.calls == [("on_order_filled", sentinel)]

    def test_disabled_propagates_reset(self) -> None:
        h = _ChainHarness()
        h._init_protective_stop(None)
        h.on_reset()
        assert h.calls == [("on_reset", None)]

    def test_enabled_also_propagates(self) -> None:
        """When enabled, super() must STILL be called — the mixin doesn't
        get to be selfish about events.  Order in the chain matters: super()
        is called first, then this mixin's logic runs.

        Without an instrument cache + order factory we can't exercise the
        full enabled path (the ``_protective_issue_stop`` call would
        AttributeError), so we just verify the super() chain fires for
        ``on_reset`` which has no I/O side-effects."""
        h = _ChainHarness()
        h._init_protective_stop(0.05)  # enabled
        h.on_reset()
        assert h.calls == [("on_reset", None)]
        # And the mixin's own reset still happened.
        assert h._protective_order_ids == {}
        assert h._protective_count == 0


# ── Restart-state rehydration + idempotency ────────────────────────────────
#
# These tests cover the "container restarts with positions/stops already on
# the venue" path documented in docs/PROTECTIVE_STOP_RESTART_AUDIT.md.  We
# stub the parts of NT's Strategy interface that the mixin touches: a fake
# cache exposing ``orders_open`` / ``positions_open`` / ``instrument``, a
# logger, and an ``order_factory`` + ``submit_order``.  This is enough to
# verify the rehydration and idempotency branches without a BacktestEngine.


class _FakeLog:
    def __init__(self) -> None:
        self.info_calls: list[str] = []
        self.warning_calls: list[str] = []
        self.error_calls: list[str] = []

    def info(self, msg: str) -> None:
        self.info_calls.append(msg)

    def warning(self, msg: str) -> None:
        self.warning_calls.append(msg)

    def error(self, msg: str) -> None:
        self.error_calls.append(msg)


class _FakeOrder:
    """Minimal stand-in for ``nautilus_trader.model.orders.Order`` —
    exposes the attributes touched by rehydrate + idempotency paths."""

    def __init__(
        self,
        client_order_id: str,
        order_type: object,
        side: object,
        is_reduce_only: bool,
        is_open: bool = True,
        trigger_price: Decimal | None = None,
    ) -> None:
        self.client_order_id = client_order_id
        self.order_type = order_type
        self.side = side
        self.is_reduce_only = is_reduce_only
        self.is_open = is_open
        self.trigger_price = trigger_price


class _FakePosition:
    def __init__(self, position_id: str, side: PositionSide) -> None:
        self.id = position_id
        self.side = side


class _FakeCache:
    def __init__(
        self,
        orders_open: list[_FakeOrder] | None = None,
        positions_open: list[_FakePosition] | None = None,
    ) -> None:
        self._orders_open = orders_open or []
        self._positions_open = positions_open or []

    def orders_open(self, *, instrument_id: object = None) -> list[_FakeOrder]:
        return list(self._orders_open)

    def positions_open(self, *, instrument_id: object = None) -> list[_FakePosition]:
        return list(self._positions_open)

    def order(self, client_order_id: str) -> _FakeOrder | None:
        for o in self._orders_open:
            if o.client_order_id == client_order_id:
                return o
        return None


class _RestartHarness(ProtectiveStopAware, _ChainEnd):
    """Mixin harness wired with fake cache + log for restart-state tests."""

    def __init__(
        self,
        cache: _FakeCache,
        log: _FakeLog,
    ) -> None:
        self.cache = cache
        self.log = log


# Sentinels we can compare against without instantiating NT types.
_BTC = "BTC-USD-PERP.HYPERLIQUID"


def _stop(
    cloid: str,
    side: OrderSide,
    *,
    trigger: Decimal | None = None,
    is_reduce_only: bool = True,
) -> _FakeOrder:
    from nautilus_trader.core.rust.model import OrderType
    return _FakeOrder(
        cloid,
        OrderType.STOP_MARKET,
        side,
        is_reduce_only=is_reduce_only,
        trigger_price=trigger,
    )


class TestFindExistingStop:
    def test_finds_reduce_only_stop_on_close_side(self) -> None:
        sell_stop = _stop("S1", OrderSide.SELL)
        cache = _FakeCache(orders_open=[sell_stop])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        found = h._protective_find_existing_stop(_BTC, OrderSide.SELL)
        assert found is sell_stop

    def test_skips_non_reduce_only_stop(self) -> None:
        bare_stop = _stop("S1", OrderSide.SELL, is_reduce_only=False)
        cache = _FakeCache(orders_open=[bare_stop])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        assert h._protective_find_existing_stop(_BTC, OrderSide.SELL) is None

    def test_skips_wrong_side(self) -> None:
        """A SELL stop is the close-side for a LONG; rehydrating a SHORT
        (which needs a BUY stop) must not pick up the LONG's SELL stop."""
        sell_stop = _stop("S1", OrderSide.SELL)
        cache = _FakeCache(orders_open=[sell_stop])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        assert h._protective_find_existing_stop(_BTC, OrderSide.BUY) is None

    def test_skips_non_stop_market_order(self) -> None:
        from nautilus_trader.core.rust.model import OrderType
        limit_order = _FakeOrder(
            "L1", OrderType.LIMIT, OrderSide.SELL, is_reduce_only=True,
        )
        cache = _FakeCache(orders_open=[limit_order])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        assert h._protective_find_existing_stop(_BTC, OrderSide.SELL) is None

    def test_tag_agnostic(self) -> None:
        """The match must NOT depend on the ``protective_stop`` tag.
        Reconciliation strips user tags and replaces them with
        ``["VENUE"]`` or ``None`` — but the structural identifiers
        (order_type / is_reduce_only / side) survive.  See
        nautilus_trader/live/execution_engine.py::_generate_order line
        ~3500 for the tag-stripping behaviour."""
        reconciled = _stop("V1", OrderSide.SELL)
        # No tag attribute — equivalent to a reconciled-from-venue order
        # that lost its ``protective_stop`` tag during reconciliation.
        cache = _FakeCache(orders_open=[reconciled])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        assert h._protective_find_existing_stop(_BTC, OrderSide.SELL) is reconciled


class TestRehydrate:
    """``_protective_rehydrate`` rebuilds ``_protective_order_ids`` from
    cache truth — covers the cache-survived and cache-rebuilt-by-NT paths."""

    def test_one_position_one_stop_binds(self) -> None:
        sell_stop = _stop("S1", OrderSide.SELL)
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(orders_open=[sell_stop], positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {"P1": "S1"}
        # Audit summary logged.
        assert any("REHYDRATE" in m for m in h.log.info_calls)
        # No warnings — clean state.
        assert h.log.warning_calls == []

    def test_position_without_stop_warns(self) -> None:
        """Stop fired during outage scenario from the audit doc — HL has
        no order but the position is still open (e.g. partially filled
        stop, or stop fired and the strategy hasn't seen the close
        event yet).  Rehydrate logs a protection-gap warning."""
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(orders_open=[], positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {}
        assert any("protection gap" in m for m in h.log.warning_calls)

    def test_orphan_stop_without_position_warns(self) -> None:
        """A reduce-only stop with no matching position — e.g. operator
        left an orphan from a prior strategy.  We don't auto-cancel
        (could be intentional); just log."""
        sell_stop = _stop("S1", OrderSide.SELL)
        cache = _FakeCache(orders_open=[sell_stop], positions_open=[])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {}
        assert any("unbound" in m for m in h.log.warning_calls)

    def test_flat_account_logs_nothing_noisy(self) -> None:
        """The post-outage "stop fired, position closed" case: zero
        positions, zero stops.  Audit summary logged but no warnings."""
        cache = _FakeCache(orders_open=[], positions_open=[])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {}
        assert h.log.warning_calls == []
        assert any("positions=0 stops=0" in m for m in h.log.info_calls)

    def test_overwrites_stale_mapping(self) -> None:
        """Rehydrate must replace, not extend.  If ``_protective_order_ids``
        has stale entries from a prior invocation (or from in-memory
        state that survived a reset path), they must be cleared."""
        sell_stop = _stop("S1", OrderSide.SELL)
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(orders_open=[sell_stop], positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_order_ids = {"P_STALE": "ORDER_STALE"}
        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {"P1": "S1"}

    def test_disabled_noop(self) -> None:
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(None)

        h._protective_rehydrate(_BTC)
        assert h._protective_order_ids == {}
        assert h.log.info_calls == []
        assert h.log.warning_calls == []

    def test_multiple_positions_match_by_side(self) -> None:
        """Netting mode has at most one position per instrument, but the
        side-matching logic should still pair stops to the right
        position even with multiple positions present (defensive)."""
        from nautilus_trader.core.rust.model import OrderType
        sell_stop = _FakeOrder("S1", OrderType.STOP_MARKET, OrderSide.SELL, is_reduce_only=True)
        buy_stop = _FakeOrder("S2", OrderType.STOP_MARKET, OrderSide.BUY, is_reduce_only=True)
        long_pos = _FakePosition("PL", PositionSide.LONG)
        short_pos = _FakePosition("PS", PositionSide.SHORT)
        cache = _FakeCache(
            orders_open=[sell_stop, buy_stop],
            positions_open=[long_pos, short_pos],
        )
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_rehydrate(_BTC)

        assert h._protective_order_ids == {"PL": "S1", "PS": "S2"}


class _MockOrderFactory:
    """Stub for ``self.order_factory`` — counts calls so we can assert
    that ``_protective_issue_stop`` did NOT submit a duplicate when an
    existing stop already covered the position."""

    def __init__(self) -> None:
        self.stop_market_calls: int = 0
        self._next_cloid_n = 0

    def stop_market(self, **kwargs: object) -> _FakeOrder:
        self.stop_market_calls += 1
        from nautilus_trader.core.rust.model import OrderType
        self._next_cloid_n += 1
        cloid = f"NEW-{self._next_cloid_n}"
        return _FakeOrder(
            cloid,
            OrderType.STOP_MARKET,
            kwargs["order_side"],
            is_reduce_only=True,
            trigger_price=Decimal(str(kwargs["trigger_price"])),
        )


class _FakeInstrument:
    """``self.cache.instrument(...)`` returns this stub.  Only the
    ``make_price`` method is exercised by ``_protective_issue_stop``."""

    @staticmethod
    def make_price(value: Decimal) -> Decimal:
        return value


class _CacheWithInstrument(_FakeCache):
    def instrument(self, instrument_id: object) -> _FakeInstrument:
        return _FakeInstrument()


class _IdempotencyHarness(_RestartHarness):
    """Adds ``order_factory`` + ``submit_order`` for full
    ``_protective_issue_stop`` exercise."""

    def __init__(self, cache: _FakeCache, log: _FakeLog) -> None:
        super().__init__(cache=cache, log=log)
        self.order_factory = _MockOrderFactory()
        self.submitted: list[_FakeOrder] = []

    def submit_order(self, order: _FakeOrder) -> None:
        self.submitted.append(order)


class TestIssueStopIdempotency:
    """``_protective_issue_stop`` must bind to an existing reduce-only
    stop instead of submitting a duplicate.  This is the defence against
    the "reconciliation rebuilt cache + PositionOpened fires" double-stop
    scenario from the audit doc."""

    def test_binds_to_existing_skips_submit(self) -> None:
        existing = _stop("EX1", OrderSide.SELL, trigger=Decimal("95"))
        cache = _CacheWithInstrument(orders_open=[existing])
        h = _IdempotencyHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_issue_stop(
            instrument_id=_BTC,
            position_id="P1",
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )

        # No duplicate submitted.
        assert h.order_factory.stop_market_calls == 0
        assert h.submitted == []
        # Mapping bound to the existing stop.
        assert h._protective_order_ids == {"P1": "EX1"}
        # Info-level audit logged (matching trigger — no warning).
        assert any("IDEMPOTENT BIND" in m for m in h.log.info_calls)
        assert h.log.warning_calls == []

    def test_binds_with_trigger_mismatch_warns(self) -> None:
        """The existing stop's trigger differs from recomputed — e.g.
        position averaged in across restarts.  We keep the existing
        stop (don't briefly remove protection) but warn loudly so
        the operator can intervene."""
        # entry=100, stop_pct=0.05 → recomputed trigger = 95.
        # Existing stop is at 90 (e.g. from a prior, lower fill).
        existing = _stop("EX1", OrderSide.SELL, trigger=Decimal("90"))
        cache = _CacheWithInstrument(orders_open=[existing])
        h = _IdempotencyHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_issue_stop(
            instrument_id=_BTC,
            position_id="P1",
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )

        assert h.order_factory.stop_market_calls == 0
        assert h._protective_order_ids == {"P1": "EX1"}
        # Trigger mismatch warning fired.
        assert any(
            "IDEMPOTENT BIND" in m and "differs from recomputed" in m
            for m in h.log.warning_calls
        )

    def test_submits_when_no_existing(self) -> None:
        """No existing stop → submit a new one (the unchanged happy
        path, pre-restart-hardening behaviour)."""
        cache = _CacheWithInstrument(orders_open=[])
        h = _IdempotencyHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_issue_stop(
            instrument_id=_BTC,
            position_id="P1",
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )

        assert h.order_factory.stop_market_calls == 1
        assert len(h.submitted) == 1
        assert h._protective_order_ids == {"P1": "NEW-1"}

    def test_existing_wrong_side_does_not_bind(self) -> None:
        """A BUY reduce-only stop exists (was the close-side for a
        prior SHORT).  Now we're opening a LONG — close-side is SELL.
        The BUY stop is NOT a match; we must submit a new SELL stop."""
        existing_buy = _stop("EX_BUY", OrderSide.BUY, trigger=Decimal("110"))
        cache = _CacheWithInstrument(orders_open=[existing_buy])
        h = _IdempotencyHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)

        h._protective_issue_stop(
            instrument_id=_BTC,
            position_id="P1",
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )

        # New SELL stop submitted.
        assert h.order_factory.stop_market_calls == 1
        assert h._protective_order_ids == {"P1": "NEW-1"}


class TestOnStartRehydration:
    """``on_start`` is the integration point that wires rehydration to
    NT's lifecycle.  The kernel runs reconciliation BEFORE trader.start
    fires on_start (see system/kernel.py::start_async), so by the time
    we get called the cache already reflects venue state."""

    def test_calls_rehydrate_when_config_present(self) -> None:
        sell_stop = _stop("S1", OrderSide.SELL)
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(orders_open=[sell_stop], positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)
        # Attach a minimal config with instrument_id (mimicking
        # MACrossConfig / etc.).
        h.config = type("C", (), {"instrument_id": _BTC})()  # type: ignore[attr-defined]

        h.on_start()

        assert h._protective_order_ids == {"P1": "S1"}

    def test_disabled_does_not_rehydrate(self) -> None:
        position = _FakePosition("P1", PositionSide.LONG)
        cache = _FakeCache(positions_open=[position])
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(None)
        h.config = type("C", (), {"instrument_id": _BTC})()  # type: ignore[attr-defined]

        h.on_start()
        # No rehydration ran → no log lines.
        assert h.log.info_calls == []

    def test_missing_config_instrument_id_no_crash(self) -> None:
        """Defensive: a strategy whose config doesn't expose
        ``instrument_id`` (multi-instrument scenarios) should NOT crash
        on_start.  Rehydration silently skips; the strategy is
        responsible for calling ``_protective_rehydrate`` explicitly
        with the right instrument(s)."""
        cache = _FakeCache()
        h = _RestartHarness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)
        # No h.config attribute at all.
        h.on_start()  # must not raise

    def test_chain_super_propagates(self) -> None:
        """``on_start`` is part of the cooperative super() chain — must
        call ``super().on_start()`` so downstream mixins (e.g. a future
        LiquidationAware that adds on_start) still see the lifecycle."""
        cache = _FakeCache()

        class _SpyChainEnd:
            def __init__(self) -> None:
                self.on_start_called = False

            def on_start(self) -> None:
                self.on_start_called = True

            def on_position_opened(self, event: object) -> None: ...
            def on_position_changed(self, event: object) -> None: ...
            def on_position_closed(self, event: object) -> None: ...
            def on_order_filled(self, event: object) -> None: ...
            def on_reset(self) -> None: ...

        class _Harness(ProtectiveStopAware, _SpyChainEnd):
            def __init__(self, cache: _FakeCache, log: _FakeLog) -> None:
                _SpyChainEnd.__init__(self)
                self.cache = cache
                self.log = log

        h = _Harness(cache=cache, log=_FakeLog())
        h._init_protective_stop(0.05)
        h.on_start()

        assert h.on_start_called is True
