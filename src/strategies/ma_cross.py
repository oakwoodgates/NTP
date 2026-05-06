"""Moving-average crossover strategy (EMA / SMA / HMA / DEMA / AMA / VIDYA)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.config import PositiveInt
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators import (
    AdaptiveMovingAverage,
    MovingAverageFactory,
    MovingAverageType,
)
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

# LiquidationConfig and SizingConfig are imported at runtime — msgspec
# resolves these as field types on the StrategyConfig at struct-build time.
from src.core.liquidation import LiquidationConfig  # noqa: TC001
from src.core.liquidation_mixin import LiquidationAware
from src.core.sizing import (
    SizingConfig,  # noqa: TC001
    compute_notional,
    resolve_sizing_from_strategy_config,
)

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


class MACrossConfig(StrategyConfig, frozen=True):
    """Configuration for MACross strategy.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    sizing : SizingConfig | None
        Position sizing config.  When ``None``, falls back to
        ``trade_notional`` (back-compat).  When set, overrides
        ``trade_notional``.
    trade_notional : Decimal | None
        Back-compat fixed notional. Equivalent to
        ``SizingConfig(mode="fixed", fixed_notional=trade_notional)``.
        Quantity is computed at entry as ``notional / current_price``.
        Either ``sizing`` or ``trade_notional`` must be set.
    ma_type : str, default "EMA"
        Moving average type: ``"EMA"`` | ``"SMA"`` | ``"HMA"`` |
        ``"DEMA"`` | ``"AMA"`` | ``"VIDYA"``.
    fast_period : int, default 10
        The fast MA period.
    slow_period : int, default 20
        The slow MA period.
    ama_alpha_fast : int, default 2
        Fast smoothing constant period for AMA (Kaufman).
        Only used when ``ma_type="AMA"``.
    ama_alpha_slow : int, default 30
        Slow smoothing constant period for AMA (Kaufman).
        Only used when ``ma_type="AMA"``.
    close_positions_on_stop : bool, default True
        If all open positions should be closed on strategy stop.
        Set to False to stop the strategy without liquidating (e.g., during
        a code deploy in live trading).
    liquidation : LiquidationConfig | None, default None
        Liquidation simulator config. When ``None`` the
        ``LiquidationAware`` mixin is inert. ``mm_rate`` is resolved by
        ``make_engine`` from ``VenueConfig`` before being passed in.

    """

    instrument_id: InstrumentId
    bar_type: BarType
    sizing: SizingConfig | None = None
    trade_notional: Decimal | None = None
    ma_type: str = "EMA"
    fast_period: PositiveInt = 10
    slow_period: PositiveInt = 20
    ama_alpha_fast: PositiveInt = 2
    ama_alpha_slow: PositiveInt = 30
    close_positions_on_stop: bool = True
    liquidation: LiquidationConfig | None = None


class MACross(LiquidationAware, Strategy):
    """Moving-average crossover strategy using MovingAverageFactory.

    Goes long when fast MA crosses above slow MA.
    Goes short when fast MA crosses below slow MA.
    Designed for perpetual futures (supports both directions).

    Inheritance order: ``LiquidationAware`` MUST come first. NT calls the
    typed handlers (``on_position_opened`` etc.) by name; ``Strategy`` defines
    those as concrete no-op stubs, so ``(Strategy, LiquidationAware)`` order
    finds the stubs first via MRO and the mixin silently never runs.

    Parameters
    ----------
    config : MACrossConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.fast_period` is not less than `config.slow_period`.
        If `config.ma_type` is not a recognised type.
        If neither `config.sizing` nor `config.trade_notional` is set.

    """

    def __init__(self, config: MACrossConfig) -> None:
        PyCondition.is_true(
            config.fast_period < config.slow_period,
            f"{config.fast_period=} must be less than {config.slow_period=}",
        )
        PyCondition.is_in(config.ma_type, _MA_TYPE_LOOKUP, "config.ma_type", "_MA_TYPE_LOOKUP")
        super().__init__(config)
        self._init_liquidation(config.liquidation)

        # Resolve sizing: explicit SizingConfig wins; else build a fixed-mode
        # config from trade_notional for back-compat.
        self._sizing = resolve_sizing_from_strategy_config(config)

        self.instrument: Instrument | None = None
        ma_enum = _MA_TYPE_LOOKUP[config.ma_type]
        # AMA requires 3 constructor args; MovingAverageFactory returns None for it.
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

    def on_start(self) -> None:
        """Register indicators, request historical bars, subscribe to bars."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.bar_type, self.fast_ma)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ma)

        # Request enough historical bars to fully hydrate the slowest indicator.
        # In backtesting this is redundant (bars feed sequentially), but in live
        # trading this determines how many bars NT fetches from the exchange on
        # startup. Under-requesting means indicators stay uninitialized for the
        # first N bars of a live session.
        lookback_bars = self.config.slow_period + 10
        self.request_bars(
            self.config.bar_type,
            start=self._clock.utc_now() - timedelta(hours=lookback_bars),
        )
        self.subscribe_bars(self.config.bar_type)

        # Sandbox's SimulatedExchange needs quote ticks to maintain a market
        # for order fills. Without this, orders are rejected with "no market".
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate MA crossover on each bar."""
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        price = Decimal(str(bar.close))
        is_flat = self.portfolio.is_flat(self.config.instrument_id)
        is_net_long = self.portfolio.is_net_long(self.config.instrument_id)
        is_net_short = self.portfolio.is_net_short(self.config.instrument_id)

        # BUY signal: fast MA >= slow MA
        if self.fast_ma.value >= self.slow_ma.value:
            if is_flat:
                self._enter(OrderSide.BUY, price)
            elif is_net_short:
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.BUY, price)

        # SELL signal: fast MA < slow MA
        elif self.fast_ma.value < self.slow_ma.value:
            if is_flat:
                self._enter(OrderSide.SELL, price)
            elif is_net_long:
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.SELL, price)

    def _enter(self, side: OrderSide, price: Decimal) -> None:
        """Submit a market order sized via SizingConfig."""
        if self.instrument is None:
            self.log.error("Instrument not loaded — cannot enter position")
            return
        if price <= 0:
            self.log.warning("Invalid price — cannot compute quantity")
            return

        venue = self.config.instrument_id.venue
        account = self.cache.account_for_venue(venue)
        equity = (
            account.balance_total(self.instrument.settlement_currency).as_decimal()
            if account is not None
            else Decimal("0")
        )
        notional = compute_notional(equity, self._sizing, self.instrument)
        if notional <= 0:
            self.log.warning(
                f"Computed notional={notional} (equity={equity}) — skipping entry",
            )
            return

        qty = self.instrument.make_qty(notional / price)
        if qty <= 0:
            self.log.warning(
                f"Computed qty=0 for notional={notional} at price={price}",
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
        """Reset indicators and liquidation state for engine reuse (parameter sweeps)."""
        super().on_reset()
        self.fast_ma.reset()
        self.slow_ma.reset()
