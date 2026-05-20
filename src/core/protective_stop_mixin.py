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
- ``super().on_start()`` in every strategy's ``on_start`` override —
  the mixin uses it to rehydrate the position→order map from the
  cache after restart-time reconciliation.  Without it, restart
  recovery is impaired (see "Restart safety" below).

Orders are tagged ``["protective_stop"]`` for downstream identification
(e.g. close-cause analysis in notebooks: filter ``cache.orders()`` by
``"protective_stop" in order.tags``).  Tags are **not** load-bearing
for restart safety — reconciliation strips them — so the mixin
identifies pre-existing stops by structural properties (reduce-only,
STOP_MARKET, close-side opposite of position).

Restart safety
==============

On container restart, three layers of state are at play:

1. **HL server-side** — the reduce-only stop order itself, still
   sitting in HL's order book.
2. **NT Redis cache** — if configured, the order's client/venue id
   mapping and the position record survive process restart.
3. **NT live reconciliation** — on startup the exec engine queries
   HL for open orders + positions and reconciles them into the
   cache (default-on in ``LiveExecEngineConfig``).

The mixin's in-memory ``_protective_order_ids`` dict is lost on every
restart and rebuilt by :meth:`_protective_rehydrate`, which the mixin
runs from ``on_start`` after NT's reconciliation has settled.  The
key invariant restored by rehydration is:

    for every open position with a matching reduce-only STOP_MARKET
    order on the close-side, the order's client_order_id is recorded
    in ``_protective_order_ids[position_id]``.

Failure modes covered:

- **Redis cache survives, fresh process.** Rehydration finds the
  cached order and position; mapping rebuilt; no duplicate stop
  submitted; ``on_position_closed`` later cancels the correct stop.
- **Cache wiped, HL still has stop + position.** Reconciliation
  recreates the order (tag stripped to ``"VENUE"``) and synthesises
  a position-aligning fill.  The fill fires ``on_position_opened``
  → :meth:`_protective_issue_stop` checks idempotently for an
  existing reduce-only stop and skips submission, just recording
  the mapping.
- **Stop fired during outage.** HL has no open position or stop.
  Rehydration is a no-op.  Strategy starts flat as expected.
- **Two reduce-only stops on same position** (operator error or
  prior bug).  Rehydration logs a warning and binds to the first;
  no auto-cleanup — leaves to the operator.

What rehydration deliberately does NOT do:

- Re-price an existing stop.  If the cached/reconciled stop's
  trigger drifted from the current ``entry × (1 ± stop_pct)``
  (e.g. position averaged in across restarts), we trust the
  existing stop rather than briefly removing protection to
  re-submit.  The audit log records the discrepancy.
- Handle hedging-mode multi-position-per-instrument matching.
  Current strategies are NETTING (single position per instrument);
  if hedging is adopted, rehydration needs ``order.position_id``
  matching rather than the side-only heuristic.

See ``docs/PROTECTIVE_STOP_RESTART_AUDIT.md`` for the full failure
mode enumeration and the NT 1.226 reconciliation references.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.core.rust.model import OrderSide, OrderType, PositionSide

if TYPE_CHECKING:
    from nautilus_trader.model.events import (
        OrderFilled,
        PositionChanged,
        PositionClosed,
        PositionOpened,
    )
    from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId
    from nautilus_trader.model.orders import Order


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

    def _protective_find_existing_stop(
        self,
        instrument_id: InstrumentId,
        close_side: OrderSide,
    ) -> Order | None:
        """Return an open reduce-only ``STOP_MARKET`` order matching the
        close-side, or ``None``.

        Tag-agnostic by design.  NT 1.226 reconciliation strips user tags
        when rebuilding orders from venue reports (see ``live/
        execution_engine.py::_generate_order``: tags become ``["VENUE"]``
        for unclaimed externals, ``None`` for claimed ones).  So
        protective stops restored from HL after a cache wipe wouldn't
        match a ``PROTECTIVE_STOP_TAG`` filter — we identify by structural
        properties that survive reconciliation instead.

        In NETTING-mode strategies (project default) there is at most one
        open position per instrument, so ``(instrument_id, close_side)``
        uniquely identifies the protective stop.  Hedging-mode adopters
        would need ``order.position_id`` matching here.
        """
        for order in self.cache.orders_open(instrument_id=instrument_id):  # type: ignore[attr-defined]
            if (
                order.order_type == OrderType.STOP_MARKET
                and order.is_reduce_only
                and order.side == close_side
            ):
                return order
        return None

    def _protective_issue_stop(
        self,
        instrument_id: InstrumentId,
        position_id: PositionId,
        side: PositionSide,
        quantity: Any,
        entry_price: Decimal,
    ) -> None:
        """Compute trigger and submit a reduce-only stop tagged ``protective_stop``.

        Used by both ``on_position_opened`` and ``on_position_changed``.
        Returns silently if the computed trigger is on the wrong side of
        entry (defensive, shouldn't happen with valid ``stop_pct``).

        Idempotency
        -----------
        If the cache already shows an open reduce-only ``STOP_MARKET``
        order on the matching close-side for this instrument, we bind to
        the existing order rather than submitting a duplicate.  This
        prevents double-stops on container restart paths where:

        - NT reconciliation rebuilt the order from HL into the cache
          before the strategy started, then a synthetic position-aligning
          fill emitted ``PositionOpened`` to the mixin; or
        - rehydration (:meth:`_protective_rehydrate`) ran but a
          ``PositionChanged`` event landed before re-binding completed.

        If the existing stop's trigger differs from the freshly-computed
        value (e.g. position averaged in across restarts), the mixin
        logs the discrepancy and keeps the existing stop.  Re-pricing
        would briefly leave the position unprotected, which is worse
        than a slightly stale trigger.
        """
        stop_price = self._protective_compute_stop_price(side, entry_price)
        if self._protective_should_skip(side, entry_price, stop_price):
            return

        close_side = self._protective_close_side(side)
        existing = self._protective_find_existing_stop(instrument_id, close_side)
        if existing is not None:
            self._protective_order_ids[position_id] = existing.client_order_id
            existing_trigger = getattr(existing, "trigger_price", None)
            if existing_trigger is not None and Decimal(str(existing_trigger)) != stop_price:
                self.log.warning(  # type: ignore[attr-defined]
                    f"PROTECTIVE STOP IDEMPOTENT BIND: {instrument_id} "
                    f"position={position_id} existing_trigger={existing_trigger} "
                    f"(client_order_id={existing.client_order_id}) "
                    f"differs from recomputed={stop_price}; keeping existing "
                    "to avoid an unprotected window",
                )
            else:
                self.log.info(  # type: ignore[attr-defined]
                    f"PROTECTIVE STOP IDEMPOTENT BIND: {instrument_id} "
                    f"position={position_id} bound to existing "
                    f"client_order_id={existing.client_order_id}",
                )
            return

        instrument = self.cache.instrument(instrument_id)  # type: ignore[attr-defined]
        trigger = instrument.make_price(stop_price)

        order = self.order_factory.stop_market(  # type: ignore[attr-defined]
            instrument_id=instrument_id,
            order_side=close_side,
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

    def _protective_rehydrate(self, instrument_id: InstrumentId) -> None:
        """Rebuild ``_protective_order_ids`` from the live cache after restart.

        Called from :meth:`on_start` once NT's live reconciliation has
        settled (the kernel's startup sequence guarantees reconciliation
        completes before ``trader.start()`` fires ``on_start`` — see
        ``nautilus_trader/system/kernel.py::start_async``).

        Matches each open position for ``instrument_id`` to an open
        reduce-only ``STOP_MARKET`` order on the close-side, recording
        the mapping for later cancel-on-close.  Logs an audit summary
        and a warning per anomaly (position without a stop, extra
        stops beyond positions).

        Safe to call multiple times — fully overwrites the in-memory
        mapping from cache state on each invocation.

        No-op when the mixin is disabled (``stop_pct=None``).
        """
        if not self._protective_enabled():
            return

        positions_open = self.cache.positions_open(instrument_id=instrument_id)  # type: ignore[attr-defined]
        stop_orders: list[Order] = [
            order
            for order in self.cache.orders_open(instrument_id=instrument_id)  # type: ignore[attr-defined]
            if order.order_type == OrderType.STOP_MARKET and order.is_reduce_only
        ]

        # Clear and rebuild from cache truth.
        self._protective_order_ids = {}

        used_orders: set[Any] = set()
        for position in positions_open:
            close_side = self._protective_close_side(position.side)
            match = next(
                (
                    o for o in stop_orders
                    if o.side == close_side and o.client_order_id not in used_orders
                ),
                None,
            )
            if match is None:
                self.log.warning(  # type: ignore[attr-defined]
                    f"PROTECTIVE STOP REHYDRATE: {instrument_id} position "
                    f"{position.id} side={position.side.name} has no matching "
                    "reduce-only STOP_MARKET on the close-side — protection gap. "
                    "Next on_position_changed/on_bar will re-issue if the "
                    "strategy emits an event; otherwise consider operator review.",
                )
                continue
            self._protective_order_ids[position.id] = match.client_order_id
            used_orders.add(match.client_order_id)

        leftover = [o for o in stop_orders if o.client_order_id not in used_orders]
        for order in leftover:
            self.log.warning(  # type: ignore[attr-defined]
                f"PROTECTIVE STOP REHYDRATE: {instrument_id} unbound "
                f"reduce-only STOP_MARKET {order.client_order_id} "
                f"side={order.side.name} — possible orphaned stop from a prior "
                "run or a duplicate.  Not auto-cancelled; operator review.",
            )

        self.log.info(  # type: ignore[attr-defined]
            f"PROTECTIVE STOP REHYDRATE: {instrument_id} "
            f"positions={len(positions_open)} stops={len(stop_orders)} "
            f"bound={len(self._protective_order_ids)} "
            f"unbound={len(leftover)}",
        )

    # ── Position lifecycle handlers ─────────────────────────────────────────
    #
    # Each handler calls ``super().on_*()`` FIRST so events propagate down
    # the MRO chain (e.g. to ``LiquidationAware``) even when this mixin is
    # disabled (``stop_pct=None``).  Without this, a strategy declared as
    # ``(ProtectiveStopAware, LiquidationAware, Strategy)`` with stop_pct
    # set to None would silently bypass ``LiquidationAware`` — meaning no
    # per-position cross-margin liq stop gets placed.  Cooperative super()
    # is what makes the mixin chain composable.

    def on_start(self) -> None:
        """Rehydrate the position→stop-order map from the cache.

        Calls ``super().on_start()`` first per the cooperative-super
        convention (downstream mixins like ``LiquidationAware`` may add
        their own ``on_start`` hooks later).  Then, if the mixin is
        enabled and the strategy exposes a ``config.instrument_id``,
        runs :meth:`_protective_rehydrate` to rebuild the in-memory
        map from cache truth.

        Strategy subclasses **must** call ``super().on_start()`` in
        their own override.  Without it, rehydration silently doesn't
        run and restart recovery degrades to "no in-memory mapping;
        cancel-on-close becomes a no-op; orphan stops accumulate
        across position turns."  The strategy still functions, but
        the safety property documented in the module docstring is
        lost.

        Backtest path: in a ``BacktestEngine`` run, the cache is empty
        at start, so this is a logged no-op.  Only paper/live runs see
        non-empty rehydration.
        """
        super().on_start()  # type: ignore[misc]
        if not self._protective_enabled():
            return
        instrument_id = getattr(getattr(self, "config", None), "instrument_id", None)
        if instrument_id is None:
            # Strategy doesn't expose config.instrument_id — caller must
            # invoke _protective_rehydrate explicitly with the right
            # instrument(s).  Defensive: don't crash on multi-instrument
            # strategies that haven't opted in yet.
            return
        self._protective_rehydrate(instrument_id)

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
