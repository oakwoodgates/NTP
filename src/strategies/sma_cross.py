"""SMA crossover strategy for pipeline validation."""

from datetime import timedelta
from decimal import Decimal

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import SimpleMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class SMACrossConfig(StrategyConfig, frozen=True):
    """Configuration for SMACross strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    trade_size : Decimal
        The position size per trade.
    fast_sma_period : int, default 10
        The fast SMA period.
    slow_sma_period : int, default 20
        The slow SMA period.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.
        Set to False to stop the strategy without liquidating (e.g., during
        a code deploy in live trading).

    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_sma_period: PositiveInt = 10
    slow_sma_period: PositiveInt = 20
    close_positions_on_stop: bool = True


class SMACross(Strategy):
    """Simple SMA crossover strategy for pipeline validation.

    Goes long when fast SMA crosses above slow SMA.
    Goes short when fast SMA crosses below slow SMA.
    Designed for perpetual futures (supports both directions).

    THIS IS A PIPELINE VALIDATION STRATEGY WITH NO ALPHA ADVANTAGE.

    Parameters
    ----------
    config : SMACrossConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.fast_sma_period` is not less than `config.slow_sma_period`.

    """

    def __init__(self, config: SMACrossConfig) -> None:
        PyCondition.is_true(
            config.fast_sma_period < config.slow_sma_period,
            f"{config.fast_sma_period=} must be less than {config.slow_sma_period=}",
        )
        super().__init__(config)

        self.instrument: Instrument | None = None
        self.fast_sma = SimpleMovingAverage(config.fast_sma_period)
        self.slow_sma = SimpleMovingAverage(config.slow_sma_period)

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_sma)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_sma)

        # Request enough historical bars to fully hydrate the slowest indicator.
        # In backtesting this is redundant (bars feed sequentially), but in live
        # trading this determines how many bars NT fetches from the exchange on
        # startup. Under-requesting means indicators stay uninitialized for the
        # first N bars of a live session.
        lookback_bars = self.config.slow_sma_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate SMA crossover on each bar."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        # BUY signal: fast SMA >= slow SMA
        if self.fast_sma.value >= self.slow_sma.value:
            if self.portfolio.is_flat(self.config.instrument_id):
                self._enter(OrderSide.BUY)
            elif self.portfolio.is_net_short(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.BUY)

        # SELL signal: fast SMA < slow SMA
        elif self.fast_sma.value < self.slow_sma.value:
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

    def on_order_filled(self, event) -> None:  # noqa: ANN001
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
        self.fast_sma.reset()
        self.slow_sma.reset()
