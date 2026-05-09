"""ProtectiveStopAware strategy mixin for fixed-percent stop-loss exits.

Maintains a reduce-only ``StopMarketOrder`` at ``entry_price × (1 ± stop_pct)``
for every open position.  Independent of any other stop the strategy might
have (e.g. trailing, ATR bracket, cross-margin liquidation) — composes by
running its own reduce-only order alongside.

The intent is **isolated-margin equivalence under cross-margin accounting**:
when ``stop_pct = 1 / venue_leverage``, the worst-case loss per trade equals
the initial margin committed (``notional × stop_pct = notional / leverage``).
See ``docs/LIQUIDATION_AND_SIZING.md`` for the IM ≠ risk-budget discussion.

Usage — **inheritance order is non-negotiable, mixin first**::

    class MACross(ProtectiveStopAware, LiquidationAware, Strategy):
        # mixins first — reverse order silently disables them

NT calls the typed handlers (``on_position_opened`` etc.) by name.
``Strategy`` defines those as concrete no-op stubs (``trading/strategy.pyx``),
so ``(Strategy, ProtectiveStopAware)`` order finds the no-op first via MRO
and the mixin's override never runs.

Composes with :class:`LiquidationAware` — both place reduce-only stops at
different prices; whichever fires first reduces the position (NT's
reduce-only logic cancels the other on fill).  Order in the MRO does not
affect correctness here because the two mixins don't share state, but
``ProtectiveStopAware`` typically goes first by convention (tighter stop,
fires first).

Requires:

- ``_init_protective_stop(stop_pct)`` called from the strategy's
  ``__init__``.  ``stop_pct`` is a fraction (e.g. ``0.05`` = 5%);
  pass ``None`` to disable.
- ``super().on_reset()`` in every strategy's ``on_reset`` override —
  the mixin uses it to clear ``_protective_order_ids`` between sweep
  iterations.

Orders are tagged ``["protective_stop"]`` for downstream identification
(e.g. close-cause analysis in notebooks: filter ``cache.orders()`` by
``"protective_stop" in order.tags``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.core.rust.model import OrderSide, PositionSide

if TYPE_CHECKING:
    from nautilus_trader.model.events import (
        OrderFilled,
        PositionChanged,
        PositionClosed,
        PositionOpened,
    )
    from nautilus_trader.model.identifiers import ClientOrderId, PositionId


# Order tag used for protective-stop orders. Notebooks/tests filter
# cache.orders() by this tag to identify protective-stop-driven exits.
PROTECTIVE_STOP_TAG = "protective_stop"


class ProtectiveStopAware:
    """Strategy mixin for fixed-percent protective stop-loss orders.

    See module docstring for the inheritance-order requirement.
    """

    # Type-only declarations.  Concrete state is created by
    # ``_init_protective_stop``; missing-state guards in
    # ``_protective_enabled`` make the mixin no-op silently if init is
    # forgotten.
    _protective_stop_pct: Decimal | None
    _protective_order_ids: dict[PositionId, ClientOrderId]
    _protective_count: int

    # ── Init ────────────────────────────────────────────────────────────────

    def _init_protective_stop(self, stop_pct: float | Decimal | None) -> None:
        """Initialize protective-stop state.

        Call this from the strategy's ``__init__`` after
        ``super().__init__()``.  ``stop_pct`` is a fraction (``0.05`` = 5%);
        pass ``None`` to disable the mixin's handlers.

        Parameters
        ----------
        stop_pct
            Fraction of entry price at which to place the protective stop.
            ``None`` (or ``0`` / negative) disables the mixin.

        Raises
        ------
        ValueError
            If ``stop_pct`` is set but >= 1 (a stop at >= 100% off entry
            crosses zero or wraps around — almost certainly a unit-of-
            measure error, e.g. passing ``5.0`` meaning "5%" instead of
            ``0.05``).

        """
        if stop_pct is None:
            self._protective_stop_pct = None
        else:
            value = Decimal(str(stop_pct))
            if value <= 0:
                self._protective_stop_pct = None
            elif value >= 1:
                msg = (
                    f"stop_pct={value} must be a fraction in (0, 1) — got a value "
                    f">= 1, which would place the stop at or past zero entry-price.  "
                    f"Did you pass a percentage (e.g. 5.0 meaning 5%) instead of a "
                    f"fraction (0.05)?"
                )
                raise ValueError(msg)
            else:
                self._protective_stop_pct = value
        self._protective_order_ids = {}
        self._protective_count = 0

    # ── Internal helpers ────────────────────────────────────────────────────

    def _protective_enabled(self) -> bool:
        """Whether protective-stop placement should run for this strategy.

        Defensive: if ``_init_protective_stop`` was forgotten,
        ``_protective_stop_pct`` won't exist as an attribute and we
        silently disable rather than crashing on the first event.
        """
        return getattr(self, "_protective_stop_pct", None) is not None

    def _protective_close_side(self, position_side: PositionSide) -> OrderSide:
        return OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY

    def _protective_compute_stop_price(
        self,
        side: PositionSide,
        entry_price: Decimal,
    ) -> Decimal:
        """Stop trigger = ``entry × (1 - stop_pct)`` for LONG, ``× (1 + stop_pct)`` for SHORT."""
        pct = self._protective_stop_pct
        assert pct is not None  # _protective_enabled() guards
        if side == PositionSide.LONG:
            return entry_price * (Decimal(1) - pct)
        return entry_price * (Decimal(1) + pct)

    def _protective_should_skip(
        self,
        side: PositionSide,
        entry_price: Decimal,
        stop_price: Decimal,
    ) -> bool:
        """Return True if the mixin should not submit a protective stop.

        Defensive: stop on the wrong side of entry would fire immediately
        and is meaningless.  This shouldn't occur with a valid ``stop_pct``
        in (0, 1), but guards against corner cases (zero/negative entry
        prices, etc.).
        """
        if entry_price <= 0:
            return True
        if side == PositionSide.LONG:
            return stop_price >= entry_price
        return stop_price <= entry_price

    def _protective_issue_stop(
        self,
        instrument_id: Any,
        position_id: PositionId,
        side: PositionSide,
        quantity: Any,
        entry_price: Decimal,
    ) -> None:
        """Compute trigger and submit a reduce-only stop tagged ``protective_stop``.

        Used by both ``on_position_opened`` and ``on_position_changed``.
        Returns silently if the computed trigger is on the wrong side of
        entry (defensive, shouldn't happen with valid ``stop_pct``).
        """
        stop_price = self._protective_compute_stop_price(side, entry_price)
        if self._protective_should_skip(side, entry_price, stop_price):
            return

        instrument = self.cache.instrument(instrument_id)  # type: ignore[attr-defined]
        trigger = instrument.make_price(stop_price)

        order = self.order_factory.stop_market(  # type: ignore[attr-defined]
            instrument_id=instrument_id,
            order_side=self._protective_close_side(side),
            quantity=quantity,
            trigger_price=trigger,
            reduce_only=True,
            tags=[PROTECTIVE_STOP_TAG],
        )
        self._protective_order_ids[position_id] = order.client_order_id
        self.submit_order(order)  # type: ignore[attr-defined]

    def _protective_cancel_stop(self, position_id: PositionId) -> None:
        """Cancel and forget the protective stop for a position, if any."""
        order_id = self._protective_order_ids.pop(position_id, None)
        if order_id is None:
            return
        order = self.cache.order(order_id)  # type: ignore[attr-defined]
        if order is not None and order.is_open:
            self.cancel_order(order)  # type: ignore[attr-defined]

    # ── Position lifecycle handlers ─────────────────────────────────────────
    #
    # Each handler calls ``super().on_*()`` FIRST so events propagate down
    # the MRO chain (e.g. to ``LiquidationAware``) even when this mixin is
    # disabled (``stop_pct=None``).  Without this, a strategy declared as
    # ``(ProtectiveStopAware, LiquidationAware, Strategy)`` with stop_pct
    # set to None would silently bypass ``LiquidationAware`` — meaning no
    # per-position cross-margin liq stop gets placed.  Cooperative super()
    # is what makes the mixin chain composable.

    def on_position_opened(self, event: PositionOpened) -> None:
        """Submit a reduce-only stop at ``entry × (1 ± stop_pct)``."""
        super().on_position_opened(event)  # type: ignore[misc]
        if not self._protective_enabled():
            return
        self._protective_issue_stop(
            instrument_id=event.instrument_id,
            position_id=event.position_id,
            side=event.side,
            quantity=event.quantity,
            entry_price=Decimal(str(event.last_px)),
        )

    def on_position_changed(self, event: PositionChanged) -> None:
        """Cancel the stale protective stop and submit a new one.

        Re-issues based on the position's average open price after the
        change — important for adds/scale-ins where the average shifts.
        For close-and-reverse flips, the new average is the new entry.
        """
        super().on_position_changed(event)  # type: ignore[misc]
        if not self._protective_enabled():
            return
        self._protective_cancel_stop(event.position_id)
        self._protective_issue_stop(
            instrument_id=event.instrument_id,
            position_id=event.position_id,
            side=event.side,
            quantity=event.quantity,
            entry_price=Decimal(str(event.avg_px_open)),
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        """Cancel any remaining protective order for this position."""
        super().on_position_closed(event)  # type: ignore[misc]
        if not self._protective_enabled():
            return
        self._protective_cancel_stop(event.position_id)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Detect protective-stop fills and bump the counter (telemetry).

        Tags live on the ``Order``, not on the ``OrderFilled`` event, so
        we look up the order from the cache and inspect its tags.
        Notebooks reading the ``fills_report`` can do the same lookup
        for close-cause analysis.
        """
        super().on_order_filled(event)  # type: ignore[misc]
        if not self._protective_enabled():
            return

        order = self.cache.order(event.client_order_id)  # type: ignore[attr-defined]
        if order is None:
            return
        tags = order.tags or []
        if PROTECTIVE_STOP_TAG not in tags:
            return

        self._protective_count += 1
        self.log.info(  # type: ignore[attr-defined]
            f"PROTECTIVE STOP FILLED: {event.instrument_id} "
            f"trigger={getattr(order, 'trigger_price', '?')} "
            f"fill={event.last_px}",
        )

    def on_reset(self) -> None:
        """Clear protective-stop state between sweep iterations.

        Strategy subclasses **must** call ``super().on_reset()`` in their
        own override or this clear never fires — iteration N+1 would
        inherit stale ``_protective_order_ids`` from iteration N.
        """
        super().on_reset()  # type: ignore[misc]
        self._protective_order_ids = {}
        self._protective_count = 0
