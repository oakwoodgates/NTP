"""MA regime-based strategy with symmetric ATR bracket exits (EMA / SMA / HMA / DEMA / AMA / VIDYA).

Adapted from NautilusTrader's example ``ema_cross_bracket.py`` (NT repo).  Uses MA
regime alignment for entries (enters when flat and fast MA >= slow for longs,
fast < slow for shorts).  Exits via symmetric ATR-sized bracket orders --
market entry + limit TP + stop-market SL at the same distance from entry.

After a TP or SL fill the strategy re-enters on the next bar if the MA
regime still holds.  This is **regime-based** entry: multiple trades per
MA regime, more aggressive trend participation.

Behavioral differences from MACrossATR:
- Entry fires every bar while flat + in regime, not just on the crossover bar.
  After a TP or SL fill the strategy is flat and re-enters immediately if the
  regime holds -- MACrossATR waits for the next fresh crossover.
- Symmetric bracket (same ATR distance for both SL and TP -> 1:1 R:R).
  MACrossATR has separate SL/TP multipliers for asymmetric R:R.
- On MA reversal while in position: cancel bracket, close position, and
  enter the opposite direction immediately on the same bar.
- Long AND short for perpetual futures (MARGIN account).

NOTE -- Hyperliquid live trading:
  Verify the HL adapter supports contingent bracket orders before Phase 3.
  If not, swap _enter_bracket() to manual order submission tracked via
  on_order_filled() + cancel_order() on the opposing leg.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import (
    AdaptiveMovingAverage,
    AverageTrueRange,
    MovingAverageFactory,
    MovingAverageType,
)
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import InstrumentId
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


class MACrossBracketConfig(StrategyConfig, frozen=True):
    """Configuration for MACrossBracket strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_notional : Decimal
        The USD notional amount per trade. Quantity is computed dynamically
        from trade_notional / entry_price at each entry.
    ma_type : str, default "EMA"
        Moving average type: ``"EMA"`` | ``"SMA"`` | ``"HMA"`` |
        ``"DEMA"`` | ``"AMA"`` | ``"VIDYA"``.
    fast_period : int, default 10
        The fast MA period.
    slow_period : int, default 20
        The slow MA period.
    atr_period : int, default 20
        The ATR period for bracket distance sizing.
    bracket_distance_atr : float, default 3.0
        SL and TP distance from entry = ATR x this multiplier.
        Symmetric: same distance for both stop-loss and take-profit (1:1 R:R).
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
    bracket_distance_atr: PositiveFloat = 3.0
    ama_alpha_fast: PositiveInt = 2
    ama_alpha_slow: PositiveInt = 30
    close_positions_on_stop: bool = True


class MACrossBracket(Strategy):
    """MA regime-based strategy with symmetric ATR bracket exits.

    Enters long when flat and fast MA >= slow MA.
    Enters short when flat and fast MA < slow MA.
    Exits via symmetric bracket orders (SL and TP at the same ATR distance).

    After a TP or SL fill, re-enters on the next bar if the MA regime still
    holds (regime-based, not crossover-based).

    Parameters
    ----------
    config : MACrossBracketConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If ``config.fast_period`` is not less than ``config.slow_period``.
        If ``config.ma_type`` is not a recognised type.

    """

    def __init__(self, config: MACrossBracketConfig) -> None:
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

    def on_bar(self, bar: Bar) -> None:
        """Evaluate MA regime on each bar and manage bracket entries."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        fast = self.fast_ma.value
        slow = self.slow_ma.value
        atr = self.atr.value

        # BUY regime: fast MA >= slow MA
        if fast >= slow:
            if self.portfolio.is_flat(self.config.instrument_id):
                self.cancel_all_orders(self.config.instrument_id)
                self._enter_bracket(OrderSide.BUY, bar, atr)
            elif self.portfolio.is_net_short(self.config.instrument_id):
                self.cancel_all_orders(self.config.instrument_id)
                self.close_all_positions(self.config.instrument_id)
                self._enter_bracket(OrderSide.BUY, bar, atr)

        # SELL regime: fast MA < slow MA
        elif fast < slow:
            if self.portfolio.is_flat(self.config.instrument_id):
                self.cancel_all_orders(self.config.instrument_id)
                self._enter_bracket(OrderSide.SELL, bar, atr)
            elif self.portfolio.is_net_long(self.config.instrument_id):
                self.cancel_all_orders(self.config.instrument_id)
                self.close_all_positions(self.config.instrument_id)
                self._enter_bracket(OrderSide.SELL, bar, atr)

    def _enter_bracket(self, side: OrderSide, bar: Bar, atr: float) -> None:
        """Submit a bracket order: market entry + symmetric ATR-sized TP/SL.

        TP and SL distances are calculated from bar.close as a proxy for
        expected fill price.  In backtesting, market orders fill at the next
        bar's open (NT default), so actual fill will differ slightly from
        bar.close.  This is an inherent approximation when using pre-calculated
        bracket levels with market entries.

        Parameters
        ----------
        side : OrderSide
            BUY or SELL.
        bar : Bar
            Current bar (close price used as entry price proxy).
        atr : float
            Current ATR value for distance sizing.

        """
        if self.instrument is None:
            self.log.error("Instrument not loaded -- cannot enter position")
            return

        entry_price = float(bar.close)
        bracket_distance = atr * self.config.bracket_distance_atr

        if side == OrderSide.BUY:
            sl_price = self.instrument.make_price(entry_price - bracket_distance)
            tp_price = self.instrument.make_price(entry_price + bracket_distance)
        else:
            sl_price = self.instrument.make_price(entry_price + bracket_distance)
            tp_price = self.instrument.make_price(entry_price - bracket_distance)

        price_dec = Decimal(str(entry_price))
        if price_dec <= 0:
            self.log.warning("Invalid price -- cannot compute quantity")
            return

        qty = self.instrument.make_qty(self.config.trade_notional / price_dec)
        if qty <= 0:
            self.log.warning(
                f"Computed qty=0 for notional={self.config.trade_notional} "
                f"at price={price_dec}"
            )
            return

        bracket = self.order_factory.bracket(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=qty,
            sl_trigger_price=sl_price,
            tp_price=tp_price,
            entry_order_type=OrderType.MARKET,
        )
        self.submit_order_list(bracket)

        self.log.info(
            f"Bracket submitted: {side.name} {qty} | "
            f"entry~{entry_price:.2f} | "
            f"SL={sl_price} | TP={tp_price} | ATR={atr:.2f} | "
            f"dist={bracket_distance:.2f}"
        )

    def on_stop(self) -> None:
        """Cancel all orders, optionally close positions, unsubscribe."""
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """Reset indicators for engine reuse (parameter sweeps)."""
        self.fast_ma.reset()
        self.slow_ma.reset()
        self.atr.reset()
