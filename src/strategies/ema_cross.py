"""EMA crossover strategy for pipeline validation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class EMACrossConfig(StrategyConfig, frozen=True):
    """Configuration for EMACross strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_size : Decimal
        The position size per trade.
    fast_ema_period : int, default 10
        The fast EMA period.
    slow_ema_period : int, default 20
        The slow EMA period.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.
        Set to False to stop the strategy without liquidating (e.g., during
        a code deploy in live trading).

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_ema_period: PositiveInt = 10
    slow_ema_period: PositiveInt = 20
    close_positions_on_stop: bool = True


class EMACross(Strategy):
    """Simple EMA crossover strategy for pipeline validation.

    Goes long when fast EMA crosses above slow EMA.
    Goes short when fast EMA crosses below slow EMA.
    Designed for perpetual futures (supports both directions).

    THIS IS A PIPELINE VALIDATION STRATEGY WITH NO ALPHA ADVANTAGE.

    Parameters
    ----------
    config : EMACrossConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.fast_ema_period` is not less than `config.slow_ema_period`.

    """

    def __init__(self, config: EMACrossConfig) -> None:
        PyCondition.is_true(
            config.fast_ema_period < config.slow_ema_period,
            f"{config.fast_ema_period=} must be less than {config.slow_ema_period=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)

        # Request enough historical bars to fully hydrate the slowest indicator.
        # In backtesting this is redundant (bars feed sequentially), but in live
        # trading this determines how many bars NT fetches from the exchange on
        # startup. Under-requesting means indicators stay uninitialized for the
        # first N bars of a live session.
        lookback_bars = self.config.slow_ema_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks to maintain a market
        # for order fills. Without this, orders are rejected with "no market".
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate EMA crossover on each bar."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        # BUY signal: fast EMA >= slow EMA
        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_flat(self.config.instrument_id):
                self._enter(OrderSide.BUY)
            elif self.portfolio.is_net_short(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.BUY)

        # SELL signal: fast EMA < slow EMA
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_flat(self.config.instrument_id):
                self._enter(OrderSide.SELL)
            elif self.portfolio.is_net_long(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
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

    def on_order_filled(self, event: Any) -> None:
        """Log fills for debugging."""
        self.log.info(f"Filled: {event}")

    def on_stop(self) -> None:
        """Cancel all orders, optionally close positions, unsubscribe."""
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """Reset indicators for engine reuse (parameter sweeps)."""
        self.fast_ema.reset()
        self.slow_ema.reset()
