"""Restart-state persistence tests for ``MACross``.

Pin the contract that ``MACross.on_save`` + ``MACross.on_load`` round-trip
the cross-gate state across a simulated restart, so a fresh container
doesn't re-act on a stale signal or re-fire the bootstrap.

Two layers:

* ``TestMACrossSaveLoad`` — pure save/load round-trip on a real ``MACross``
  instance.  Touches the strategy directly; no engine, no Redis.
* ``TestMACrossRestartBehaviour`` — full end-to-end: run a backtest to
  populate ``_last_signal``, snapshot the state via ``on_save``, build a
  fresh strategy, ``on_load`` the state, and assert the gate behaves as
  if the prior run continued (no spurious entry on the first bar after
  the snapshot when the signal matches what was saved).

The mixin keys (``protective_*``, ``liq_*``) round-trip too — they're
layered into the same dict via cooperative ``super().on_save()``.  The
mixin tests pin them individually; here we just confirm the merged dict
preserves them through the MACross level.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import ClientOrderId, PositionId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from src.backtesting import make_engine
from src.backtesting.engine import resolve_strategy_liquidation_config
from src.core import LiquidationConfig, bar_type_str, get_venue_config
from src.strategies.ma_cross import MACross, MACrossConfig

if TYPE_CHECKING:
    from nautilus_trader.backtest.engine import BacktestEngine


def _build_engine_and_strategy(
    *,
    fast_period: int = 3,
    slow_period: int = 5,
    bootstrap_on_deploy: bool = False,
    stop_pct: float | None = None,
) -> tuple[BacktestEngine, MACross]:
    """Mirror the helper in ``test_ma_cross_cross_gate.py``.

    Kept local rather than imported to keep these two test files
    independent — a future refactor of the cross-gate harness shouldn't
    cascade into this file's assertions.
    """
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    venue = instrument.venue
    bar_type = BarType.from_str(bar_type_str(str(instrument.id), "1d"))

    px_prec = int(instrument.price_precision)
    qty_prec = int(instrument.size_precision)
    px_fmt = f"{{:.{px_prec}f}}"
    qty_fmt = f"{{:.{qty_prec}f}}"

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
        stop_pct=stop_pct,
    ))
    engine.add_strategy(strategy)
    return engine, strategy


# ── Direct save/load round-trip ────────────────────────────────────────────


class TestMACrossSaveLoad:
    """``MACross.on_save`` → ``MACross.on_load`` round-trip on a real
    instance.  Does not run the engine — pokes the strategy's state
    directly and asserts the on-the-wire dict carries it back.

    NT's ``Strategy.save()`` requires the strategy to be registered with
    a trader before it can be called; ``Strategy.on_save()`` itself does
    not.  These tests exercise ``on_save`` / ``on_load`` directly to keep
    the test free of NT bootstrap plumbing.
    """

    def test_round_trip_long_signal(self) -> None:
        _, strat = _build_engine_and_strategy()
        strat._last_signal = 1
        strat._bootstrap_pending = False

        state = strat.on_save()
        # State is a flat ``dict[str, bytes]`` — the NT contract.
        assert isinstance(state, dict)
        for key, value in state.items():
            assert isinstance(key, str), f"key {key!r} must be str"
            assert isinstance(value, bytes), f"value at {key!r} must be bytes"

        # Construct a fresh strategy and load the state — simulates restart.
        _, strat2 = _build_engine_and_strategy()
        assert strat2._last_signal == 0  # default before load
        strat2.on_load(state)
        assert strat2._last_signal == 1
        assert strat2._bootstrap_pending is False

    def test_round_trip_short_signal(self) -> None:
        _, strat = _build_engine_and_strategy()
        strat._last_signal = -1
        strat._bootstrap_pending = False

        state = strat.on_save()
        _, strat2 = _build_engine_and_strategy()
        strat2.on_load(state)
        assert strat2._last_signal == -1
        assert strat2._bootstrap_pending is False

    def test_round_trip_clears_bootstrap_pending(self) -> None:
        """Operator deploys with ``bootstrap_on_deploy=True``, first bar
        fires the synthetic cross, ``_bootstrap_pending`` flips to False.
        On restart, the loaded state must preserve ``False`` so the
        bootstrap doesn't fire a SECOND time."""
        _, strat = _build_engine_and_strategy(bootstrap_on_deploy=True)
        # Simulate the bootstrap having fired.
        strat._last_signal = 1
        strat._bootstrap_pending = False

        state = strat.on_save()

        # Restart with same config — ``__init__`` sets _bootstrap_pending=True
        # again from config, but on_load must clobber it back to False.
        _, strat2 = _build_engine_and_strategy(bootstrap_on_deploy=True)
        assert strat2._bootstrap_pending is True  # pre-load default from config
        strat2.on_load(state)
        assert strat2._bootstrap_pending is False  # post-load: persisted value

    def test_round_trip_preserves_bootstrap_pending_true(self) -> None:
        """If the container died BEFORE the first bar (bootstrap never fired),
        the state still has ``_bootstrap_pending=True`` — restore that so
        the bootstrap can still fire on the first post-restart bar."""
        _, strat = _build_engine_and_strategy(bootstrap_on_deploy=True)
        # Initial state — no bars yet.
        assert strat._last_signal == 0
        assert strat._bootstrap_pending is True

        state = strat.on_save()
        _, strat2 = _build_engine_and_strategy(bootstrap_on_deploy=False)
        # New instance with bootstrap_on_deploy=False would default
        # _bootstrap_pending=False — load must clobber back to True.
        assert strat2._bootstrap_pending is False
        strat2.on_load(state)
        assert strat2._bootstrap_pending is True
        assert strat2._last_signal == 0

    def test_round_trip_with_mixin_state(self) -> None:
        """Mixin keys layer onto the same dict via cooperative super().
        Verify the merged dict survives a round-trip without dropping
        either the cross-gate keys or the mixin keys.
        """
        _, strat = _build_engine_and_strategy(stop_pct=0.05)
        strat._last_signal = -1
        strat._bootstrap_pending = False
        strat._protective_order_ids = {
            PositionId("P-001"): ClientOrderId("O-001"),
        }
        strat._protective_count = 1
        strat._liq_order_ids = {
            PositionId("P-002"): ClientOrderId("LIQ-002"),
        }
        strat._liq_count = 3

        state = strat.on_save()
        # The merged dict carries all three groups' keys.
        assert "cross_gate_last_signal" in state
        assert "cross_gate_bootstrap_pending" in state
        assert "protective_order_ids" in state
        assert "protective_count" in state
        assert "liq_order_ids" in state
        assert "liq_count" in state

        _, strat2 = _build_engine_and_strategy(stop_pct=0.05)
        strat2.on_load(state)
        # Cross-gate keys restored.
        assert strat2._last_signal == -1
        assert strat2._bootstrap_pending is False
        # Mixin keys restored.
        assert {p.value: o.value for p, o in strat2._protective_order_ids.items()} == {
            "P-001": "O-001",
        }
        assert strat2._protective_count == 1
        assert {p.value: o.value for p, o in strat2._liq_order_ids.items()} == {
            "P-002": "LIQ-002",
        }
        assert strat2._liq_count == 3

    def test_load_with_empty_state_keeps_defaults(self) -> None:
        """Empty state dict (NT skips on_load entirely in this case, but
        defensive check anyway) leaves the strategy in its post-init state."""
        _, strat = _build_engine_and_strategy()
        strat.on_load({})
        assert strat._last_signal == 0
        assert strat._bootstrap_pending is False

    def test_load_with_unknown_keys_is_ignored(self) -> None:
        """Forward-compat: an upgraded build might write keys we don't
        know about.  on_load must not crash on them."""
        _, strat = _build_engine_and_strategy()
        state = {
            "cross_gate_last_signal": b"1",
            "future_v3_feature": b"some_blob",
        }
        strat.on_load(state)
        assert strat._last_signal == 1

    def test_load_with_malformed_last_signal_keeps_default(self) -> None:
        """Bad data must NOT stop the trader — log and continue."""
        _, strat = _build_engine_and_strategy()
        # default after __init__
        assert strat._last_signal == 0
        strat.on_load({"cross_gate_last_signal": b"not_an_int"})
        assert strat._last_signal == 0  # unchanged


# ── End-to-end restart behaviour ───────────────────────────────────────────


class TestMACrossRestartBehaviour:
    """Simulate the operator's "restart with reconciliation" scenario.

    Step 1: Run the strategy to populate ``_last_signal`` from a real cross.
    Step 2: Snapshot via ``on_save``.
    Step 3: Build a fresh strategy + engine, ``on_load`` the snapshot.
    Step 4: Assert the gate now treats the loaded signal as the
            most-recent acted signal — i.e., a same-direction bar after
            load does NOT count as a fresh cross.
    """

    def test_loaded_state_suppresses_same_direction_action(self) -> None:
        """After load, a same-direction bar must NOT be acted upon.

        This is the critical safety property: without load, the fresh
        strategy has ``_last_signal=0`` and would treat the next signal
        as a brand-new cross — potentially closing the just-reconciled
        position on the next opposite-direction bar even though the
        prior run was already long.
        """
        # Run engine to populate cross-gate state from a real cross.
        engine, strat = _build_engine_and_strategy(fast_period=3, slow_period=5)
        engine.run()
        # After a full run, _last_signal is whatever the last cross was.
        saved_signal = strat._last_signal
        assert saved_signal != 0
        state = strat.on_save()
        engine.reset()  # release the strategy

        # Build a fresh strategy that mimics container restart.
        _, fresh = _build_engine_and_strategy(fast_period=3, slow_period=5)
        assert fresh._last_signal == 0  # post-init default
        fresh.on_load(state)
        assert fresh._last_signal == saved_signal  # restored

        # The gate now treats a same-direction signal as no-action.
        new_signal, should_act = MACross._cross_gate_decision(
            fast_value=101.0 if saved_signal > 0 else 99.0,
            slow_value=100.0,
            last_signal=fresh._last_signal,
            bootstrap_pending=fresh._bootstrap_pending,
        )
        assert new_signal == saved_signal
        assert should_act is False, (
            f"Loaded state with _last_signal={saved_signal} should suppress "
            f"action on a same-direction bar — but gate returned should_act=True. "
            f"This means the persisted state isn't being used by the gate."
        )

    def test_loaded_state_still_fires_on_opposite_signal(self) -> None:
        """Symmetric check: a flip after load must STILL trigger action.

        The persistence shouldn't disable the gate — it should just shift
        the gate's reference point to the loaded signal.
        """
        engine, strat = _build_engine_and_strategy(fast_period=3, slow_period=5)
        engine.run()
        saved_signal = strat._last_signal
        state = strat.on_save()
        engine.reset()

        _, fresh = _build_engine_and_strategy(fast_period=3, slow_period=5)
        fresh.on_load(state)

        # Synthesize the OPPOSITE-direction signal — the gate should fire.
        flipped_fast = 99.0 if saved_signal > 0 else 101.0
        new_signal, should_act = MACross._cross_gate_decision(
            fast_value=flipped_fast,
            slow_value=100.0,
            last_signal=fresh._last_signal,
            bootstrap_pending=fresh._bootstrap_pending,
        )
        assert new_signal != saved_signal
        assert should_act is True

    def test_no_state_after_reset(self) -> None:
        """``on_reset`` clears state; subsequent ``on_save`` produces a
        baseline dict with ``last_signal=0`` and ``bootstrap_pending``
        matching the original config flag.

        This is the sweep-iteration boundary — reset → save should NOT
        leak state from the prior iteration into a saved snapshot.
        """
        engine, strat = _build_engine_and_strategy(
            fast_period=3, slow_period=5, bootstrap_on_deploy=True,
        )
        engine.run()
        engine.reset()
        state = strat.on_save()
        assert state["cross_gate_last_signal"] == b"0"
        assert state["cross_gate_bootstrap_pending"] == b"1"  # restored from config
