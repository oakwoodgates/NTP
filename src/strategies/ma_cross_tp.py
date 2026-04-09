"""MA crossover strategy with percentage-based take-profit exit (EMA / SMA / HMA / DEMA / AMA / VIDYA).

Uses MA crossover for entries (fires once per cross, not every bar in
regime).  Exits via bar-close take-profit check or MA cross reversal —
whichever comes first.

After a TP exit the strategy goes flat and waits for the next fresh
crossover before re-entering.  MA reversal while in position closes
the current position and immediately enters the opposite direction.

Take-profit is evaluated on bar close: for longs, ``bar.close >= entry
× (1 + tp_pct / 100)``; for shorts, ``bar.close <= entry × (1 - tp_pct
/ 100)``.  Entry price is the actual fill price captured from the
``OrderFilled`` event.
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
    MovingAverageFactory,
    MovingAverageType,
)
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import MarketOrder
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


class MACrossTPConfig(StrategyConfig, frozen=True):
    """Configuration for MACrossTP strategy.

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
    tp_pct : float, default 5.0
        Take-profit percentage.  A value of 5.0 means the position is
        closed when bar close reaches 5% profit from the entry price.
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
    tp_pct: PositiveFloat = 5.0
    ama_alpha_fast: PositiveInt = 2
    ama_alpha_slow: PositiveInt = 30
    close_positions_on_stop: bool = True


class MACrossTP(Strategy):
    """MA crossover strategy with percentage take-profit exit.

    Enters long on bullish MA crossover, enters short on bearish
    crossover.  Exits when bar close reaches the take-profit target or
    when the MA crosses in the opposite direction.

    After a TP exit the strategy goes flat and waits for the next fresh
    crossover.  On MA reversal while in position, the strategy closes
    and immediately enters the opposite direction.

    Parameters
    ----------
    config : MACrossTPConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If ``config.fast_period`` is not less than ``config.slow_period``.
        If ``config.ma_type`` is not a recognised type.

    """

    def __init__(self, config: MACrossTPConfig) -> None:
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

        # Previous bar values for crossover detection.
        self._prev_fast: float = 0.0
        self._prev_slow: float = 0.0

        # Entry tracking for take-profit calculation.
        self._entry_order: MarketOrder | None = None
        self._entry_price: float = 0.0

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ma)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ma)

        lookback_bars = self.config.slow_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks for order fills.
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate MA crossover and take-profit on each bar."""
        # Update prev values during warmup so the first post-warmup bar
        # has valid crossover detection state.
        if not self.indicators_initialized():
            self._prev_fast = self.fast_ma.value
            self._prev_slow = self.slow_ma.value
            return

        if bar.is_single_price():
            return

        fast = self.fast_ma.value
        slow = self.slow_ma.value

        # True crossover detection — fires once per cross.
        crossed_above = self._prev_fast <= self._prev_slow and fast > slow
        crossed_below = self._prev_fast >= self._prev_slow and fast < slow

        # Advance state before any early returns.
        self._prev_fast = fast
        self._prev_slow = slow

        price = Decimal(str(bar.close))
        close = float(bar.close)
        is_flat = self.portfolio.is_flat(self.config.instrument_id)
        is_net_long = self.portfolio.is_net_long(self.config.instrument_id)
        is_net_short = self.portfolio.is_net_short(self.config.instrument_id)

        # ── Position management: TP check, then reversal check ───────
        if is_net_long:
            if self._entry_price <= 0.0:
                self.log.warning("Long position open but entry price unknown — skipping TP check")
            else:
                tp_target = self._entry_price * (1.0 + self.config.tp_pct / 100.0)
                if close >= tp_target:
                    self.log.info(
                        f"TP hit LONG: close={close:.2f} >= target={tp_target:.2f} "
                        f"(entry={self._entry_price:.2f} + {self.config.tp_pct}%)"
                    )
                    self.close_all_positions(self.config.instrument_id)
                    return  # Go flat, wait for next crossover
            if crossed_below:
                self.log.info(
                    f"MA reversal while LONG: closing + entering SHORT at {price}"
                )
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.SELL, price)
                return

        elif is_net_short:
            if self._entry_price <= 0.0:
                self.log.warning("Short position open but entry price unknown — skipping TP check")
            else:
                tp_target = self._entry_price * (1.0 - self.config.tp_pct / 100.0)
                if close <= tp_target:
                    self.log.info(
                        f"TP hit SHORT: close={close:.2f} <= target={tp_target:.2f} "
                        f"(entry={self._entry_price:.2f} - {self.config.tp_pct}%)"
                    )
                    self.close_all_positions(self.config.instrument_id)
                    return  # Go flat, wait for next crossover
            if crossed_above:
                self.log.info(
                    f"MA reversal while SHORT: closing + entering BUY at {price}"
                )
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.BUY, price)
                return

        # ── Entry when flat + crossover ──────────────────────────────
        if is_flat:
            if crossed_above:
                self._enter(OrderSide.BUY, price)
            elif crossed_below:
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
        self._entry_order = order
        self.submit_order(order)
        self.log.info(f"Entry submitted: {side.name} {qty} at ~{price}")

    def on_event(self, event: Event) -> None:
        """Capture entry fill price for take-profit calculation."""
        if (
            isinstance(event, OrderFilled)
            and self._entry_order is not None
            and event.client_order_id == self._entry_order.client_order_id
        ):
            self._entry_price = float(event.last_px)
            self.log.info(
                f"Entry filled: {event.order_side.name} at {event.last_px} "
                f"(TP target: {self._entry_price * (1.0 + self.config.tp_pct / 100.0):.2f} "
                f"for long, {self._entry_price * (1.0 - self.config.tp_pct / 100.0):.2f} for short)"
            )

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
        self._prev_fast = 0.0
        self._prev_slow = 0.0
        self._entry_order = None
        self._entry_price = 0.0
