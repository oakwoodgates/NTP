"""EMA regime-based strategy with breakout confirmation entry and trailing stop exit.

Adapted from NautilusTrader's example ``ema_cross_stop_entry.py``.  Combines
**breakout confirmation** with **trailing stop exit** for a classic
trend-following setup: only enter if the next bar confirms the EMA regime
by breaking beyond the previous bar's extreme, then trail the stop to let
winners run.

Order flow:
1. Each bar while flat + EMA regime aligned: record a breakout trigger level.
   - BUY: trigger at bar.high + (entry_offset_ticks x tick_size)
   - SELL: trigger at bar.low - (entry_offset_ticks x tick_size)
2. On the NEXT bar: if bar.high >= trigger (BUY) or bar.low <= trigger
   (SELL), submit a MARKET entry order.
3. Entry fill -> on_event detects PositionOpened -> submit trailing stop on
   opposite side (reduce_only=True).
4. Trailing stop fills -> position closed -> re-entry on next bar if regime
   still holds.

Behavioral differences from EMACrossTrailing:
- EMACrossTrailing enters immediately on any bar while in regime.
  EMACrossStopEntry requires the next bar to break beyond the previous
  bar's high/low — filtering false EMA signals via breakout confirmation.
- Both use trailing stop exit.  Both are regime-based (re-enter after exit
  if regime holds).

No EMA reversal handling — the trailing stop is the sole exit mechanism.
The strategy does NOT close on EMA flip while in position.

NOTE — Bar-based backtesting:
  The original NT example uses MarketIfTouched orders, which only trigger
  in tick-level or live trading.  In bar-only backtests, MIT triggers are
  never evaluated (synthetic ticks from bar decomposition are not published
  to the OrderEmulator, and the SimulatedExchange's bid/ask are never
  initialized from OHLCV data).  This strategy checks the breakout
  condition manually in ``on_bar`` and uses MARKET orders for entry,
  which is the standard approach for bar-based breakout strategies.

Fill price note:
  The original NT example's MIT fills at the trigger price (breakout level).
  This bar-based version fills at the next bar's open after confirmation —
  slightly more conservative, but intra-bar precision isn't available from
  bar data anyway.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.message import Event
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import (
    OrderSide,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.events import (
    OrderFilled,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import MarketOrder, TrailingStopMarketOrder
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class EMACrossStopEntryConfig(StrategyConfig, frozen=True):
    """Configuration for EMACrossStopEntry strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_notional : Decimal
        The USD notional amount per trade. Quantity is computed dynamically
        from trade_notional / entry_price at each entry.
    fast_ema_period : int, default 10
        The fast EMA period.
    slow_ema_period : int, default 20
        The slow EMA period.
    atr_period : int, default 20
        The ATR period for trailing stop offset sizing.
    trailing_atr_multiple : float, default 3.0
        Trailing stop offset = ATR x this multiplier.
    entry_offset_ticks : int, default 2
        Number of ticks above bar.high (BUY) or below bar.low (SELL)
        for the breakout trigger. Requires the next bar to confirm the
        breakout before entering.
    trailing_offset_type : str, default "PRICE"
        The trailing offset type (interpreted as ``TrailingOffsetType``).
    trigger_type : str, default "LAST_PRICE"
        The trigger type for the trailing stop (interpreted as ``TriggerType``).
    emulation_trigger : str, default "NO_TRIGGER"
        Emulation trigger for trailing stop orders.  ``"NO_TRIGGER"`` means
        the exchange (or SimulatedExchange) manages the trailing stop natively.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_notional: Decimal
    fast_ema_period: PositiveInt = 10
    slow_ema_period: PositiveInt = 20
    atr_period: PositiveInt = 20
    trailing_atr_multiple: PositiveFloat = 3.0
    entry_offset_ticks: PositiveInt = 2
    trailing_offset_type: str = "PRICE"
    trigger_type: str = "LAST_PRICE"
    emulation_trigger: str = "NO_TRIGGER"
    close_positions_on_stop: bool = True


class EMACrossStopEntry(Strategy):
    """EMA regime-based strategy with breakout confirmation and trailing stop.

    On each bar while flat and EMA regime aligned, records a breakout trigger
    level (bar.high + offset for BUY, bar.low - offset for SELL).  On the
    next bar, if the bar's range crosses the trigger, enters via MARKET order.

    Exits via trailing stop market order submitted on position open.
    No EMA reversal handling — the trailing stop is the sole exit.

    After a trailing stop exit, re-enters on the next bar if the EMA regime
    still holds (regime-based, not crossover-based).

    Parameters
    ----------
    config : EMACrossStopEntryConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If fast_ema_period >= slow_ema_period.

    """

    def __init__(self, config: EMACrossStopEntryConfig) -> None:
        PyCondition.is_true(
            config.fast_ema_period < config.slow_ema_period,
            f"{config.fast_ema_period=} must be less than {config.slow_ema_period=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None
        self.tick_size: Price | None = None

        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self.atr = AverageTrueRange(config.atr_period)

        # Order/position state for lifecycle management
        self.entry: MarketOrder | None = None
        self.trailing_stop: TrailingStopMarketOrder | None = None
        self.position_id: PositionId | None = None

        # Breakout trigger state (checked on next bar)
        self._pending_side: OrderSide | None = None
        self._pending_trigger: Price | None = None

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to data."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.tick_size = self.instrument.price_increment

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.atr)

        lookback_bars = max(self.config.slow_ema_period, self.config.atr_period) + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Needed for trailing stop trigger and Sandbox fills
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Check breakout trigger, then set next trigger if flat + in regime."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        # Check pending breakout trigger from previous bar
        if self._pending_side is not None and self._pending_trigger is not None:
            triggered = False
            if self._pending_side == OrderSide.BUY:
                triggered = bar.high >= self._pending_trigger
            else:
                triggered = bar.low <= self._pending_trigger

            if triggered and self.portfolio.is_flat(self.config.instrument_id):
                self._market_enter(self._pending_side, bar)
                self._pending_side = None
                self._pending_trigger = None
                return

        # Only set new trigger when flat — trailing stop handles exits
        if not self.portfolio.is_flat(self.config.instrument_id):
            self._pending_side = None
            self._pending_trigger = None
            return

        # Set breakout trigger for next bar
        if self.instrument is None or self.tick_size is None:
            return

        offset = self.tick_size * self.config.entry_offset_ticks

        if self.fast_ema.value >= self.slow_ema.value:
            self._pending_side = OrderSide.BUY
            self._pending_trigger = self.instrument.make_price(bar.high + offset)
        else:
            self._pending_side = OrderSide.SELL
            self._pending_trigger = self.instrument.make_price(bar.low - offset)

    def _market_enter(self, side: OrderSide, bar: Bar) -> None:
        """Submit a MARKET entry order after breakout confirmation.

        Parameters
        ----------
        side : OrderSide
            BUY or SELL.
        bar : Bar
            Current bar (close used for qty sizing).

        """
        if self.instrument is None:
            self.log.error("Instrument not loaded — cannot enter position")
            return

        price_dec = Decimal(str(bar.close))
        if price_dec <= 0:
            self.log.warning("Invalid price — cannot compute quantity")
            return

        qty = self.instrument.make_qty(self.config.trade_notional / price_dec)
        if qty <= 0:
            self.log.warning(
                f"Computed qty=0 for notional={self.config.trade_notional} "
                f"at price={price_dec}"
            )
            return

        order: MarketOrder = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=qty,
        )
        self.entry = order
        self.submit_order(order)

    def on_event(self, event: Event) -> None:
        """Manage trailing stop lifecycle on position events."""
        if isinstance(event, OrderFilled):
            # Trailing stop was filled — clear reference
            if (
                self.trailing_stop is not None
                and event.client_order_id == self.trailing_stop.client_order_id
            ):
                self.trailing_stop = None

        elif isinstance(event, (PositionOpened, PositionChanged)):
            if self.trailing_stop is not None:
                return  # Already managing a trailing stop
            if (
                self.entry is not None
                and event.opening_order_id == self.entry.client_order_id
            ):
                self.position_id = event.position_id
                if event.entry == OrderSide.BUY:
                    self._submit_trailing_stop(OrderSide.SELL, event.quantity)
                elif event.entry == OrderSide.SELL:
                    self._submit_trailing_stop(OrderSide.BUY, event.quantity)

        elif isinstance(event, PositionClosed):
            self.position_id = None

    def _submit_trailing_stop(
        self,
        side: OrderSide,
        quantity: Quantity,
    ) -> None:
        """Submit a trailing stop market order to protect the open position."""
        if self.instrument is None:
            self.log.error("Instrument not loaded — cannot submit trailing stop")
            return

        offset = self.atr.value * self.config.trailing_atr_multiple
        order: TrailingStopMarketOrder = self.order_factory.trailing_stop_market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=quantity,
            trailing_offset=Decimal(
                f"{offset:.{self.instrument.price_precision}f}"
            ),
            trailing_offset_type=TrailingOffsetType[
                self.config.trailing_offset_type
            ],
            trigger_type=TriggerType[self.config.trigger_type],
            reduce_only=True,
            emulation_trigger=TriggerType[self.config.emulation_trigger],
        )
        self.trailing_stop = order
        self.submit_order(order, position_id=self.position_id)

    def on_stop(self) -> None:
        """Cancel all orders, optionally close positions, unsubscribe."""
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """Reset indicators and state for engine reuse (parameter sweeps)."""
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.atr.reset()
        self.entry = None
        self.trailing_stop = None
        self.position_id = None
        self._pending_side = None
        self._pending_trigger = None
