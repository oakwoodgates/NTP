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
from src.core.protective_stop_mixin import ProtectiveStopAware
from src.core.signal_event import TOPIC_SIGNAL_MA_CROSS, SignalEvent
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


# ── Default sweep grids per MA type ────────────────────────────────────────
#
# Each MA type has a different "useful range" — EMA/SMA/DEMA can run long
# lookbacks cleanly, so they get a 12×12 grid up to 100 fast / 200 slow.
# HMA/AMA/VIDYA are heavier or less stable at long windows, so they get
# a tighter 11×10 grid capped at 50 fast / 100 slow.  These grids are
# the project's defaults shared between ``notebooks/backtest/ma_cross.ipynb``
# and ``scripts/batch_backtest.py`` — change here once, both update.
#
# Notebooks / scripts can locally extend before running a sweep, e.g.::
#
#     from src.strategies.ma_cross import MA_FAST_GRIDS
#     fast_grid = MA_FAST_GRIDS["EMA"] + [120]   # try one bigger lookback

MA_FAST_GRIDS: dict[str, list[int]] = {
    "EMA":   [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100],
    "SMA":   [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100],
    "DEMA":  [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100],
    "HMA":   [5,  8, 10, 12, 15, 20, 25, 30, 35, 40, 50],
    "AMA":   [5,  8, 10, 12, 15, 20, 25, 30, 35, 40, 50],
    "VIDYA": [5,  8, 10, 12, 15, 20, 25, 30, 35, 40, 50],
}

MA_SLOW_GRIDS: dict[str, list[int]] = {
    "EMA":   [10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100, 200],
    "SMA":   [10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100, 200],
    "DEMA":  [10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100, 200],
    "HMA":   [10, 15, 20, 25, 30, 35, 40, 50, 75, 100],
    "AMA":   [10, 15, 20, 25, 30, 35, 40, 50, 75, 100],
    "VIDYA": [10, 15, 20, 25, 30, 35, 40, 50, 75, 100],
}

# Off-grid "spotlight" combos mixed into the sweep alongside the regular
# fast/slow grid.  Each entry is a param dict with ``_kind: "spotlight"``
# (and optional ``_note: ...``).  The ``_kind`` tag passes through to the
# sweep parquet but is stripped before the strategy factory sees the
# params; the heatmap silently excludes spotlight rows so the grid stays
# clean, while the sortable HTML report badges them with [SPOT].
#
# Per-MA-type spotlights:
#   * EMA   — trader-lore pairs (8/21, 12/26 MACD, Fibonacci, 9/18)
#   * SMA   — classic 50/200 golden cross
#   * HMA/DEMA/AMA/VIDYA — empty by default (no widely-used named combos;
#     AMA/VIDYA are adaptive so fixed pairs are meaningless).
MA_SPOTLIGHTS: dict[str, list[dict[str, object]]] = {
    "EMA": [
        {"fast":  9, "slow":  18, "_kind": "spotlight", "_note": "9/18 trial"},
        {"fast":  8, "slow":  21, "_kind": "spotlight", "_note": "8/21 EMA classic"},
        {"fast": 13, "slow":  21, "_kind": "spotlight", "_note": "Fibonacci"},
        {"fast": 12, "slow":  26, "_kind": "spotlight", "_note": "MACD periods"},
    ],
    "SMA": [
        {"fast": 50, "slow": 200, "_kind": "spotlight", "_note": "Golden cross"},
    ],
    "HMA":   [],
    "DEMA":  [],
    "AMA":   [],
    "VIDYA": [],
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
    stop_pct : float | None, default None
        Protective stop-loss as a fraction of entry price (``0.05`` = 5%).
        When set, the ``ProtectiveStopAware`` mixin places a reduce-only
        ``StopMarketOrder`` at ``entry × (1 - stop_pct)`` for longs (or
        ``× (1 + stop_pct)`` for shorts) on every position open, and
        cancels/replaces it on flips.  Composes with ``liquidation``;
        whichever stop fires first reduces the position.

        For **isolated-margin equivalence under cross-margin** accounting,
        set ``stop_pct = 1 / venue_leverage`` (e.g. ``0.05`` at 20×) — this
        makes the worst-case loss per trade equal the initial margin
        committed.  See ``docs/LIQUIDATION_AND_SIZING.md``.
    bootstrap_on_deploy : bool, default False
        If True, the *first observed signal* on a freshly-started
        strategy counts as a synthetic cross and triggers an immediate
        entry.  Use this when deploying a strategy mid-trend and you
        want to pick up the current move rather than wait for the next
        cross.  Default False is the conservative steady-state
        behaviour: the strategy waits for an actual MA transition
        before entering, which is what the cross-gate semantics
        require for honest signal-event-driven trading.

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
    stop_pct: float | None = None
    bootstrap_on_deploy: bool = False


class MACross(ProtectiveStopAware, LiquidationAware, Strategy):
    """Moving-average crossover strategy using MovingAverageFactory.

    Enters on a *cross of the fast and slow MAs* — a signal **transition**,
    not a signal **state**.  Long entry fires when ``fast_ma`` crosses
    above ``slow_ma``; short entry when ``fast_ma`` crosses below.
    Designed for perpetual futures (supports both directions).

    Entry rule — cross-gated, not state-polled
    ------------------------------------------
    The strategy tracks the last signal direction it acted on.  An
    entry only fires when the current signal differs from the last
    one — i.e. on a fresh transition.  Same-direction bars after a
    cross do nothing.

    The same rule covers all exit causes uniformly: after *any* exit
    (strategy cross-back, ``ProtectiveStopAware`` stop, ``LiquidationAware``
    stop, take-profit, trailing stop, account liquidation), the
    last-signal state persists, so the next entry waits for the next
    *new* cross.  Re-entering on an unchanged signal that just produced
    a stop-out is denied: a stop is information that the market
    disagreed with the signal at that price level, and acting on the
    same stale signal is just polling on a state the market has
    already disproved.

    Bootstrap (live-deploy override)
    --------------------------------
    Set ``config.bootstrap_on_deploy=True`` to treat the *first*
    observed signal on a freshly-started strategy as a synthetic
    cross.  Use this when deploying mid-trend and you want to pick
    up the current move rather than wait for the next MA transition.
    Defaults to False — the conservative steady-state behaviour.

    Inheritance order
    -----------------
    Mixins MUST come before ``Strategy``. NT calls the typed handlers
    (``on_position_opened`` etc.) by name; ``Strategy`` defines those
    as concrete no-op stubs, so ``(Strategy, ...mixins)`` order finds
    the stubs first via MRO and the mixins silently never run.

    Mixin composition
    -----------------
    ``ProtectiveStopAware`` places a fixed-pct reduce-only stop and
    ``LiquidationAware`` places a cross-margin reduce-only stop at a
    (typically wider) liq price.  Whichever fires first reduces the
    position; NT's reduce-only logic cancels the other on fill.

    Parameters
    ----------
    config : MACrossConfig
        The configuration for the instance.

    Raises
    ------
    ValueError
        If `config.fast_period` is not less than `config.slow_period`.
        If `config.ma_type` is not a recognized type.
        If neither `config.sizing` nor `config.trade_notional` is set.
        If `config.stop_pct` is set but >= 1 (likely a percentage-vs-fraction
        unit error — pass 0.05 for 5%, not 5.0).

    """

    def __init__(self, config: MACrossConfig) -> None:
        PyCondition.is_true(
            config.fast_period < config.slow_period,
            f"{config.fast_period=} must be less than {config.slow_period=}",
        )
        PyCondition.is_in(config.ma_type, _MA_TYPE_LOOKUP, "config.ma_type", "_MA_TYPE_LOOKUP")
        super().__init__(config)
        self._init_liquidation(config.liquidation)
        self._init_protective_stop(config.stop_pct)

        # Resolve sizing: explicit SizingConfig wins; else build a fixed-mode
        # config from trade_notional for back-compat.
        self._sizing = resolve_sizing_from_strategy_config(config)

        # Cross-gate state.  Tracks the last signal direction we acted
        # on so re-entries are gated on a fresh transition rather than
        # on the polled signal *state*:
        #   0  → no signal seen yet (pre-warmup, or freshly reset)
        #  +1  → last action was on a LONG signal (fast > slow)
        #  -1  → last action was on a SHORT signal (fast < slow)
        # The state persists across position closes — strategy exits,
        # protective stops, liquidations all leave the signal direction
        # unchanged, so the next entry waits for a new cross.  See
        # the class docstring for the rationale.
        self._last_signal: int = 0
        # When ``bootstrap_on_deploy`` is True, the very first
        # observed signal counts as a synthetic cross — used for live
        # mid-trend deployment.  Cleared on first use (and on
        # ``on_reset``) so steady-state behaviour resumes immediately.
        self._bootstrap_pending: bool = bool(config.bootstrap_on_deploy)

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

    @staticmethod
    def _cross_gate_decision(
        fast_value: float,
        slow_value: float,
        last_signal: int,
        bootstrap_pending: bool,
    ) -> tuple[int, bool]:
        """Pure cross-gate decision — extracted so it's testable in isolation.

        Computes the current signal direction from the MA values and
        decides whether the caller should act.  Equal MA values map to
        a LONG signal (matches the original "fast >= slow" branch).

        Returns
        -------
        tuple[int, bool]
            ``(new_signal, should_act)`` where:

            * ``new_signal`` is ``+1`` (LONG) or ``-1`` (SHORT) — the
              signal direction observed on this bar.
            * ``should_act`` is True when this is a fresh cross (the
              new signal differs from ``last_signal``) OR when
              ``bootstrap_pending`` is True (live mid-trend deploy).
              Caller acts on ``new_signal`` and clears the
              ``bootstrap_pending`` flag.
        """
        new_signal = 1 if fast_value >= slow_value else -1
        should_act = new_signal != last_signal or bootstrap_pending
        return new_signal, should_act

    def _publish_signal_event(
        self,
        *,
        ts_event: int,
        signal: int,
        fast_value: float,
        slow_value: float,
        acted: bool,
        bootstrap: bool,
    ) -> None:
        """Publish a :class:`SignalEvent` on the message bus for persistence.

        The PersistenceActor subscribes to ``signals.*`` and writes one
        row per event to the ``signal_events`` PG table. Emitted on every
        initialized bar (not just acted ones).
        """
        event = SignalEvent(
            ts_event=ts_event,
            strategy_id=str(self.id),
            instrument_id=str(self.config.instrument_id),
            signal=signal,
            fast_value=Decimal(str(fast_value)),
            slow_value=Decimal(str(slow_value)),
            acted=acted,
            bootstrap=bootstrap,
        )
        self.msgbus.publish(topic=TOPIC_SIGNAL_MA_CROSS, msg=event)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate MA crossover on each bar.

        Cross-gated entry: the strategy acts only when the signed
        signal differs from ``self._last_signal``.  Same-direction bars
        after a cross do nothing.  After any exit (cross-back, stop,
        liquidation), ``self._last_signal`` retains the direction we
        entered on, so a stop-out doesn't trigger an immediate
        re-entry on the unchanged signal — the next entry waits for
        the next *new* MA cross.
        """
        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        fast_value = float(self.fast_ma.value)
        slow_value = float(self.slow_ma.value)
        signal, should_act = self._cross_gate_decision(
            fast_value,
            slow_value,
            self._last_signal,
            self._bootstrap_pending,
        )

        # Emit a SignalEvent EVERY initialized bar (not just acted ones)
        # so Phase 2.5 analysis can reconstruct the full per-bar gate
        # stream from PG and align it against backtest cross times.
        self._publish_signal_event(
            ts_event=bar.ts_event,
            signal=signal,
            fast_value=fast_value,
            slow_value=slow_value,
            acted=should_act,
            bootstrap=self._bootstrap_pending,
        )
        self.log.info(
            f"cross_gate: signal={signal:+d} fast={fast_value:.4f} "
            f"slow={slow_value:.4f} bootstrap={self._bootstrap_pending} "
            f"acted={should_act}",
        )

        if not should_act:
            return
        self._bootstrap_pending = False
        self._last_signal = signal

        price = Decimal(str(bar.close))
        is_flat      = self.portfolio.is_flat(self.config.instrument_id)
        is_net_long  = self.portfolio.is_net_long(self.config.instrument_id)
        is_net_short = self.portfolio.is_net_short(self.config.instrument_id)

        if signal > 0:
            # Fresh LONG signal.  Close any open SHORT first (covers a
            # cross-back when the prior position is still open — i.e.
            # no stop has fired yet).
            if is_net_short:
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.BUY, price)
            elif is_flat:
                self._enter(OrderSide.BUY, price)
            # else: already long (shouldn't normally happen on a
            # transition but guard anyway — leave the position alone).
        else:
            # Fresh SHORT signal.
            if is_net_long:
                self.close_all_positions(self.config.instrument_id)
                self._enter(OrderSide.SELL, price)
            elif is_flat:
                self._enter(OrderSide.SELL, price)
            # else: already short, no action.

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
        """Reset indicators and per-iteration state for engine reuse.

        Also clears the cross-gate state so each sweep iteration starts
        from a clean ``_last_signal=0`` (otherwise the residual signal
        from iteration N would let iteration N+1 skip its own first
        cross).  Restores the ``bootstrap_on_deploy`` flag so subsequent
        sweeps see consistent behaviour.
        """
        super().on_reset()
        self.fast_ma.reset()
        self.slow_ma.reset()
        self._last_signal = 0
        self._bootstrap_pending = bool(self.config.bootstrap_on_deploy)
