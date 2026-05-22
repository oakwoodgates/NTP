"""LiquidationAware strategy mixin for position-liquidation simulation.

Maintains a reduce-only StopMarketOrder at the cross-margin liquidation
price for every open position.  On fill, publishes ``PositionLiquidated``
on the message bus for the sweep runner to count.

Usage — **inheritance order is non-negotiable, mixin first**::

    class MACross(LiquidationAware, Strategy):
        # mixin first — reverse order silently disables the mixin

NT calls the typed handlers (``on_position_opened`` etc.) by name.
``Strategy`` defines those as concrete no-op stubs (``trading/strategy.pyx:755-801``),
so with ``(Strategy, LiquidationAware)`` order Python's MRO finds the
no-op on Strategy first and the mixin's override never runs.  With
``(LiquidationAware, Strategy)`` order, MRO hits the mixin's overrides
before the stubs.

Requires:

- ``_init_liquidation(config)`` called from the strategy's ``__init__``.
  ``config`` is a fully-resolved :class:`LiquidationConfig` (``mm_rate``
  populated from ``VenueConfig.mm_rate`` by ``make_engine``); pass ``None``
  to disable.
- ``super().on_reset()`` in every strategy's ``on_reset`` override —
  the mixin uses it to clear ``_liq_order_ids`` between sweep iterations.

Does NOT handle account-level halt — that lives in
``AccountAliveMonitor`` (``src/actors/account_alive.py``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.core.rust.model import OrderSide, PositionSide
from nautilus_trader.model.identifiers import ClientOrderId, PositionId

from src.core.liquidation import (
    TOPIC_POSITION_LIQUIDATED,
    LiquidationConfig,
    PositionLiquidated,
    compute_liquidation_price,
)

if TYPE_CHECKING:
    from nautilus_trader.model.events import (
        OrderFilled,
        PositionChanged,
        PositionClosed,
        PositionOpened,
    )


# Order tag used for cross-margin liquidation stops. Notebooks/tests filter
# cache.orders() by this tag to identify liq-stop-driven exits (parallel
# to PROTECTIVE_STOP_TAG in protective_stop_mixin).
LIQUIDATION_TAG = "liquidation"


class LiquidationAware:
    """Strategy mixin for position-liquidation simulation.

    See module docstring for the inheritance-order requirement.
    """

    # Type-only declarations.  Concrete state is created by
    # ``_init_liquidation``; missing-state guards in ``_liq_enabled``
    # and ``_safe_state`` make the mixin no-op silently if init is forgotten.
    _liq_config: LiquidationConfig | None
    _liq_order_ids: dict[PositionId, ClientOrderId]
    _liq_count: int

    # ── Init ────────────────────────────────────────────────────────────────

    def _init_liquidation(self, config: LiquidationConfig | None) -> None:
        """Initialize liquidation state.

        Call this from the strategy's ``__init__`` after ``super().__init__()``.
        ``config`` is a fully-resolved :class:`LiquidationConfig` —
        ``make_engine`` populates ``mm_rate`` from ``VenueConfig.mm_rate``
        before the strategy is constructed.  Passing ``None`` disables the
        mixin's handlers.
        """
        self._liq_config = config
        self._liq_order_ids = {}
        self._liq_count = 0

    # ── Internal helpers ────────────────────────────────────────────────────

    def _liq_enabled(self) -> bool:
        """Whether liquidation simulation should run for this strategy.

        Defensive: if ``_init_liquidation`` was forgotten, ``_liq_config``
        won't exist as an attribute and we silently disable rather than
        crashing on the first event.
        """
        cfg = getattr(self, "_liq_config", None)
        return cfg is not None and cfg.enabled

    def _liq_mm_rate(self) -> Decimal:
        """Read the resolved ``mm_rate`` from config.

        ``make_engine`` populates ``LiquidationConfig.mm_rate`` from
        ``VenueConfig.mm_rate`` before the strategy is constructed, so
        the field is always set when the mixin runs.
        """
        cfg = self._liq_config
        if cfg is None or cfg.mm_rate is None:
            # Should not happen — make_engine resolves mm_rate before
            # the strategy receives the LiquidationConfig.  Guard anyway
            # so a hand-constructed config doesn't crash silently.
            msg = (
                "LiquidationConfig.mm_rate is None — make_engine should "
                "have resolved this from VenueConfig.mm_rate before passing "
                "the config to the strategy."
            )
            raise ValueError(msg)
        return cfg.mm_rate

    def _liq_get_equity(self) -> Decimal:
        """Read current account equity from cache.

        Uses ``account.currencies()`` (what's actually in the account)
        rather than an instrument-derived currency. On Hyperliquid the
        instrument's cost_currency reports USDC (on-chain collateral)
        but the account is funded in USD (quote_currency, where PnL
        flows). See PersistenceActor for the full HL-specific rationale.
        """
        venue = self.config.instrument_id.venue  # type: ignore[attr-defined]
        account = self.cache.account_for_venue(venue)  # type: ignore[attr-defined]
        if account is None:
            return Decimal("0")
        currencies = list(account.currencies())
        if not currencies:
            return Decimal("0")
        balance = account.balance_total(currencies[0])
        if balance is None:
            return Decimal("0")
        return balance.as_decimal()  # type: ignore[no-any-return]

    def _liq_close_side(self, position_side: PositionSide) -> OrderSide:
        return OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY

    def _liq_should_skip_stop(
        self,
        side: PositionSide,
        entry_price: Decimal,
        liq_price: Decimal,
    ) -> bool:
        """Return True if the mixin should not submit a liquidation stop.

        Two cases produce a useless or invalid stop:

        1. **Already past liquidation.** ``equity ≤ notional × mm_rate`` at
           open means the position can't even be margined; a stop trigger
           on the wrong side of entry would fire immediately and the order
           is meaningless.
           - LONG: ``liq_price ≥ entry`` (above entry is "wrong side")
           - SHORT: ``liq_price ≤ entry``

        2. **Over-collateralised.** ``equity > notional × (1 + mm_rate)``
           means ``liq_distance > 1`` — for LONG, ``liq_price`` is negative
           (NT rejects it as an invalid trigger); for SHORT, ``liq_price``
           is at or above ``2 × entry`` (technically valid but unreachable
           in practice). No real liquidation can happen at this collateral
           level, so don't bother placing a stop.
           - LONG: ``liq_price ≤ 0``
           - SHORT: ``liq_price ≥ 2 × entry`` (equivalently ``liq_distance ≥ 1``)
        """
        if side == PositionSide.LONG:
            return liq_price >= entry_price or liq_price <= Decimal("0")
        # SHORT
        return liq_price <= entry_price or liq_price >= entry_price * Decimal("2")

    def _liq_issue_stop(
        self,
        instrument_id: Any,
        position_id: PositionId,
        side: PositionSide,
        quantity: Any,
        entry_price: Decimal,
    ) -> None:
        """Compute liq price and submit a reduce-only stop.

        Used by both ``on_position_opened`` and ``on_position_changed``.
        Returns silently if equity is already past the liquidation
        threshold (no useful order to place).
        """
        equity = self._liq_get_equity()
        notional = Decimal(str(quantity)) * entry_price
        if notional <= 0:
            return

        liq_price = compute_liquidation_price(
            entry_price=entry_price,
            side=side,
            equity=equity,
            notional=notional,
            mm_rate=self._liq_mm_rate(),
        )

        if self._liq_should_skip_stop(side, entry_price, liq_price):
            return

        instrument = self.cache.instrument(instrument_id)  # type: ignore[attr-defined]
        trigger = instrument.make_price(liq_price)

        order = self.order_factory.stop_market(  # type: ignore[attr-defined]
            instrument_id=instrument_id,
            order_side=self._liq_close_side(side),
            quantity=quantity,
            trigger_price=trigger,
            reduce_only=True,
            tags=[LIQUIDATION_TAG],
        )
        self._liq_order_ids[position_id] = order.client_order_id
        self.submit_order(order)  # type: ignore[attr-defined]

    def _liq_cancel_stop(self, position_id: PositionId) -> None:
        """Cancel and forget the liquidation stop for a position, if any."""
        order_id = self._liq_order_ids.pop(position_id, None)
        if order_id is None:
            return
        order = self.cache.order(order_id)  # type: ignore[attr-defined]
        if order is not None and order.is_open:
            self.cancel_order(order)  # type: ignore[attr-defined]

    # ── Position lifecycle handlers ─────────────────────────────────────────

    def on_position_opened(self, event: PositionOpened) -> None:
        """Submit a reduce-only stop at the liquidation price.

        Note: ``event.entry`` is an ``OrderSide`` (BUY/SELL); ``event.side``
        is the ``PositionSide`` (LONG/SHORT) we need for the formula.
        """
        if not self._liq_enabled():
            return
        self._liq_issue_stop(
            instrument_id=event.instrument_id,
            position_id=event.position_id,
            side=event.side,
            quantity=event.quantity,
            entry_price=Decimal(str(event.last_px)),
        )

    def on_position_changed(self, event: PositionChanged) -> None:
        """Cancel the stale liq stop and submit a new one for the changed position.

        v1: only handles close-and-reverse re-submission.  Multi-instrument
        equity-pool drift recompute is out of scope (single-position-per-strategy
        v1 only).
        """
        if not self._liq_enabled():
            return
        self._liq_cancel_stop(event.position_id)
        self._liq_issue_stop(
            instrument_id=event.instrument_id,
            position_id=event.position_id,
            side=event.side,
            quantity=event.quantity,
            entry_price=Decimal(str(event.avg_px_open)),
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        """Cancel any remaining liq order for this position."""
        if not self._liq_enabled():
            return
        self._liq_cancel_stop(event.position_id)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Detect liquidation fills and publish ``PositionLiquidated``.

        Tags live on the ``Order``, not on the ``OrderFilled`` event, so
        we look up the order from the cache and inspect its tags. The
        same lookup gets us the original trigger price for the
        ``PositionLiquidated`` event's slippage telemetry.
        """
        if not self._liq_enabled():
            return

        order = self.cache.order(event.client_order_id)  # type: ignore[attr-defined]
        if order is None:
            return
        tags = order.tags or []
        if LIQUIDATION_TAG not in tags:
            return

        self._liq_count += 1

        # Trigger price the mixin originally set (from cross-margin formula
        # at position-open time). Compare to fill price for gap-risk telemetry.
        trigger_price = (
            Decimal(str(order.trigger_price))
            if getattr(order, "trigger_price", None) is not None
            else Decimal("0")
        )

        # Position state at this point reflects the closing fill.
        position = self.cache.position(event.position_id)  # type: ignore[attr-defined]
        side = position.side if position is not None else PositionSide.FLAT
        entry_price = (
            Decimal(str(position.avg_px_open))
            if position is not None
            else Decimal("0")
        )
        realized_pnl = (
            position.realized_pnl.as_decimal()
            if position is not None and position.realized_pnl is not None
            else Decimal("0")
        )
        fill_price = Decimal(str(event.last_px))

        liq_event = PositionLiquidated(
            instrument_id=str(event.instrument_id),
            side=side,
            entry_price=entry_price,
            trigger_price=trigger_price,
            fill_price=fill_price,
            realized_pnl=realized_pnl,
            ts_event=event.ts_event,
        )

        slippage = fill_price - trigger_price if trigger_price > 0 else Decimal("0")
        self.log.warning(  # type: ignore[attr-defined]
            f"POSITION LIQUIDATED: {liq_event.instrument_id} "
            f"trigger={trigger_price} fill={fill_price} "
            f"(entry={entry_price}, slippage={slippage}, pnl={realized_pnl})",
        )

        # Plain msgbus.publish — accepts any object.  Sweep runner subscribes
        # to TOPIC_POSITION_LIQUIDATED and counts events.
        self.msgbus.publish(  # type: ignore[attr-defined]
            topic=TOPIC_POSITION_LIQUIDATED,
            msg=liq_event,
        )

    def on_start(self) -> None:
        """Terminate the cooperative ``super().on_start()`` chain.

        Deliberately does NOT call ``super().on_start()``. Without this,
        the chain ``MACross.on_start → ProtectiveStopAware.on_start →
        super → ...`` falls through to NT's ``Strategy.on_start``, which
        logs ``"The Strategy.on_start handler was called when not
        overridden"`` even though every level above IS overridden. The
        warning is a false positive — NT's base is a no-op stub that
        isn't designed to be reached via cooperative super().

        ``LiquidationAware`` is the last mixin in the MACross MRO before
        Strategy, so this is the right place to terminate. Mirrors the
        existing ``on_reset`` pattern in this same class (also no
        ``super()`` call).

        If a future mixin is inserted BELOW LiquidationAware in the MRO,
        this terminator will swallow its ``on_start`` — that future
        mixin should add its own terminator or document the constraint.
        """
        # Intentionally no super().on_start() call — see docstring.

    def on_reset(self) -> None:
        """Clear liquidation state between sweep iterations.

        Strategy subclasses **must** call ``super().on_reset()`` in their
        own override or this clear never fires — iteration N+1 would
        inherit stale ``_liq_order_ids`` from iteration N.
        """
        self._liq_order_ids = {}
        self._liq_count = 0

    # ── State persistence across restarts ───────────────────────────────────
    #
    # NT calls ``save()`` during graceful shutdown when the kernel is built
    # with ``save_state=True``.  We persist the ``position_id → order_id``
    # mapping so the mixin can still cancel the right reduce-only stop on
    # ``on_position_closed`` after a restart.  See the analogous block in
    # ``protective_stop_mixin.py`` for the full rationale.
    #
    # ``on_save`` / ``on_load`` call ``super()`` here even though the
    # legacy position-event handlers don't — the persistence chain is a
    # NEW cooperative path that doesn't need to match the legacy handlers'
    # non-cooperative pattern.  Any mixin combined with this one MUST
    # implement ``on_save`` / ``on_load`` cooperatively if it wants its
    # state persisted.

    # Use mixin-prefixed attribute names so multiple mixins co-existing on
    # the same strategy can each look up their own state key via ``self.``
    # without colliding through Python's MRO attribute resolution.
    # (``self._STATE_KEY_*`` would resolve to whichever mixin appears first
    # in MRO — a footgun when two mixins independently define the same key.)
    _LIQ_STATE_KEY_ORDER_IDS = "liq_order_ids"
    _LIQ_STATE_KEY_COUNT = "liq_count"

    def on_save(self) -> dict[str, bytes]:
        """Persist the liquidation ``position_id → order_id`` mapping + counter.

        Encodes the dict as JSON ``{position_id_str: order_id_str, ...}``.
        ``PositionId`` and ``ClientOrderId`` round-trip via their string
        values (verified in NT 1.226.0).
        """
        state: dict[str, bytes] = super().on_save()  # type: ignore[misc]
        if hasattr(self, "_liq_order_ids"):
            serializable = {
                pos_id.value: order_id.value
                for pos_id, order_id in self._liq_order_ids.items()
            }
            state[self._LIQ_STATE_KEY_ORDER_IDS] = json.dumps(serializable).encode()
        if hasattr(self, "_liq_count"):
            state[self._LIQ_STATE_KEY_COUNT] = str(self._liq_count).encode()
        return state

    def on_load(self, state: dict[str, bytes]) -> None:
        """Restore the liquidation mapping + counter.

        Defensive: missing or unparseable values fall back to empty dict /
        zero count.  A state-load error must not stop the trader.
        """
        super().on_load(state)  # type: ignore[misc]
        raw_ids = state.get(self._LIQ_STATE_KEY_ORDER_IDS)
        if raw_ids is not None:
            try:
                decoded = json.loads(raw_ids.decode())
                self._liq_order_ids = {
                    PositionId(pos_id): ClientOrderId(order_id)
                    for pos_id, order_id in decoded.items()
                }
            except (ValueError, TypeError) as exc:
                self.log.warning(  # type: ignore[attr-defined]
                    f"on_load: invalid {self._LIQ_STATE_KEY_ORDER_IDS}={raw_ids!r} "
                    f"({exc}); resetting to empty",
                )
                self._liq_order_ids = {}
        raw_count = state.get(self._LIQ_STATE_KEY_COUNT)
        if raw_count is not None:
            try:
                self._liq_count = int(raw_count)
            except ValueError:
                self.log.warning(  # type: ignore[attr-defined]
                    f"on_load: invalid {self._LIQ_STATE_KEY_COUNT}={raw_count!r}; "
                    "resetting to 0",
                )
                self._liq_count = 0
