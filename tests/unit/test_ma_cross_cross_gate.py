"""Cross-gate behaviour tests for ``MACross``.

Two layers of coverage:

* ``TestCrossGateDecision`` — exhaustive tests of the pure
  ``MACross._cross_gate_decision`` helper.  This is the brain of the
  cross-gate; every entry-rule scenario reduces to one call here.

* ``TestEndToEndOnBar`` — a small set of integration smokes that walk
  a real ``MACross`` instance through the engine to confirm the wire-up
  (gate → enter, gate → no-enter, bootstrap_on_deploy, on_reset).
  These would catch regressions where the gate logic is right in
  isolation but ``on_bar`` mis-applies it.

The original BTC EMA 10/40 failure mode (three SHORTs in a row, each
stopped at -101 USDC) is the reason this gate exists.  See
``docs/ANALYZER_RETURNS_CAVEAT.md`` neighbours for the strategy
docstring on entry semantics.
"""
from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from nautilus_trader.model.data import BarType

from src.backtesting import make_engine
from src.backtesting.engine import resolve_strategy_liquidation_config
from src.core import LiquidationConfig, bar_type_str, get_venue_config
from src.strategies.ma_cross import MACross, MACrossConfig

if TYPE_CHECKING:
    from nautilus_trader.backtest.engine import BacktestEngine

# ── Pure cross-gate decision ───────────────────────────────────────────────


class TestCrossGateDecision:
    """Pin the pure ``_cross_gate_decision`` helper.

    Returns ``(new_signal, should_act)`` so callers know which direction
    to enter and whether to act.
    """

    # Ordering of the truth table:
    #   fast_value, slow_value  →  new_signal  ∈ {+1 (long), -1 (short)}
    #   last_signal             →  +1, -1, 0 (pre-warmup / reset)
    #   bootstrap_pending       →  True / False
    # Should-act fires on (new_signal != last_signal) OR bootstrap_pending.

    @pytest.mark.parametrize(
        ("fast", "slow", "last", "boot", "expected_signal", "expected_act"),
        [
            # Fresh LONG cross from no-prior-state.
            (101.0, 100.0, 0, False, 1, True),
            # Fresh SHORT cross from no-prior-state.
            (99.0,  100.0, 0, False, -1, True),
            # Same direction LONG, no fresh cross → no action.
            (101.0, 100.0, 1, False, 1, False),
            # Same direction SHORT, no fresh cross → no action.
            (99.0,  100.0, -1, False, -1, False),
            # LONG → SHORT flip → act.
            (99.0,  100.0, 1, False, -1, True),
            # SHORT → LONG flip → act.
            (101.0, 100.0, -1, False, 1, True),
            # Equal MAs map to LONG (matches original >= semantics).
            (100.0, 100.0, 0, False, 1, True),
            (100.0, 100.0, 1, False, 1, False),
            # Bootstrap forces an action even on an unchanged signal.
            (101.0, 100.0, 1, True, 1, True),
            (99.0,  100.0, -1, True, -1, True),
            # Bootstrap also flips → act regardless of bootstrap.
            (101.0, 100.0, -1, True, 1, True),
        ],
    )
    def test_decision_table(
        self,
        fast: float,
        slow: float,
        last: int,
        boot: bool,
        expected_signal: int,
        expected_act: bool,
    ) -> None:
        signal, act = MACross._cross_gate_decision(fast, slow, last, boot)
        assert signal == expected_signal
        assert act == expected_act


# ── End-to-end on_bar via real engine ──────────────────────────────────────


def _build_engine_and_strategy(
    *,
    fast_period: int = 3,
    slow_period: int = 5,
    bootstrap_on_deploy: bool = False,
) -> tuple[BacktestEngine, MACross]:
    """Build a minimal in-memory engine + MACross strategy for end-to-end tests.

    Uses a tiny synthetic bar series instead of the real catalog so the
    tests are deterministic and fast (no parquet read, no network).
    Returns (engine, strategy) so tests can poke at strategy state and
    inspect engine outputs.
    """
    from datetime import datetime

    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    venue = instrument.venue
    bar_type_string = bar_type_str(str(instrument.id), "1d")
    bar_type = BarType.from_str(bar_type_string)

    # Match the instrument's precision so SimulatedExchange accepts the bars.
    px_prec = int(instrument.price_precision)
    qty_prec = int(instrument.size_precision)
    px_fmt = f"{{:.{px_prec}f}}"
    qty_fmt = f"{{:.{qty_prec}f}}"

    # Synthetic 1d bar series.  Pattern: 30 down-bars (price 100→70),
    # then 30 up-bars (70→130), then 30 down-bars (130→100).  Two MA
    # crosses guaranteed inside the slow_period warmup window.
    closes = (
        [100.0 - i for i in range(30)]
        + [70.0 + i for i in range(60)]
        + [130.0 - i for i in range(30)]
    )
    base_ns = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9)
    one_day_ns = 86_400 * 10**9
    bars = []
    for i, c in enumerate(closes):
        ts = base_ns + i * one_day_ns
        bars.append(Bar(
            bar_type,
            Price.from_str(px_fmt.format(c - 0.5)),
            Price.from_str(px_fmt.format(c + 1.0)),
            Price.from_str(px_fmt.format(c - 1.0)),
            Price.from_str(px_fmt.format(c)),
            Quantity.from_str(qty_fmt.format(1.0)),
            ts, ts,
        ))

    venue_config = get_venue_config("HYPERLIQUID_PERP")
    liq_cfg = LiquidationConfig(
        enabled=False, halt_on_account_liquidation=False,
        min_trade_notional=Decimal("10"),
    )
    liq_res = resolve_strategy_liquidation_config(liq_cfg, venue_config, instrument)

    engine = make_engine(
        starting_capital=10_000, instrument=instrument, bars=bars,
        venue=venue, venue_config=venue_config, liquidation=liq_cfg,
        leverage=1,
    )
    strategy = MACross(MACrossConfig(
        instrument_id=instrument.id, bar_type=bar_type,
        fast_period=fast_period, slow_period=slow_period, ma_type="EMA",
        trade_notional=Decimal(1000), liquidation=liq_res,
        bootstrap_on_deploy=bootstrap_on_deploy,
    ))
    engine.add_strategy(strategy)
    return engine, strategy


class TestEndToEndOnBar:
    """Walk a real engine to confirm the gate is wired correctly."""

    def test_default_strategy_starts_with_zero_signal(self) -> None:
        """Pre-run, ``_last_signal`` is 0 and bootstrap is off by default."""
        _, strat = _build_engine_and_strategy()
        assert strat._last_signal == 0
        assert strat._bootstrap_pending is False

    def test_bootstrap_flag_propagates(self) -> None:
        _, strat = _build_engine_and_strategy(bootstrap_on_deploy=True)
        assert strat._bootstrap_pending is True

    def test_run_produces_finite_trades_not_one_per_bar(self) -> None:
        """The pre-fix bug: every bar where signal=true could produce a trade.
        Post-fix: trades only fire on signal transitions.  With a
        synthetic series containing 2 obvious MA crosses, expect a small
        finite number of positions, *not* one per bar.
        """
        engine, strat = _build_engine_and_strategy(fast_period=3, slow_period=5)
        engine.run()
        positions = engine.cache.position_snapshots() + engine.cache.positions()
        n_closed = sum(1 for p in positions if p.is_closed)
        # 120 bars total; the right answer is "a few" (one entry per
        # cross), not "many" (one per bar).  Pin upper bound at ~20%.
        assert n_closed <= 25, (
            f"Got {n_closed} closed positions on 120 bars — "
            "cross-gate isn't suppressing same-signal re-entries."
        )


class TestSignalEventEmission:
    """``on_bar`` must publish a SignalEvent every initialized bar.

    Phase 2.5 verification needs the full per-bar gate stream in PG,
    not just acted bars.  This pins both the emit-on-every-bar contract
    and the ``acted`` flag matching the cross-gate decision.
    """

    def test_emits_one_signal_event_per_bar_after_warmup(self) -> None:
        from src.core.signal_event import TOPIC_SIGNAL_MA_CROSS, SignalEvent

        engine, _strat = _build_engine_and_strategy(fast_period=3, slow_period=5)
        captured: list[SignalEvent] = []

        # Subscribe BEFORE engine.run() so the cache is primed for this
        # concrete topic; the msgbus cache quirk that bites AccountAliveMonitor
        # would bite us here too.
        engine.kernel.msgbus.subscribe(
            topic=TOPIC_SIGNAL_MA_CROSS,
            handler=lambda e: captured.append(e) if isinstance(e, SignalEvent) else None,
        )

        engine.run()

        # 120 bars total, slow=5 so first ~5 are warmup-only (no emit).
        # Expect roughly one emit per bar after warmup, not zero and not
        # one-per-position. Lower bound is generous to absorb the synthetic
        # series's single-price-bar guard.
        assert len(captured) >= 100, (
            f"Got {len(captured)} SignalEvents on 120 bars — "
            "expected ~one per bar after warmup."
        )

        # Every captured event has a valid signal direction.
        for ev in captured:
            assert ev.signal in (1, -1), f"unexpected signal value: {ev.signal}"
            assert isinstance(ev.acted, bool)
            assert isinstance(ev.bootstrap, bool)

    def test_acted_flag_aligns_with_signal_transition(self) -> None:
        """``acted=True`` rows should be where the signed signal actually changed."""
        from src.core.signal_event import TOPIC_SIGNAL_MA_CROSS, SignalEvent

        engine, _strat = _build_engine_and_strategy(fast_period=3, slow_period=5)
        captured: list[SignalEvent] = []
        engine.kernel.msgbus.subscribe(
            topic=TOPIC_SIGNAL_MA_CROSS,
            handler=lambda e: captured.append(e) if isinstance(e, SignalEvent) else None,
        )
        engine.run()

        # Walk the captured stream.  ``acted=True`` should be exactly
        # the bars where the signal differed from the previous emitted
        # signal (excluding the very first emit — which counts as a
        # transition from the initial 0 state).
        prev_signal = 0
        acted_count = 0
        transition_count = 0
        for ev in captured:
            if ev.acted:
                acted_count += 1
            if ev.signal != prev_signal:
                transition_count += 1
            prev_signal = ev.signal
        assert acted_count == transition_count, (
            f"acted_count={acted_count} != transition_count={transition_count} — "
            "acted flag doesn't match signal transitions."
        )

    def test_reset_clears_signal_state(self) -> None:
        """After on_reset, gate is back to (last_signal=0, bootstrap=False)."""
        engine, strat = _build_engine_and_strategy(bootstrap_on_deploy=False)
        engine.run()
        # After a run, last_signal should be set to whatever the last
        # cross was (not 0).
        assert strat._last_signal != 0
        engine.reset()
        assert strat._last_signal == 0
        assert strat._bootstrap_pending is False

    def test_reset_restores_bootstrap_when_configured(self) -> None:
        engine, strat = _build_engine_and_strategy(bootstrap_on_deploy=True)
        engine.run()
        # After run, bootstrap was consumed.
        assert strat._bootstrap_pending is False
        engine.reset()
        assert strat._bootstrap_pending is True
