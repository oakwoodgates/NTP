"""EMA crossover strategy with ATR-based take-profit and stop-loss.

Uses EMA crossover for entries with ATR-sized bracket orders for exits.
Bracket orders (market entry + limit TP + stop-market SL) are submitted
as a linked OrderList — NT cancels the remaining leg automatically when
either TP or SL fills.

Behavioral differences from EMACross:
- Entry fires only on the crossover bar, not on every bar in the regime.
  After a TP or SL fill the strategy is flat and waits for the next cross.
- No immediate reversal: EMA flip while in position cancels the bracket
  and closes the position. Re-entry requires the next fresh crossover.
- Long AND short for perpetual futures (MARGIN account).

VERIFIED — bracket order API (NT 1.223.0):
  order_factory.bracket() params confirmed: instrument_id, order_side,
  quantity, sl_trigger_price, tp_price, entry_order_type. Returns OrderList.
  submit_order_list() is the correct submission method.
  NT's matching engine cancels the remaining leg when either fills.

NOTE — Hyperliquid live trading:
  Verify the HL adapter supports contingent bracket orders before Phase 3.
  If not, swap _enter_bracket() to manual order submission tracked via
  on_order_filled() + cancel_order() on the opposing leg.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class EMACrossATRConfig(StrategyConfig, frozen=True):
    """Configuration for EMACrossATR strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_size : Decimal
        The position size per trade.
    fast_ema_period : int, default 20
        The fast EMA period.
    slow_ema_period : int, default 50
        The slow EMA period.
    atr_period : int, default 14
        The ATR period for TP/SL sizing.
    atr_sl_multiplier : float, default 1.5
        Stop-loss distance = ATR × this multiplier.
    atr_tp_multiplier : float, default 3.0
        Take-profit distance = ATR × this multiplier.
        Default ratio is 2:1 reward-to-risk (3.0 / 1.5).
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_ema_period: PositiveInt = 20
    slow_ema_period: PositiveInt = 50
    atr_period: PositiveInt = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0
    close_positions_on_stop: bool = True


class EMACrossATR(Strategy):
    """EMA crossover strategy with ATR-based bracket TP/SL.

    Enters on EMA crossover bars only. After a TP or SL fill the strategy
    goes flat and waits for the next fresh crossover before re-entering.

    EMA reversal while in position cancels the bracket (both legs) and
    closes the position via market order. No immediate reversal entry —
    the next crossover is required.

    Parameters
    ----------
    config : EMACrossATRConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If fast_ema_period >= slow_ema_period.
    ValueError
        If atr_sl_multiplier <= 0 or atr_tp_multiplier <= atr_sl_multiplier.

    """

    def __init__(self, config: EMACrossATRConfig) -> None:
        PyCondition.is_true(
            config.fast_ema_period < config.slow_ema_period,
            f"{config.fast_ema_period=} must be less than {config.slow_ema_period=}",
        )
        PyCondition.is_true(
            config.atr_sl_multiplier > 0,
            f"{config.atr_sl_multiplier=} must be positive",
        )
        # Enforces reward > risk. Change to >= if 1:1 R:R configs are needed.
        PyCondition.is_true(
            config.atr_tp_multiplier > config.atr_sl_multiplier,
            f"{config.atr_tp_multiplier=} must exceed {config.atr_sl_multiplier=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None

        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        # ATR sizes the bracket legs dynamically per volatility regime.
        # Registered for bars — NT updates it automatically.
        self.atr = AverageTrueRange(config.atr_period)

        # Previous bar values for crossover detection.
        # True crossover (not regime state) prevents re-entry after TP/SL fill.
        self._prev_fast: float = 0.0
        self._prev_slow: float = 0.0

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.atr)

        # Warm up all three indicators. Slow EMA is the binding constraint
        # unless ATR period exceeds it.
        lookback_bars = max(self.config.slow_ema_period, self.config.atr_period) + 10
        self.request_bars(
            self.config.bar_type,
            # Assumes 1h bars. For other timeframes, scale timedelta accordingly.
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate EMA crossover on each bar and manage bracket exits."""
        # Always update prev values during warm-up so the first post-warmup
        # bar has valid crossover detection state.
        if not self.indicators_initialized():
            self._prev_fast = self.fast_ema.value
            self._prev_slow = self.slow_ema.value
            return

        if bar.is_single_price():
            return

        fast = self.fast_ema.value
        slow = self.slow_ema.value
        atr = self.atr.value

        # True crossover detection — fires once per cross, not every bar in regime.
        crossed_above = self._prev_fast <= self._prev_slow and fast > slow
        crossed_below = self._prev_fast >= self._prev_slow and fast < slow

        # Advance state before any early returns.
        self._prev_fast = fast
        self._prev_slow = slow

        # Exit: EMA reversal while in position.
        # Cancel bracket legs first, then close. Going flat here — re-entry
        # requires the next fresh crossover, not an immediate reversal.
        if self.portfolio.is_net_long(self.config.instrument_id) and crossed_below:
            self.cancel_all_orders(self.config.instrument_id)
            self.close_all_positions(self.config.instrument_id)
            return

        if self.portfolio.is_net_short(self.config.instrument_id) and crossed_above:
            self.cancel_all_orders(self.config.instrument_id)
            self.close_all_positions(self.config.instrument_id)
            return

        # Entry: flat + crossover + valid ATR value.
        if not self.portfolio.is_flat(self.config.instrument_id):
            return

        if crossed_above:
            self._enter_bracket(OrderSide.BUY, bar, atr)
        elif crossed_below:
            self._enter_bracket(OrderSide.SELL, bar, atr)

    def _enter_bracket(self, side: OrderSide, bar: Bar, atr: float) -> None:
        """Submit a bracket order: market entry + ATR-sized TP limit + SL stop-market.

        TP and SL distances are calculated from bar.close as a proxy for
        expected fill price. In backtesting, market orders fill at the next
        bar's open (NT default), so actual fill will differ slightly from
        bar.close. This is an inherent approximation when using pre-calculated
        bracket levels with market entries — accept the small discrepancy
        rather than adding complexity.

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
            self.log.error("Instrument not loaded — cannot enter position")
            return

        entry_price = float(bar.close)
        sl_distance = atr * self.config.atr_sl_multiplier
        tp_distance = atr * self.config.atr_tp_multiplier

        if side == OrderSide.BUY:
            sl_price = self.instrument.make_price(entry_price - sl_distance)
            tp_price = self.instrument.make_price(entry_price + tp_distance)
        else:
            sl_price = self.instrument.make_price(entry_price + sl_distance)
            tp_price = self.instrument.make_price(entry_price - tp_distance)

        qty = self.instrument.make_qty(self.config.trade_size)

        # Bracket: market entry + contingent TP limit + contingent SL stop-market.
        # NT cancels the remaining leg automatically when either fills.
        # Signature verified against NT 1.223.0 (factories.pyx line 1193).
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
            f"entry≈{entry_price:.2f} | "
            f"SL={sl_price} | TP={tp_price} | ATR={atr:.2f}"
        )

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
        self._prev_fast = 0.0
        self._prev_slow = 0.0
