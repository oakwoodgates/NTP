"""Backtest engine helpers — shared infrastructure for notebook workflows.

Wraps NT's BacktestEngine setup and parameter sweep boilerplate so that
notebooks only need to provide strategy-specific configuration.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec
import numpy as np
import pandas as pd
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common import Environment
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.rust.model import PositionSide, TradingState
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

from src.actors.account_alive import (
    AccountAliveMonitor,
    AccountAliveMonitorConfig,
)
from src.backtesting.metrics import (
    TradeRecord,
    compute_all_metrics,
)
from src.core.liquidation import (
    TOPIC_ACCOUNT_LIQUIDATED,
    TOPIC_POSITION_LIQUIDATED,
    AccountLiquidated,
    LiquidationConfig,
    PositionLiquidated,
)
from src.core.sizing import resolve_min_trade_notional

if TYPE_CHECKING:
    from collections.abc import Callable

    from nautilus_trader.common.component import LogGuard
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import Instrument

    from src.core.sizing import SizingConfig
    from src.core.venues import VenueConfig


# ── Default sweep output directory ───────────────────────────────────────────
_DEFAULT_SWEEP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "sweeps"


# ── Sweep parquet schema version ─────────────────────────────────────────────
# Bumped whenever sweep result columns are added or removed.
#
#   v1 — original (general + PnL + returns analyzer dump)
#   v2 — returns analyzer columns dropped; see docs/ANALYZER_RETURNS_CAVEAT.md
#
# Loaders should check ``_schema_version`` and adapt or warn on mismatch.
SWEEP_SCHEMA_VERSION = 2


# ── Rust LogGuard singleton ──────────────────────────────────────────────────
# NT 1.225.0's Rust logging subsystem panics if re-initialized after the
# LogGuard is freed (i.e. after an engine.dispose()).  BacktestNode solves
# this by capturing the guard once and keeping it alive (node.py:369-374).
# We mirror that pattern here so that make_engine() can create unlimited
# fresh engines without triggering the Rust panic.
_log_guard: LogGuard | None = None


def _ensure_log_guard(log_level: str = "ERROR") -> None:
    """Initialize the Rust logger once and capture the LogGuard."""
    global _log_guard
    if _log_guard is not None:
        return
    init_engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level=log_level)),
    )
    _log_guard = init_engine.kernel.get_log_guard()
    init_engine.dispose()


def _native_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convert numpy scalars to Python native types."""
    return {k: v.item() if hasattr(v, "item") else v for k, v in params.items()}


class _LiquidationCounters:
    """Per-run accumulator for liquidation telemetry published on the msgbus.

    Tracks:
    - position_count: number of mixin-driven position liquidations.
    - account_event: first AccountLiquidated event seen (latched).
    - denied_post_halt: count of OrderDenied events with reason="TradingState.HALTED".
      Useful sanity check that HALTED is actually denying new submits — for a
      dead combo with continuing signals we expect this to be > 0.
    - position_events: full list of PositionLiquidated events. The sweep
      uses these to compute trigger-vs-fill slippage statistics — the
      simulator's "gap risk" quality signal.
    """

    def __init__(self) -> None:
        self.position_count: int = 0
        self.account_event: AccountLiquidated | None = None
        self.denied_post_halt: int = 0
        self.position_events: list[PositionLiquidated] = []

    def on_position_liquidated(self, msg: Any) -> None:
        if isinstance(msg, PositionLiquidated):
            self.position_count += 1
            self.position_events.append(msg)

    def on_account_liquidated(self, msg: Any) -> None:
        if isinstance(msg, AccountLiquidated) and self.account_event is None:
            self.account_event = msg

    def on_order_event(self, msg: Any) -> None:
        # OrderDenied has a `reason` str. We only count denials caused by
        # the AccountAliveMonitor halting trading (vs throttling, reduce-only
        # mismatches, etc).
        if (
            type(msg).__name__ == "OrderDenied"
            and "TradingState.HALTED" in (getattr(msg, "reason", None) or "")
        ):
            self.denied_post_halt += 1


def _slippage_pct(event: PositionLiquidated) -> Decimal:
    """Compute liquidation-stop slippage as a % of entry price.

    Sign convention: positive = worse than trigger (gap-risk loss).
    For a long, fill below trigger is worse → slippage = trigger − fill.
    For a short, fill above trigger is worse → slippage = fill − trigger.

    Returns Decimal("0") if entry_price is 0 (defensive — shouldn't happen).
    """
    if event.entry_price <= 0:
        return Decimal("0")
    if event.side == PositionSide.LONG:
        delta = event.trigger_price - event.fill_price
    else:
        delta = event.fill_price - event.trigger_price
    return (delta / event.entry_price) * Decimal("100")


def make_engine(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    leverage: int = 1,
    log_level: str = "ERROR",
    *,
    environment: Environment = Environment.BACKTEST,
    liquidation: LiquidationConfig | None = None,
    venue_config: VenueConfig | None = None,
    sizing: SizingConfig | None = None,
) -> BacktestEngine:
    """Create a configured BacktestEngine with venue, instrument, and data.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in the instrument's settlement currency.
    leverage
        Account leverage (e.g. 20 for 20x). Passed as default_leverage
        to the simulated venue.
    log_level
        NT log level. Default ``"ERROR"`` to avoid stdout flooding.
    environment
        Defensive parameter — must be ``Environment.BACKTEST`` (the
        default).  ``make_engine`` returns a ``BacktestEngine``; live
        and sandbox runs use ``TradingNode`` directly via
        ``scripts/run_live.py`` and ``scripts/run_sandbox.py``.
        Passing ``Environment.LIVE`` or ``Environment.SANDBOX`` here
        raises ``ValueError`` with a pointer to the right runner script.
    liquidation
        When set, registers the ``AccountAliveMonitor`` actor so the
        engine simulates account-level liquidation (HALTED via
        ``RiskEngine.set_trading_state``) on equity floor breach.
        Per-position liquidation lives in the strategy's
        ``LiquidationAware`` mixin and is configured via the strategy
        config's ``liquidation`` field — pass the resolved
        :class:`LiquidationConfig` returned by
        :func:`resolve_strategy_liquidation_config` into both.
    venue_config
        Required when ``liquidation`` is set.  Source of mm_rate /
        fee_rate defaults (see :class:`~src.core.venues.VenueConfig`).
    sizing
        Optional.  When set, contributes to the
        ``min_trade_notional`` resolution order
        (``min_notional`` → ``fixed_notional``).

    Raises
    ------
    ValueError
        If ``environment`` is not ``Environment.BACKTEST``.

    """
    if environment != Environment.BACKTEST:
        msg = (
            f"make_engine() builds a BacktestEngine; got environment={environment!r}. "
            "For live / sandbox use TradingNode directly — see "
            "scripts/run_live.py and scripts/run_sandbox.py. "
            "Pass `liquidation=liquidation_for_environment(cfg, env)` to those "
            "scripts to disable / adjust the simulator per environment."
        )
        raise ValueError(msg)

    _ensure_log_guard(log_level)
    engine = BacktestEngine(config=BacktestEngineConfig())
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(starting_capital, instrument.settlement_currency)],
        default_leverage=Decimal(leverage),
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)

    if liquidation is not None and liquidation.enabled:
        if venue_config is None:
            msg = (
                "make_engine(liquidation=...) requires venue_config to resolve "
                "mm_rate and fee_rate per-venue. Pass the VenueConfig from "
                "src.core.venues.get_venue_config(...)."
            )
            raise ValueError(msg)
        _register_account_alive_monitor(
            engine=engine,
            instrument=instrument,
            leverage=leverage,
            liquidation=liquidation,
            venue_config=venue_config,
            sizing=sizing,
        )

    return engine


def resolve_strategy_liquidation_config(
    user: LiquidationConfig | None,
    venue_config: VenueConfig,
    instrument: Instrument,
    sizing: SizingConfig | None = None,
) -> LiquidationConfig | None:
    """Return a fully-resolved LiquidationConfig for embedding on a strategy.

    The strategy's ``LiquidationAware`` mixin reads ``mm_rate`` directly
    off this config; ``make_engine`` cannot mutate the strategy config
    after construction, so callers must build the resolved config first
    and pass it into both the strategy and ``make_engine``.

    Returns ``None`` (or the original config with ``enabled=False``) when
    liquidation is disabled — the strategy mixin no-ops in that case.
    """
    if user is None or not user.enabled:
        return user
    mm_rate = user.mm_rate if user.mm_rate is not None else venue_config.mm_rate
    fee_rate = (
        user.fee_rate
        if user.fee_rate is not None
        else _instrument_taker_fee(instrument)
    )
    min_trade_notional = resolve_min_trade_notional(
        sizing=sizing,
        instrument=instrument,
        explicit=user.min_trade_notional,
    )
    return LiquidationConfig(
        enabled=user.enabled,
        mm_rate=mm_rate,
        fee_rate=fee_rate,
        min_trade_notional=min_trade_notional,
        alive_trades_buffer=user.alive_trades_buffer,
        halt_on_account_liquidation=user.halt_on_account_liquidation,
    )


def liquidation_for_environment(
    config: LiquidationConfig | None,
    environment: Environment,
) -> LiquidationConfig | None:
    """Adjust a LiquidationConfig for the given run environment.

    The simulator is appropriate in some environments and dangerous in
    others. This helper enforces the right behavior so a notebook config
    that gets copy-pasted into a live runner doesn't put simulated stops
    on a real venue's order book.

    Mapping
    -------
    - ``BACKTEST`` — return the config as-is. Both per-position
      liquidation stops and account halts are appropriate.
    - ``SANDBOX`` — return a copy with ``halt_on_account_liquidation=False``.
      Sandbox runs ``SimulatedExchange`` against live data (the same
      no-margin-enforcement bug applies, so simulating liquidation IS
      appropriate), but you typically don't want a paper-trading
      session to stop dead on a single liquidation event — the goal is
      ongoing observation.
    - ``LIVE`` — return ``None``. The venue handles its own liquidation;
      our stops on top of the real book would conflict with HL's own
      forced close.

    Use this in ``scripts/run_live.py`` and ``scripts/run_sandbox.py``::

        strategy_liquidation = liquidation_for_environment(
            config=USER_LIQUIDATION_CONFIG,
            environment=Environment.LIVE,   # or SANDBOX
        )
        # Pass strategy_liquidation into the strategy config so the
        # mixin no-ops in live (None) or keeps placing stops without
        # halting in sandbox.
    """
    if environment == Environment.BACKTEST:
        return config
    if environment == Environment.LIVE:
        return None
    if environment == Environment.SANDBOX:
        if config is None or not config.enabled:
            return config
        return msgspec.structs.replace(config, halt_on_account_liquidation=False)
    msg = f"Unknown environment: {environment!r}"
    raise ValueError(msg)


def _register_account_alive_monitor(
    *,
    engine: BacktestEngine,
    instrument: Instrument,
    leverage: int,
    liquidation: LiquidationConfig,
    venue_config: VenueConfig,
    sizing: SizingConfig | None,
) -> None:
    """Construct AccountAliveMonitor and register it on the engine.

    Resolves all config values up front so the actor sees no Nones.
    Builds the halt callback against the engine's RiskEngine.
    """
    mm_rate = liquidation.mm_rate or venue_config.mm_rate
    fee_rate = liquidation.fee_rate or _instrument_taker_fee(instrument)
    min_trade_notional = resolve_min_trade_notional(
        sizing=sizing,
        instrument=instrument,
        explicit=liquidation.min_trade_notional,
    )

    monitor_config = AccountAliveMonitorConfig(
        venue=venue_config.nt_venue,
        settlement_currency=str(instrument.settlement_currency),
        venue_leverage=leverage,
        min_trade_notional=min_trade_notional,
        fee_rate=fee_rate,
        alive_trades_buffer=liquidation.alive_trades_buffer,
    )

    halt_callback: Callable[[], None] | None = None
    if liquidation.halt_on_account_liquidation:
        risk_engine = engine.kernel.risk_engine

        def _halt() -> None:
            risk_engine.set_trading_state(TradingState.HALTED)

        halt_callback = _halt

    monitor = AccountAliveMonitor(monitor_config, halt_callback=halt_callback)
    engine.add_actor(monitor)

    # NT's MessageBus caches concrete-topic → subscriber lists on first
    # publish. Subscriptions added later (e.g., from actor.on_start
    # during engine.run()) are NOT inserted into existing concrete-topic
    # caches, and the cache is only re-resolved when its subscriber list
    # is empty. So a late wildcard subscription to `events.account.*` is
    # silently lost if any AccountState event has already published —
    # which it has, because adding the venue with starting balances
    # triggers an initial AccountState before this actor is started.
    #
    # Solution: subscribe BEFORE engine.run() with a closure that
    # forwards into the actor's handler. The actor's `on_start` no
    # longer subscribes (kept as a no-op for symmetry with NT lifecycle).
    engine.kernel.msgbus.subscribe(
        topic="events.account.*",
        handler=monitor._on_account_state,
    )

    # Silence unused-import warning at the type-check layer.
    _ = mm_rate  # mm_rate is consumed by the strategy mixin via LiquidationConfig


def _instrument_taker_fee(instrument: Instrument) -> Decimal:
    """Read the instrument's taker fee as Decimal."""
    raw = getattr(instrument, "taker_fee", None)
    if raw is None:
        return Decimal("0")
    if hasattr(raw, "as_decimal"):
        return raw.as_decimal()  # type: ignore[no-any-return]
    return Decimal(str(raw))


def _trade_records_from_positions(positions: list[Any]) -> list[TradeRecord]:
    """Extract closed trades into the metric module's plain-data record.

    Open positions (no ``ts_closed``) are skipped — they have no realized
    PnL yet, and metrics must be deterministic against closed trades only.
    Positions with ``realized_pnl=None`` are skipped for the same reason.
    """
    out: list[TradeRecord] = []
    for p in positions:
        if not p.is_closed:
            continue
        if p.realized_pnl is None:
            continue
        side_name = "LONG" if p.entry.name == "BUY" else "SHORT"
        out.append(
            TradeRecord(
                pnl=float(p.realized_pnl.as_decimal()),
                ts_opened_ns=int(p.ts_opened),
                ts_closed_ns=int(p.ts_closed),
                side=side_name,
            ),
        )
    return out


def _bar_interval_ns_from_bars(bars: list[Bar]) -> int | None:
    """Infer bar interval from the first two bars in the data.

    Used to convert raw nanosecond durations into "bars" in the metrics
    output.  Returns ``None`` for single-bar or empty inputs — the
    metrics module then falls back to seconds.
    """
    if len(bars) < 2:
        return None
    return int(bars[1].ts_event - bars[0].ts_event)


def _sum_commissions(engine: BacktestEngine, instrument: Instrument) -> Decimal:
    """Sum per-position commission across all closed + open positions.

    Each position carries a per-currency dict of accumulated commissions.
    We pick the entry matching the instrument's settlement currency and
    sum across positions. Used by the sweep schema to cross-check the
    fee model: total_fees / num_positions should be roughly
    ``2 × avg_notional × taker_fee`` (round-trip per position).
    """
    settlement = instrument.settlement_currency
    total = Decimal("0")
    all_positions = engine.cache.position_snapshots() + engine.cache.positions()
    for pos in all_positions:
        for comm in pos.commissions():
            if comm.currency == settlement:
                total += comm.as_decimal()
    return total


def run_single_backtest(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    params: dict[str, Any],
    add_strategy: Callable[[BacktestEngine], None],
    score_from_ns: int | None = None,
    leverage: int = 1,
    log_level: str = "ERROR",
    *,
    liquidation: LiquidationConfig | None = None,
    venue_config: VenueConfig | None = None,
    sizing: SizingConfig | None = None,
) -> dict[str, Any]:
    """Run one backtest and return a flat dict of results.

    Creates a fresh engine, runs the backtest, extracts stats, detects
    liquidation, and disposes the engine. Designed for parameter sweeps.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in the instrument's settlement currency.
    params
        Sweep parameters (e.g. ``{"fast": 10, "slow": 50}``).
        Passed through to the returned dict as-is.
    add_strategy
        Callback that receives the engine and must call
        ``engine.add_strategy(...)`` with the desired strategy.
    score_from_ns
        If provided, only positions opened at or after this nanosecond
        timestamp are included in PnL scoring.  Used by walk-forward
        analysis to exclude trades that fire during the warmup period
        (bars prepended for indicator initialization).  When ``None``
        (the default), all positions are scored.
    leverage
        Account leverage (e.g. 20 for 20x).
    log_level
        NT log level.
    liquidation, venue_config, sizing
        Forwarded to :func:`make_engine`. When ``liquidation`` is set,
        the row dict gains ``liquidated_positions``, ``liquidated_account``,
        and ``liquidated_at_ts`` columns.

    Returns
    -------
    dict[str, Any]
        Contains all keys from *params* plus ``total_pnl``,
        ``total_pnl_pct``, ``num_positions``, ``final_balance``,
        ``min_balance``, ``error``, and analyzer performance stats.
        When ``liquidation`` is set: ``liquidated_positions`` (int),
        ``liquidated_account`` (bool), ``liquidated_at_ts`` (str | None).

    """
    eng = make_engine(
        venue,
        instrument,
        bars,
        starting_capital,
        leverage,
        log_level,
        liquidation=liquidation,
        venue_config=venue_config,
        sizing=sizing,
    )

    # Subscribe sweep counters to the liquidation topics before strategies run.
    # NB: subscriptions added BEFORE engine.run() are registered into the
    # msgbus's _subscriptions list so they get picked up on first publish
    # to a matching topic. See the long comment in _register_account_alive_monitor.
    liq_state = _LiquidationCounters()
    if liquidation is not None and liquidation.enabled:
        eng.kernel.msgbus.subscribe(
            topic=TOPIC_POSITION_LIQUIDATED,
            handler=liq_state.on_position_liquidated,
        )
        eng.kernel.msgbus.subscribe(
            topic=TOPIC_ACCOUNT_LIQUIDATED,
            handler=liq_state.on_account_liquidated,
        )
        # OrderDenied events flow on `events.order.*`. Counting HALT-reasoned
        # denials gives us a sanity check that HALTED is actually rejecting
        # post-halt submits, not just appearing to.
        eng.kernel.msgbus.subscribe(
            topic="events.order.*",
            handler=liq_state.on_order_event,
        )

    add_strategy(eng)

    row: dict[str, Any] = {**params}

    try:
        eng.run()

        a = eng.portfolio.analyzer
        acct = eng.cache.account_for_venue(venue)
        pos = eng.cache.position_snapshots() + eng.cache.positions()

        # Filter to only positions opened in the scoring window
        if score_from_ns is not None:
            pos = [p for p in pos if p.ts_opened >= score_from_ns]

        # Pull account report unconditionally — needed by both the metrics
        # block (balance series) and the liquidation-detection block.
        acct_report = eng.trader.generate_account_report(venue)

        if acct is None:
            row["error"] = "no account"
            row.update(
                total_pnl=np.nan, total_pnl_pct=np.nan,
                num_positions=len(pos), final_balance=np.nan,
                min_balance=np.nan,
            )
        else:
            a.calculate_statistics(acct, pos)
            currency = instrument.settlement_currency
            balance = float(acct.balance_total(currency))

            # When scoring a subset of positions, derive PnL from
            # those positions — not the account (which includes warmup).
            # Use the account balance at the scoring boundary as the
            # capital base for pct calculation, not starting_capital.
            if score_from_ns is not None and pos:
                scored_pnl = sum(
                    float(p.realized_pnl.as_decimal())
                    for p in pos
                    if p.realized_pnl is not None
                )

                # Look up account balance at the scoring start so
                # total_pnl_pct reflects actual capital, not the
                # original starting_capital (which doesn't account
                # for warmup trades).
                score_capital = float(starting_capital)
                if not acct_report.empty:
                    score_ts = pd.Timestamp(score_from_ns, unit="ns", tz="UTC")
                    prior = acct_report.loc[acct_report.index <= score_ts]
                    if not prior.empty:
                        score_capital = float(prior["total"].iloc[-1])

                row.update(
                    total_pnl=scored_pnl,
                    total_pnl_pct=(
                        scored_pnl / score_capital * 100
                        if score_capital > 0
                        else 0.0
                    ),
                    num_positions=len(pos),
                    final_balance=balance,
                    error="",
                )
            else:
                row.update(
                    total_pnl=float(a.total_pnl(currency)),
                    total_pnl_pct=float(a.total_pnl_percentage(currency)),
                    num_positions=len(pos),
                    final_balance=balance,
                    error="",
                )

            # Detect if equity ever hit zero during the run
            if not acct_report.empty:
                min_bal = acct_report["total"].astype(float).min()
                row["min_balance"] = min_bal
                if min_bal <= 0:
                    row["error"] = "liquidated"
            else:
                row["min_balance"] = balance

            # Pull only the trustworthy analyzer stats into the sweep row.
            # The Returns section (Sharpe, Sortino, Volatility, Returns
            # Profit Factor, Avg Return, Risk Return Ratio) is deliberately
            # NOT persisted — it's derived from a zero-padded daily returns
            # series that biases all returns-derived stats for any strategy
            # that doesn't trade every day.  See
            # docs/ANALYZER_RETURNS_CAVEAT.md for the analysis and the list
            # of stats that will return when upstream lands the fix.
            for stats_name, stats_fn in [
                ("general", a.get_performance_stats_general),
                ("PnL", lambda: a.get_performance_stats_pnls(currency)),
            ]:
                try:
                    for k, v in stats_fn().items():
                        row[k] = v
                except Exception as e:
                    print(f"  Warning: {stats_name} stats failed for {params}: {e}")

        # ── Trustworthy v2 metrics (see src/backtesting/metrics.py) ──
        # Computed from closed positions + event-time balance series.
        # Independent of the broken NT returns-series methodology.
        # Runs in both the no-account and normal paths so the schema is
        # uniform across rows even when a backtest produces no fills.
        try:
            trades = _trade_records_from_positions(pos)
            bar_interval_ns = _bar_interval_ns_from_bars(bars)
            balance_series = (
                acct_report["total"].astype(float)
                if not acct_report.empty
                else pd.Series(dtype=float)
            )
            fees_so_far = (
                float(_sum_commissions(eng, instrument))
                if acct is not None
                else 0.0
            )
            row.update(
                compute_all_metrics(
                    trades,
                    balance_series,
                    starting_capital=float(starting_capital),
                    total_bars=len(bars),
                    bar_interval_ns=bar_interval_ns,
                    first_bar_ts_ns=int(bars[0].ts_event) if bars else None,
                    last_bar_ts_ns=int(bars[-1].ts_event) if bars else None,
                    total_fees=fees_so_far,
                    total_pnl=float(row.get("total_pnl", float("nan"))),
                ),
            )
        except Exception as e:
            print(f"  Warning: v2 metrics failed for {params}: {e}")

    except Exception as e:
        row["error"] = str(e)
        row.update(
            total_pnl=np.nan, total_pnl_pct=np.nan,
            num_positions=0, final_balance=np.nan, min_balance=np.nan,
        )
    finally:
        # Always populate liquidation columns — when liquidation is off
        # they're zero/false/None, keeping the schema consistent across rows.
        row["liquidated_positions"] = liq_state.position_count
        row["liquidated_account"] = liq_state.account_event is not None
        row["liquidated_at_ts"] = (
            pd.Timestamp(liq_state.account_event.ts_event, unit="ns", tz="UTC").isoformat()
            if liq_state.account_event is not None
            else None
        )
        row["denied_post_halt"] = liq_state.denied_post_halt

        # Consolidated single-flag indicator — True if EITHER the
        # account-level alive monitor halted OR balance hit zero
        # post-hoc (caught via min_balance check, useful when the
        # liquidation simulator was disabled).  This is the column to
        # filter on in compare_sweeps / validate_strategy when ranking.
        row["liquidated"] = bool(
            row.get("error") == "liquidated"
            or row.get("liquidated_account", False),
        )

        # Liquidation-stop slippage: trigger vs actual fill, as % of entry.
        # NaN when no liquidations fired (most rows).  Average and max
        # across the per-row events let us spot bar-decomposition gap risk.
        if liq_state.position_events:
            slips = [_slippage_pct(ev) for ev in liq_state.position_events]
            row["liq_slippage_avg_pct"] = float(sum(slips) / len(slips))
            row["liq_slippage_max_pct"] = float(max(slips))
        else:
            row["liq_slippage_avg_pct"] = np.nan
            row["liq_slippage_max_pct"] = np.nan

        # Sum commissions across all closed + open positions in the settlement
        # currency. Cross-checks the fee model — total_fees / num_positions
        # should be roughly 2 × notional × taker_fee.
        try:
            row["total_fees"] = float(_sum_commissions(eng, instrument))
        except Exception as e:
            print(f"  Warning: fee sum failed for {params}: {e}")
            row["total_fees"] = np.nan
        eng.dispose()

    return row


# ── Sweep orchestration + persistence ────────────────────────────────────────


def run_sweep(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    param_combos: list[dict[str, Any]],
    strategy_factory: Callable[[BacktestEngine, dict[str, Any]], None],
    *,
    strategy_name: str,
    instrument_id: str,
    bar_interval: str,
    sweep_name: str | None = None,
    save_sweep: bool = True,
    sweep_dir: str | Path = _DEFAULT_SWEEP_DIR,
    leverage: int = 1,
    log_level: str = "ERROR",
    verbose: bool = True,
    liquidation: LiquidationConfig | None = None,
    venue_config: VenueConfig | None = None,
    sizing: SizingConfig | None = None,
) -> pd.DataFrame:
    """Run a parameter sweep, persist results to Parquet, return DataFrame.

    Iterates over *param_combos*, calling ``run_single_backtest`` for each.
    Adds metadata columns so the saved file is self-describing.  Writes
    to ``sweep_dir`` with a deterministic filename based on strategy,
    instrument, and bar interval (re-running overwrites the previous
    sweep for the same combination).

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add to each engine.
    bars
        Bar data to feed to each engine.
    starting_capital
        Starting balance in the instrument's settlement currency.
    param_combos
        List of parameter dicts, e.g.
        ``[{"fast": 10, "slow": 50}, {"fast": 10, "slow": 75}, ...]``.
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy to the
        engine using the given params.  Example::

            def ma_factory(eng, params):
                cfg = MACrossConfig(
                    instrument_id=instrument.id,
                    bar_type=BarType.from_str(BAR_TYPE_STR),
                    trade_notional=TRADE_NOTIONAL,
                    ma_type="EMA",
                    fast_period=params["fast"],
                    slow_period=params["slow"],
                )
                eng.add_strategy(MACross(cfg))

    strategy_name
        Human-readable strategy label, e.g. ``"MACross-EMA"``.
    instrument_id
        Instrument string, e.g. ``"BTC-USD-PERP.HYPERLIQUID"``.
    bar_interval
        Bar interval string, e.g. ``"1h"`` or ``"5m"``.
    sweep_name
        If provided, use this as the Parquet filename stem
        (``"{sweep_name}.parquet"``).  When ``None``, the filename is
        auto-generated from *strategy_name*, *instrument_id*, and
        *bar_interval*.
    save_sweep
        Whether to save the sweep to a Parquet file.
    sweep_dir
        Directory for Parquet output.  Created if it doesn't exist.
    log_level
        NT log level passed to each engine.
    verbose
        Print per-combo progress lines.

    Returns
    -------
    pd.DataFrame
        One row per param combo with all stats, plus metadata columns
        prefixed with ``_`` (``_strategy``, ``_instrument_id``, etc.).

    """

    total = len(param_combos)
    results: list[dict[str, Any]] = []
    t0 = time.monotonic()

    for i, params in enumerate(param_combos, 1):
        # Separate strategy params from metadata.  Any key starting with
        # "_" is metadata that follows the row into the parquet but is
        # NOT passed to the user's strategy_factory.  This lets users tag
        # rows (e.g. ``_kind: "spotlight"``, ``_note: "Fib pair"``) without
        # the tags leaking into strategy construction.
        strategy_params = {k: v for k, v in params.items() if not k.startswith("_")}

        # Bind strategy_params at lambda-definition time so closure capture
        # picks up *this* iteration's value, not the loop variable.
        def _add(eng: BacktestEngine, p: dict[str, Any] = strategy_params) -> None:
            strategy_factory(eng, p)

        row = run_single_backtest(
            venue=venue,
            instrument=instrument,
            bars=bars,
            starting_capital=starting_capital,
            params=params,
            add_strategy=_add,
            leverage=leverage,
            log_level=log_level,
            liquidation=liquidation,
            venue_config=venue_config,
            sizing=sizing,
        )
        results.append(row)

        if verbose:
            pnl = row.get("total_pnl", float("nan"))
            pnl_pct = row.get("total_pnl_pct", float("nan"))
            npos = row.get("num_positions", 0)
            err = f"  !! {row['error']}" if row.get("error") else ""
            # Display strategy params; flag spotlight/etc rows visibly.
            param_str = ", ".join(f"{k}={v}" for k, v in strategy_params.items())
            kind = params.get("_kind")
            kind_tag = f" [{kind}]" if kind else ""
            print(
                f"  [{i}/{total}]{kind_tag} {param_str}  "
                f"PnL={pnl:>10.2f} PnL%={pnl_pct:>7.2f}%"
                f"  positions={npos}{err}"
            )

    elapsed = time.monotonic() - t0

    # ── Build DataFrame with metadata ────────────────────────────────────
    df = pd.DataFrame(results)

    # Data date range from the bars themselves
    data_start = pd.Timestamp(bars[0].ts_event, unit="ns", tz="UTC")
    data_end = pd.Timestamp(bars[-1].ts_event, unit="ns", tz="UTC")
    swept_at = datetime.now(tz=UTC)

    df.insert(0, "_strategy", strategy_name)
    df.insert(1, "_instrument_id", instrument_id)
    df.insert(2, "_bar_interval", bar_interval)
    df.insert(3, "_starting_capital", starting_capital)
    df.insert(4, "_data_start", data_start.isoformat())
    df.insert(5, "_data_end", data_end.isoformat())
    df.insert(6, "_swept_at", swept_at.isoformat())
    # Schema version — bump when columns added/removed.
    #   v1: original (general + PnL + returns analyzer dump)
    #   v2: returns analyzer columns dropped (see ANALYZER_RETURNS_CAVEAT.md)
    df.insert(7, "_schema_version", SWEEP_SCHEMA_VERSION)

    # ── Persist to Parquet ────────────────────────────────────────────────
    # Deterministic name: re-running the same strategy+instrument+interval
    # overwrites the previous file.  Timestamp is NOT in the filename —
    # _swept_at inside the file records when it was generated.
    if save_sweep:
        if sweep_name is not None:
            filename = f"{sweep_name}.parquet"
        else:
            safe_instrument = instrument_id.replace("/", "-")
            filename = f"{strategy_name}_{safe_instrument}_{bar_interval}.parquet"
        out_path = Path(sweep_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_path, index=False)
        print(f"✓ Saved → {out_path}")

    if verbose:
        print(f"✓ Sweep complete — {total} combos in {elapsed:.1f}s")

    return df


def load_sweeps(
    sweep_dir: str | Path = _DEFAULT_SWEEP_DIR,
    *,
    strategy: str | None = None,
    instrument_id: str | None = None,
    bar_interval: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Load saved sweep Parquet files into a dict keyed by label.

    Parameters
    ----------
    sweep_dir
        Directory containing sweep Parquet files.
    strategy
        If provided, only load sweeps matching this strategy name.
    instrument_id
        If provided, only load sweeps matching this instrument.
    bar_interval
        If provided, only load sweeps matching this bar interval.

    Returns
    -------
    dict[str, pd.DataFrame]
        Keys are human-readable labels derived from filename,
        e.g. ``"MACross-EMA · BTC-USD-PERP.HYPERLIQUID · 5m"``.

    """
    sweep_path = Path(sweep_dir)
    if not sweep_path.exists():
        print(f"No sweep directory found at {sweep_path}")
        return {}

    files = sorted(sweep_path.glob("*.parquet"))
    if not files:
        print(f"No Parquet files found in {sweep_path}")
        return {}

    sweeps: dict[str, pd.DataFrame] = {}
    schema_warnings: list[str] = []
    for f in files:
        df = pd.read_parquet(f)

        # Filter by metadata columns if requested
        if strategy and df["_strategy"].iloc[0] != strategy:
            continue
        if instrument_id and df["_instrument_id"].iloc[0] != instrument_id:
            continue
        if bar_interval and df["_bar_interval"].iloc[0] != bar_interval:
            continue

        # Schema-version check.  Old files predating ``_schema_version``
        # are treated as v1 (still loadable but flagged).
        file_version = (
            int(df["_schema_version"].iloc[0])
            if "_schema_version" in df.columns
            else 1
        )
        if file_version != SWEEP_SCHEMA_VERSION:
            schema_warnings.append(
                f"  {f.name}: schema v{file_version} "
                f"(current is v{SWEEP_SCHEMA_VERSION}) — "
                f"may contain stale columns; re-run sweep to refresh.",
            )

        # Build a readable label from metadata
        strat = df["_strategy"].iloc[0]
        inst = df["_instrument_id"].iloc[0]
        interval = df["_bar_interval"].iloc[0]
        label = f"{strat} · {inst} · {interval}"
        sweeps[label] = df

    print(f"Loaded {len(sweeps)} sweep(s) from {sweep_path}")
    if schema_warnings:
        print("⚠️ Some sweeps were loaded with an older schema version:")
        for warning in schema_warnings:
            print(warning)
    return sweeps


# ── Walk-forward analysis ────────────────────────────────────────────────────


def run_walk_forward(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    param_combos: list[dict[str, Any]],
    strategy_factory: Callable[[BacktestEngine, dict[str, Any]], None],
    *,
    train_pct: float = 0.50,
    test_pct: float = 0.125,
    select_by: str = "total_pnl",
    warmup_bars: int = 200,
    leverage: int = 1,
    log_level: str = "ERROR",
    verbose: bool = True,
    liquidation: LiquidationConfig | None = None,
    venue_config: VenueConfig | None = None,
    sizing: SizingConfig | None = None,
) -> pd.DataFrame:
    """Sliding-window walk-forward analysis.

    Splits *bars* into train/test windows, slides by ``test_pct`` each
    fold.  For each fold: runs a full parameter sweep on the training
    window, selects the best combo by *select_by*, then runs that combo
    on the out-of-sample test window.

    Both training and test slices are prepended with up to *warmup_bars*
    extra bars so that indicators are fully initialized before the scored
    region begins.  For both slices, any trades that fire during the
    warmup period are excluded from results via *score_from_ns*,
    preventing warmup trades from influencing parameter selection or
    OOS scoring.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add to each engine.
    bars
        Full bar dataset — sliced internally per fold.
    starting_capital
        Starting balance in the instrument's settlement currency.
    param_combos
        Parameter grid to sweep each fold (same as ``run_sweep``).
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy.
    train_pct
        Fraction of total bars for each training window. Default 0.50.
    test_pct
        Fraction of total bars for each test window. Default 0.125.
        The window slides by this amount each fold, so test windows
        are non-overlapping.
    select_by
        Column name to maximize when selecting best in-sample params.
        Default ``"total_pnl"``.
    warmup_bars
        Number of extra bars prepended to each training and test window
        so that indicators are fully initialized before the scored region
        begins.  Default 200 — covers most MA/oscillator periods.
        Override in the notebook when your slowest indicator needs more
        (or fewer).
    log_level
        NT log level.
    verbose
        Print fold-level progress.

    Returns
    -------
    pd.DataFrame
        One row per fold with columns: ``fold``, ``train_start``,
        ``train_end``, ``test_start``, ``test_end``, ``best_*`` (selected
        params), ``in_sample_pnl``, ``oos_pnl``, ``oos_pnl_pct``,
        ``oos_positions``, and selected analyzer stats.

    Example
    -------
    With 10,000 bars, ``train_pct=0.50``, ``test_pct=0.125``::

        Fold 1: train bars[0:5000],     test bars[5000:6250]
        Fold 2: train bars[1250:6250],  test bars[6250:7500]
        Fold 3: train bars[2500:7500],  test bars[7500:8750]
        Fold 4: train bars[3750:8750],  test bars[8750:10000]

    """
    import time

    total_bars = len(bars)
    train_size = int(total_bars * train_pct)
    test_size = int(total_bars * test_pct)
    step_size = test_size  # non-overlapping test windows
    n_combos = len(param_combos)

    if train_size + test_size > total_bars:
        msg = (
            f"train_pct ({train_pct}) + test_pct ({test_pct}) = {train_pct + test_pct} "
            f"exceeds 1.0 — not enough data for even one fold."
        )
        raise ValueError(msg)

    if verbose:
        n_folds_est = (total_bars - train_size - test_size) // step_size + 1
        print(
            f"Walk-forward: {total_bars:,} bars, "
            f"train={train_size:,} ({train_pct:.0%}), "
            f"test={test_size:,} ({test_pct:.1%}), "
            f"~{n_folds_est} folds × {n_combos} combos"
        )

    folds: list[dict[str, Any]] = []
    fold_num = 0
    start = 0
    t0 = time.monotonic()

    while start + train_size + test_size <= total_bars:
        fold_num += 1

        # ── Training slice with warmup padding ──────────────────────
        train_warmup_start = max(0, start - warmup_bars)
        train_slice = bars[train_warmup_start : start + train_size]
        train_score_from_ns = bars[start].ts_event

        # ── Test slice with warmup padding ──────────────────────────
        # Prepend bars from the end of the training window so indicators
        # are initialized when the real test region begins.  Trades
        # during warmup are excluded via score_from_ns below.
        test_start_idx = start + train_size
        test_warmup_start = max(0, test_start_idx - warmup_bars)
        test_slice = bars[test_warmup_start : test_start_idx + test_size]
        test_score_from_ns = bars[test_start_idx].ts_event

        train_start_ts = pd.Timestamp(train_score_from_ns, unit="ns", tz="UTC")
        train_end_ts = pd.Timestamp(train_slice[-1].ts_event, unit="ns", tz="UTC")
        test_start_ts = pd.Timestamp(test_score_from_ns, unit="ns", tz="UTC")
        test_end_ts = pd.Timestamp(test_slice[-1].ts_event, unit="ns", tz="UTC")

        if verbose:
            print(
                f"\n── Fold {fold_num} ──\n"
                f"  Train: {train_start_ts:%Y-%m-%d} → {train_end_ts:%Y-%m-%d}"
                f"  ({len(train_slice):,} bars, {warmup_bars} warmup)\n"
                f"  Test:  {test_start_ts:%Y-%m-%d} → {test_end_ts:%Y-%m-%d}"
                f"  ({len(test_slice):,} bars, {warmup_bars} warmup)"
            )

        # ── Sweep on training data (no per-combo output) ────────────
        train_results: list[dict[str, Any]] = []
        for params in param_combos:
            row = run_single_backtest(
                venue=venue,
                instrument=instrument,
                bars=train_slice,
                starting_capital=starting_capital,
                params=params,
                add_strategy=lambda eng, p=params: strategy_factory(eng, p),  # type: ignore[misc]
                score_from_ns=train_score_from_ns,
                leverage=leverage,
                log_level=log_level,
                liquidation=liquidation,
                venue_config=venue_config,
                sizing=sizing,
            )
            train_results.append(row)

        train_df = pd.DataFrame(train_results)
        valid = train_df[
            train_df[select_by].notna() & (train_df["error"].fillna("") == "")
        ]

        if valid.empty:
            if verbose:
                errors = train_df["error"].dropna()
                errors = errors[errors != ""]
                first_err = errors.iloc[0] if not errors.empty else "unknown"
                print("  ⚠️ No valid results in training — skipping fold")
                print(f"    First error: {first_err}")
            start += step_size
            continue

        best_idx = valid[select_by].idxmax()
        param_keys = list(param_combos[0].keys())
        best_params = _native_params({k: train_df.loc[best_idx, k] for k in param_keys})
        best_train_pnl = float(train_df.loc[best_idx, "total_pnl"])

        if verbose:
            param_str = ", ".join(f"{k}={v}" for k, v in best_params.items())
            print(f"  Best in-sample: {param_str}  (PnL={best_train_pnl:,.2f})")

        # ── Test best params on out-of-sample data ──────────────────
        oos_row = run_single_backtest(
            venue=venue,
            instrument=instrument,
            bars=test_slice,
            starting_capital=starting_capital,
            params=best_params,
            add_strategy=lambda eng, p=best_params: strategy_factory(eng, p),  # type: ignore[misc]
            score_from_ns=test_score_from_ns,
            leverage=leverage,
            log_level=log_level,
            liquidation=liquidation,
            venue_config=venue_config,
            sizing=sizing,
        )

        oos_pnl = oos_row.get("total_pnl", float("nan"))
        if verbose:
            oos_pnl_pct = oos_row.get("total_pnl_pct", float("nan"))
            oos_npos = oos_row.get("num_positions", 0)
            print(
                f"  Out-of-sample: PnL={oos_pnl:,.2f}  "
                f"PnL%={oos_pnl_pct:.2f}%  positions={oos_npos}"
            )
            if oos_row.get("error"):
                print(f"    ⚠️ OOS error: {oos_row['error']}")

        fold_result: dict[str, Any] = {
            "fold": fold_num,
            "train_start": train_start_ts.isoformat(),
            "train_end": train_end_ts.isoformat(),
            "test_start": test_start_ts.isoformat(),
            "test_end": test_end_ts.isoformat(),
            "train_bars": len(train_slice),
            "test_bars": len(test_slice),
        }
        for k, v in best_params.items():
            fold_result[f"best_{k}"] = v
        fold_result.update({
            "in_sample_pnl": best_train_pnl,
            "oos_pnl": oos_pnl,
            "oos_pnl_pct": oos_row.get("total_pnl_pct", float("nan")),
            "oos_positions": oos_row.get("num_positions", 0),
            "oos_error": oos_row.get("error", ""),
        })

        # Pull through any analyzer stats that made it into the OOS row
        for stat_key in [
            "Win Rate", "Profit Factor", "Sharpe Ratio (252 days)",
            "Max Drawdown", "Avg Winner", "Avg Loser", "Expectancy",
        ]:
            if stat_key in oos_row:
                fold_result[f"oos_{stat_key}"] = oos_row[stat_key]

        folds.append(fold_result)
        start += step_size

    elapsed = time.monotonic() - t0

    if not folds:
        print("No folds completed. Check data length vs train_pct / test_pct.")
        return pd.DataFrame()

    result_df = pd.DataFrame(folds)

    if verbose:
        profitable = int((result_df["oos_pnl"] > 0).sum())
        total_folds = len(result_df)
        total_oos_pnl = result_df["oos_pnl"].sum()

        print(f"\n{'─' * 50}")
        print(f"Walk-Forward Summary  ({elapsed:.1f}s)")
        print(f"{'─' * 50}")
        print(f"  Folds:          {total_folds}")
        print(
            f"  OOS profitable: {profitable}/{total_folds}"
            f"  ({profitable / total_folds * 100:.0f}%)"
        )
        print(f"  Total OOS PnL:  {total_oos_pnl:,.2f}")

        # Param stability check
        param_cols = [c for c in result_df.columns if c.startswith("best_")]
        if param_cols:
            all_same = all(result_df[c].nunique() == 1 for c in param_cols)
            if all_same:
                vals = ", ".join(
                    f"{c.removeprefix('best_')}={result_df[c].iloc[0]}"
                    for c in param_cols
                )
                print(f"  Params:         STABLE ({vals} every fold)")
            else:
                print("  Params:         UNSTABLE (different params per fold)")
                for c in param_cols:
                    # .tolist() converts numpy scalars (e.g. np.int64) to
                    # plain Python primitives so the printed list reads as
                    # ``[5, 10, 20, 75]`` rather than ``[np.int64(5), …]``
                    # under NumPy 2.x.
                    unique = sorted(result_df[c].dropna().unique().tolist())
                    print(f"    {c}: {unique}")

    return result_df
