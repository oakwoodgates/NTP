"""MA crossover strategy with ATR-based trailing stop exit (EMA / SMA / HMA / DEMA / AMA / VIDYA).

Uses MA regime alignment for entries (enters when flat and fast MA >= slow
for longs, fast < slow for shorts).  Exits via trailing stop market orders
managed through ``on_event()`` — NT's engine adjusts the trigger price
automatically as the market moves favorably.

After a trailing stop exit the strategy re-enters immediately on the next
bar if the MA regime still holds.  This is regime-based entry (like
``ma_cross.py``), not crossover-based (like ``ema_cross_atr.py``).

Trailing offset = ATR × ``trailing_atr_multiple``.  NT's SimulatedExchange
handles trailing stops natively in backtesting (no emulation needed).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.message import Event
from nautilus_trader.indicators import (
    AdaptiveMovingAverage,
    AverageTrueRange,
    MovingAverageFactory,
    MovingAverageType,
)
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TrailingOffsetType, TriggerType
from nautilus_trader.model.events import (
    OrderFilled,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import MarketOrder, TrailingStopMarketOrder
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument

_MA_TYPE_LOOKUP: dict[str, MovingAverageType] = {
    "SMA":   MovingAverageType.SIMPLE,
    "EMA":   MovingAverageType.EXPONENTIAL,
    "HMA":   MovingAverageType.HULL,
    "DEMA":  MovingAverageType.DOUBLE_EXPONENTIAL,
    "AMA":   MovingAverageType.ADAPTIVE,
    "VIDYA": MovingAverageType.VARIABLE_INDEX_DYNAMIC,
}


class MACrossTrailingStopConfig(StrategyConfig, frozen=True):
    """Configuration for MACrossTrailingStop strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_notional : Decimal
        The USD notional size per trade.  Quantity is computed at entry
        time as ``trade_notional / current_price``.
    ma_type : str, default "EMA"
        Moving average type: ``"EMA"`` | ``"SMA"`` | ``"HMA"`` |
        ``"DEMA"`` | ``"AMA"`` | ``"VIDYA"``.
    fast_period : int, default 10
        The fast MA period.
    slow_period : int, default 20
        The slow MA period.
    atr_period : int, default 20
        The ATR period for trailing offset sizing.
    trailing_atr_multiple : float, default 3.0
        Trailing stop offset = ATR × this multiplier.
    trailing_offset_type : str, default "PRICE"
        The trailing offset type (interpreted as ``TrailingOffsetType``).
        Common values: ``"PRICE"`` (fixed price distance),
        ``"BASIS_POINTS"`` (percentage), ``"TICKS"`` (tick count).
    trigger_type : str, default "LAST_PRICE"
        The trigger type for the trailing stop (interpreted as ``TriggerType``).
    emulation_trigger : str, default "NO_TRIGGER"
        Emulation trigger for trailing stop orders.  ``"NO_TRIGGER"`` means
        the exchange (or SimulatedExchange) manages the trailing stop
        natively.  Set to ``"LAST_PRICE"`` to emulate via NT.
    ama_alpha_fast : int, default 2
        Fast smoothing constant period for AMA (Kaufman).
        Only used when ``ma_type="AMA"``.
    ama_alpha_slow : int, default 30
        Slow smoothing constant period for AMA (Kaufman).
        Only used when ``ma_type="AMA"``.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_notional: Decimal
    ma_type: str = "EMA"
    fast_period: PositiveInt = 10
    slow_period: PositiveInt = 20
    atr_period: PositiveInt = 20
    trailing_atr_multiple: PositiveFloat = 3.0
    trailing_offset_type: str = "PRICE"
    trigger_type: str = "LAST_PRICE"
    emulation_trigger: str = "NO_TRIGGER"
    ama_alpha_fast: PositiveInt = 2
    ama_alpha_slow: PositiveInt = 30
    close_positions_on_stop: bool = True


class MACrossTrailingStop(Strategy):
    """MA crossover strategy with ATR trailing stop exit.

    Enters long when flat and fast MA >= slow MA.
    Enters short when flat and fast MA < slow MA.
    Exits via trailing stop market order submitted on position open.

    After a trailing stop exit, re-enters on the next bar if the MA
    regime still holds (regime-based, not crossover-based).

    Parameters
    ----------
    config : MACrossTrailingStopConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.fast_period` is not less than `config.slow_period`.
        If `config.ma_type` is not a recognised type.

    """

    def __init__(self, config: MACrossTrailingStopConfig) -> None:
        PyCondition.is_true(
            config.fast_period < config.slow_period,
            f"{config.fast_period=} must be less than {config.slow_period=}",
        )
        PyCondition.is_in(config.ma_type, _MA_TYPE_LOOKUP, "config.ma_type", "_MA_TYPE_LOOKUP")
        super().__init__(config)

        self.instrument: Instrument | None = None

        ma_enum = _MA_TYPE_LOOKUP[config.ma_type]
        if config.ma_type == "AMA":
            self.fast_ma = AdaptiveMovingAverage(
                config.fast_period, config.ama_alpha_fast, config.ama_alpha_slow,
            )
            self.slow_ma = AdaptiveMovingAverage(
                config.slow_period, config.ama_alpha_fast, config.ama_alpha_slow,
            )
        else:
            self.fast_ma = MovingAverageFactory.create(config.fast_period, ma_enum)
            self.slow_ma = MovingAverageFactory.create(config.slow_period, ma_enum)

        self.atr = AverageTrueRange(config.atr_period)

        # Order/position state for trailing stop management
        self.entry: MarketOrder | None = None
        self.trailing_stop: TrailingStopMarketOrder | None = None
        self.position_id: PositionId | None = None

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ma)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ma)
        self.register_indicator_for_bars(self.config.bar_type, self.atr)

        lookback_bars = max(self.config.slow_period, self.config.atr_period) + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks for order fills.
        # Also needed by trailing stop for quote-based trigger types.
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Enter when flat and MA regime aligned. Exits are via trailing stop."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        # Only enter when flat — trailing stop handles all exits
        if not self.portfolio.is_flat(self.config.instrument_id):
            return

        price = Decimal(str(bar.close))

        if self.fast_ma.value >= self.slow_ma.value:
            self._enter(OrderSide.BUY, price)
        else:
            self._enter(OrderSide.SELL, price)

    def _enter(self, side: OrderSide, price: Decimal) -> None:
        """Submit a market entry order sized by notional USD amount."""
        if self.instrument is None:
            self.log.error("Instrument not loaded — cannot enter position")
            return
        if price <= 0:
            self.log.warning("Invalid price — cannot compute quantity")
            return

        qty = self.instrument.make_qty(self.config.trade_notional / price)
        if qty <= 0:
            self.log.warning(
                f"Computed qty=0 for notional={self.config.trade_notional} "
                f"at price={price}"
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
        self.fast_ma.reset()
        self.slow_ma.reset()
        self.atr.reset()
        self.entry = None
        self.trailing_stop = None
        self.position_id = None
