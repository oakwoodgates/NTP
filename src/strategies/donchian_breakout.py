"""Donchian Channel breakout strategy with dual-period channels.

Classic Turtle-style breakout: enter when price breaks above (long) or below
(short) the entry channel.  Exit long when price drops below the exit channel
lower; exit short when price rises above the exit channel upper.

The entry channel (longer period) captures major breakouts.  The exit channel
(shorter period) acts as a tighter trailing exit, locking in gains on pullbacks
without requiring the full entry-channel reversal.

Uses crossover detection — only enters on the breakout bar itself, not on every
bar while price remains beyond the channel.  Market orders only (MIT/LIT orders
never trigger in bar-only backtests — see BAR_BACKTESTING_GOTCHAS.md).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import DonchianChannel
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class DonchianBreakoutConfig(StrategyConfig, frozen=True):
    """Configuration for DonchianBreakout strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_notional : Decimal
        The USD notional size per trade.  Quantity is computed at entry
        time as ``trade_notional / current_price``, so each trade risks
        approximately the same dollar amount regardless of asset price.
    entry_period : int, default 20
        The Donchian Channel period for entry signals (longer channel).
    exit_period : int, default 10
        The Donchian Channel period for exit signals (shorter channel).
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.
        Set to False to stop the strategy without liquidating (e.g., during
        a code deploy in live trading).

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_notional: Decimal
    entry_period: PositiveInt = 20
    exit_period: PositiveInt = 10
    close_positions_on_stop: bool = True


class DonchianBreakout(Strategy):
    """Donchian Channel breakout strategy with dual-period channels.

    Enters long when price breaks above the entry channel upper band
    (new N-period high).  Enters short when price breaks below the entry
    channel lower band (new N-period low).

    Exits long when price drops below the exit channel lower band.
    Exits short when price rises above the exit channel upper band.

    Uses crossover detection — only enters on the breakout bar, not on
    every bar while price remains beyond the channel.

    Parameters
    ----------
    config : DonchianBreakoutConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If ``config.exit_period`` is not less than ``config.entry_period``.

    """

    def __init__(self, config: DonchianBreakoutConfig) -> None:
        PyCondition.is_true(
            config.exit_period < config.entry_period,
            f"{config.exit_period=} must be less than {config.entry_period=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None
        self.dc_entry = DonchianChannel(config.entry_period)
        self.dc_exit = DonchianChannel(config.exit_period)

        self._prev_close: float = 0.0

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.dc_entry)
        self.register_indicator_for_bars(self.config.bar_type, self.dc_exit)

        lookback_bars = self.config.entry_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks to maintain a market
        # for order fills. Without this, orders are rejected with "no market".
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate Donchian Channel breakout signals on each bar."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        close = bar.close.as_double()
        price = Decimal(str(bar.close))

        # Check exit first; if we exited, skip entry this bar
        if not self._check_exit(close):
            self._check_entry(close, price)

        self._prev_close = close

    def _check_exit(self, close: float) -> bool:
        """Close position if price has breached the exit channel.

        Returns True if a position was closed (caller should skip entry).
        """
        iid = self.config.instrument_id
        if self.portfolio.is_net_long(iid) and close < self.dc_exit.lower:
            self.close_all_positions(iid)
            return True
        if self.portfolio.is_net_short(iid) and close > self.dc_exit.upper:
            self.close_all_positions(iid)
            return True
        return False

    def _check_entry(self, close: float, price: Decimal) -> None:
        """Enter on breakout above entry upper or below entry lower."""
        iid = self.config.instrument_id

        # Long breakout: price crosses above entry channel upper
        if self._prev_close <= self.dc_entry.upper and close > self.dc_entry.upper:
            if self.portfolio.is_net_short(iid):
                self.close_all_positions(iid)
            if not self.portfolio.is_net_long(iid):
                self._enter(OrderSide.BUY, price)

        # Short breakout: price crosses below entry channel lower
        elif self._prev_close >= self.dc_entry.lower and close < self.dc_entry.lower:
            if self.portfolio.is_net_long(iid):
                self.close_all_positions(iid)
            if not self.portfolio.is_net_short(iid):
                self._enter(OrderSide.SELL, price)

    def _enter(self, side: OrderSide, price: Decimal) -> None:
        """Submit a market order sized by notional USD amount."""
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

        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=qty,
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        """Cancel all orders, optionally close positions, unsubscribe."""
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """Reset indicators and state for engine reuse (parameter sweeps)."""
        self.dc_entry.reset()
        self.dc_exit.reset()
        self._prev_close = 0.0
