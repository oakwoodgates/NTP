"""MACD + RSI confluence strategy.

Uses MACD crossover signals for entries with RSI as a momentum filter
to avoid false signals in choppy markets. RSI overbought/oversold levels
provide take-profit exits.

NT's MACD only computes fast_MA - slow_MA (no signal line), so the
signal line is computed manually via a separate EMA fed with MACD values.
NT's RSI uses a 0.0-1.0 scale (not 0-100).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import (
    ExponentialMovingAverage,
    MovingAverageConvergenceDivergence,
    RelativeStrengthIndex,
)
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class MACDRSIConfig(StrategyConfig, frozen=True):
    """Configuration for MACDRSI strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_size : Decimal
        The position size per trade.
    macd_fast_period : int, default 12
        The fast EMA period for the MACD calculation.
    macd_slow_period : int, default 26
        The slow EMA period for the MACD calculation.
    macd_signal_period : int, default 9
        The EMA period for the MACD signal line.
    rsi_period : int, default 14
        The RSI period.
    rsi_overbought : float, default 0.70
        RSI overbought threshold (NT RSI uses 0.0-1.0 scale).
    rsi_oversold : float, default 0.30
        RSI oversold threshold (NT RSI uses 0.0-1.0 scale).
    rsi_entry_threshold : float, default 0.50
        RSI must be above this for longs, below (1 - this) for shorts.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    macd_fast_period: PositiveInt = 12
    macd_slow_period: PositiveInt = 26
    macd_signal_period: PositiveInt = 9
    rsi_period: PositiveInt = 14
    rsi_overbought: float = 0.70
    rsi_oversold: float = 0.30
    rsi_entry_threshold: float = 0.50
    close_positions_on_stop: bool = True


class MACDRSI(Strategy):
    """MACD + RSI confluence strategy.

    Entry signals require both MACD crossover AND RSI momentum confirmation.
    Exits on MACD reversal crossover or RSI extreme (take-profit).

    On a MACD reversal exit, the strategy can immediately enter the opposite
    direction if RSI confirms. On an RSI take-profit exit, the strategy waits
    for the next MACD crossover before re-entering.

    Parameters
    ----------
    config : MACDRSIConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.macd_fast_period` is not less than `config.macd_slow_period`.
    ValueError
        If RSI thresholds are not ordered: oversold < entry_threshold < overbought.

    """

    def __init__(self, config: MACDRSIConfig) -> None:
        PyCondition.is_true(
            config.macd_fast_period < config.macd_slow_period,
            f"{config.macd_fast_period=} must be less than {config.macd_slow_period=}",
        )
        PyCondition.is_true(
            config.rsi_oversold < config.rsi_entry_threshold < config.rsi_overbought,
            f"RSI thresholds must be ordered: {config.rsi_oversold=} < "
            f"{config.rsi_entry_threshold=} < {config.rsi_overbought=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None

        # MACD line (fast_MA - slow_MA) — registered for bars
        self.macd = MovingAverageConvergenceDivergence(
            config.macd_fast_period,
            config.macd_slow_period,
        )
        # Signal line (EMA of MACD values) — manually updated
        self.signal_ema = ExponentialMovingAverage(config.macd_signal_period)
        # RSI — registered for bars
        self.rsi = RelativeStrengthIndex(config.rsi_period)

        # Previous bar's values for crossover detection
        self._prev_macd: float = 0.0
        self._prev_signal: float = 0.0

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        # Register MACD and RSI for automatic bar updates.
        # Signal EMA is NOT registered — it receives MACD output, not bar closes.
        self.register_indicator_for_bars(self.config.bar_type, self.macd)
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)

        lookback_bars = self.config.macd_slow_period + self.config.macd_signal_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate MACD/RSI signals on each bar."""
        # Step 1: Feed MACD value to signal EMA (must happen every bar)
        if self.macd.initialized:
            self.signal_ema.update_raw(self.macd.value)

        # Step 2: Wait for registered indicators (MACD + RSI)
        if not self.indicators_initialized():
            return

        # Step 3: Wait for signal EMA to warm up
        if not self.signal_ema.initialized:
            self._prev_macd = self.macd.value
            self._prev_signal = self.signal_ema.value if self.signal_ema.has_inputs else 0.0
            return

        if bar.is_single_price():
            return

        # Step 4: Current values
        macd_val = self.macd.value
        signal_val = self.signal_ema.value
        rsi_val = self.rsi.value

        # Step 5: Crossover detection
        macd_crossed_above = self._prev_macd <= self._prev_signal and macd_val > signal_val
        macd_crossed_below = self._prev_macd >= self._prev_signal and macd_val < signal_val

        # Step 6: Update prev for next bar
        self._prev_macd = macd_val
        self._prev_signal = signal_val

        # Step 7: Exit logic (check before entries)
        if self.portfolio.is_net_long(self.config.instrument_id):
            if macd_crossed_below or rsi_val > self.config.rsi_overbought:
                self.close_all_positions(self.config.instrument_id)
                # On MACD reversal, enter short immediately if RSI allows
                if (
                    macd_crossed_below
                    and rsi_val < (1.0 - self.config.rsi_entry_threshold)
                    and rsi_val > self.config.rsi_oversold
                ):
                    self._enter(OrderSide.SELL)
                return

        elif self.portfolio.is_net_short(self.config.instrument_id) and (
            macd_crossed_above or rsi_val < self.config.rsi_oversold
        ):
            self.close_all_positions(self.config.instrument_id)
            # On MACD reversal, enter long immediately if RSI allows
            if (
                macd_crossed_above
                and rsi_val > self.config.rsi_entry_threshold
                and rsi_val < self.config.rsi_overbought
            ):
                self._enter(OrderSide.BUY)
            return

        # Step 8: Entry logic (only when flat)
        if self.portfolio.is_flat(self.config.instrument_id):
            if (
                macd_crossed_above
                and rsi_val > self.config.rsi_entry_threshold
                and rsi_val < self.config.rsi_overbought
            ):
                self._enter(OrderSide.BUY)
            elif (
                macd_crossed_below
                and rsi_val < (1.0 - self.config.rsi_entry_threshold)
                and rsi_val > self.config.rsi_oversold
            ):
                self._enter(OrderSide.SELL)

    def _enter(self, side: OrderSide) -> None:
        """Submit a market order for the given side."""
        if self.instrument is None:
            self.log.error("Instrument not loaded — cannot enter position")
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        """Cancel all orders, optionally close positions, unsubscribe."""
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """Reset indicators for engine reuse (parameter sweeps)."""
        self.macd.reset()
        self.signal_ema.reset()
        self.rsi.reset()
        self._prev_macd = 0.0
        self._prev_signal = 0.0
