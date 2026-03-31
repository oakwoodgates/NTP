"""Bollinger Band mean reversion strategy with RSI filter.

Enters long when price touches the lower BB band with RSI oversold
confirmation.  Enters short when price touches the upper BB band with RSI
overbought confirmation.  Exits when price reverts to the middle band (SMA).

NT's BollingerBands indicator uses typical price (high+low+close)/3
internally for band calculation.  NT's RSI uses a 0.0-1.0 scale (not 0-100).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveFloat, PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import BollingerBands, RelativeStrengthIndex
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class BBMeanRevConfig(StrategyConfig, frozen=True):
    """Configuration for BBMeanRev strategy.

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
    bb_period : int, default 20
        The Bollinger Bands rolling window period.
    bb_std : float, default 2.0
        The Bollinger Bands standard deviation multiplier.
    rsi_period : int, default 14
        The RSI rolling window period.
    rsi_buy_threshold : float, default 0.30
        RSI must be below this for a long entry (0.0-1.0 scale).
    rsi_sell_threshold : float, default 0.70
        RSI must be above this for a short entry (0.0-1.0 scale).
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.
        Set to False to stop the strategy without liquidating (e.g., during
        a code deploy in live trading).

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_notional: Decimal
    bb_period: PositiveInt = 20
    bb_std: PositiveFloat = 2.0
    rsi_period: PositiveInt = 14
    rsi_buy_threshold: float = 0.30
    rsi_sell_threshold: float = 0.70
    close_positions_on_stop: bool = True


class BBMeanRev(Strategy):
    """Bollinger Band mean reversion strategy with RSI filter.

    When price touches the lower band with RSI confirmation, enter long.
    When price touches the upper band with RSI confirmation, enter short.
    Exit positions when price reverts to the middle band.

    Parameters
    ----------
    config : BBMeanRevConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If ``config.rsi_buy_threshold`` is not less than
        ``config.rsi_sell_threshold``.

    """

    def __init__(self, config: BBMeanRevConfig) -> None:
        PyCondition.is_true(
            config.rsi_buy_threshold < config.rsi_sell_threshold,
            f"{config.rsi_buy_threshold=} must be less than {config.rsi_sell_threshold=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None
        self.bb = BollingerBands(config.bb_period, config.bb_std)
        self.rsi = RelativeStrengthIndex(config.rsi_period)

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.bb)
        self.register_indicator_for_bars(self.config.bar_type, self.rsi)

        lookback_bars = max(self.config.bb_period, self.config.rsi_period) + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks to maintain a market
        # for order fills. Without this, orders are rejected with "no market".
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate BB mean reversion signals on each bar."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        close = bar.close.as_double()
        price = Decimal(str(bar.close))

        # Check exit first; if we exited, skip entry this bar
        if not self._check_exit(close):
            self._check_entry(close, price)

    def _check_exit(self, close: float) -> bool:
        """Close position if price has reverted to the middle band.

        Returns True if a position was closed (caller should skip entry).
        """
        iid = self.config.instrument_id
        if self.portfolio.is_net_long(iid) and close >= self.bb.middle:
            self.close_all_positions(iid)
            return True
        if self.portfolio.is_net_short(iid) and close <= self.bb.middle:
            self.close_all_positions(iid)
            return True
        return False

    def _check_entry(self, close: float, price: Decimal) -> None:
        """Enter long at lower band or short at upper band with RSI confirmation."""
        iid = self.config.instrument_id

        if close <= self.bb.lower and self.rsi.value < self.config.rsi_buy_threshold:
            if self.portfolio.is_net_short(iid):
                self.close_all_positions(iid)
            if not self.portfolio.is_net_long(iid):
                self._enter(OrderSide.BUY, price)

        elif close >= self.bb.upper and self.rsi.value > self.config.rsi_sell_threshold:
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
        """Reset indicators for engine reuse (parameter sweeps)."""
        self.bb.reset()
        self.rsi.reset()
