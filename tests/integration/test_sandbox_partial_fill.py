"""Reproduce NT 1.227.0's `SandboxExecutionClient` MARKET-order partial-fill bug.

Background
----------

In the sandbox, MARKET orders that exceed the L1 book's available size
at top-of-book partially fill and then become zombies — status
``PARTIALLY_FILLED`` with ``leaves_qty > 0`` and no further fills. The
order accumulates in the cache's ``orders_open`` index indefinitely.

Symptom evidence: ``reports/incidents/2026-05-30_sandbox_partial_fill/``
(an actual incident on a 6-day live trader run).

Root cause (verified in ``.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx``):

1. ``SandboxExecutionClient`` hard-codes ``FillModel()`` (default — no
   ``get_orderbook_for_fill_simulation`` override). See
   ``nautilus_trader/adapters/sandbox/execution.py:122``.
2. With ``book_type=L1_MBP`` (sandbox default), the matching engine
   builds the book from bars by decomposing each bar into 4 synthetic
   trade ticks (open, high, low, close). Each tick's available size is
   ``bar.volume / 4`` via ``compute_bar_quarter_sizes``
   (``nautilus_trader/model/data.pxd:226``). That's the maximum size
   the L1 book holds at any instant.
3. A MARKET order asking for more than that ``quarter`` size partial-
   fills against the L1 level. ``apply_fills`` in
   ``engine.pyx:7299-7336`` has an "exhausted book volume" fallback
   that slip-fills the residual at +1 tick — but it's gated on
   ``order.is_open_c()``. ``is_open_c()`` returns True only for
   ``ACCEPTED / TRIGGERED / PENDING_CANCEL / PENDING_UPDATE /
   PARTIALLY_FILLED`` (``orders/base.pyx:428``). MARKET orders skip
   ``ACCEPTED`` (with default ``use_market_order_acks=False``).
4. After the first partial fill, the order *should* be
   ``PARTIALLY_FILLED``. But ``OrderFilled`` events go through
   ``msgbus.send("ExecEngine.process", event)``. In live/sandbox the
   handler is ``LiveExecutionEngine.process`` which **enqueues** the
   event on an asyncio queue (``live/execution_engine.py:482``) — it
   does NOT update order state synchronously. So when ``apply_fills``
   reaches the slip-fill block, the order is still in ``SUBMITTED``
   state. ``is_open_c()`` → False → slip-fill block skipped → order
   left ``leaves_qty > 0`` forever.

In a pure ``BacktestEngine``, ``ExecutionEngine.process`` is sync
(``execution/engine.pyx:892``) so the bug does not manifest.

This test reproduces the bug in a ``BacktestEngine`` by wrapping the
``ExecEngine.process`` endpoint to defer event handling until after
``apply_fills`` returns — replicating the live async behavior. It then
demonstrates the fix: use a custom ``FillModel`` that provides
unlimited liquidity at the best price (NT's built-in
``BestPriceFillModel``).

The test passes when:

* The default-``FillModel`` path leaves the order zombie (asserts the
  bug is present).
* The ``BestPriceFillModel`` path fills the order in one event (asserts
  the workaround is effective).

Re-run after any NT upgrade. If the default ``FillModel`` path starts
filling fully, NT has fixed the bug upstream — remove the workaround.

See ``docs/SANDBOX_PARTIAL_FILL_AUDIT.md`` for the full audit.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import BestPriceFillModel, FillModel
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

if TYPE_CHECKING:
    from nautilus_trader.model.orders import MarketOrder


# ── Synthetic bars with TINY volume ───────────────────────────────────────────
#
# The bug surfaces when the L1 book has less size than the MARKET order
# requests. ``compute_bar_quarter_sizes`` makes per-tick size
# ``bar.volume / 4``. With bar.volume = 0.2 and an order of 1.0 ETH, the
# close-tick L1 level holds at most ~0.05 ETH — exactly the pathology
# the live trader saw (fill ratios 0.056 / 0.078 etc.).
#
# 30 bars is enough to warm up the matching engine and submit a single
# MARKET order.


_BAR_VOLUME = Decimal("0.2")        # tiny → close_size ≈ 0.05
_ORDER_QTY = Decimal("1.0")          # >> 0.05, will partial-fill


def _make_bars(instrument: Any, bar_type: BarType, n: int = 30) -> list[Bar]:
    """Synthetic 1m bars, flat price, tiny volume."""
    px_prec = int(instrument.price_precision)
    qty_prec = int(instrument.size_precision)
    px_fmt = f"{{:.{px_prec}f}}"
    qty_fmt = f"{{:.{qty_prec}f}}"

    base_ns = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1e9)
    one_min_ns = 60 * 10**9

    bars: list[Bar] = []
    for i in range(n):
        ts = base_ns + i * one_min_ns
        # Price walks up by 1 each bar (3000 → 3029) to keep the matching
        # engine seeing trades; the partial-fill bug is volume-driven,
        # not price-driven.
        px = 3000 + i
        bars.append(
            Bar(
                bar_type,
                Price.from_str(px_fmt.format(px - Decimal("0.5"))),  # open
                Price.from_str(px_fmt.format(px + Decimal("1.0"))),  # high
                Price.from_str(px_fmt.format(px - Decimal("1.0"))),  # low
                Price.from_str(px_fmt.format(px)),                    # close
                Quantity.from_str(qty_fmt.format(_BAR_VOLUME)),
                ts,
                ts,
            ),
        )
    return bars


# ── Single-shot MARKET-order strategy ─────────────────────────────────────────


# NT's StrategyConfig is a pyx class; mypy can't see frozen/kw_only kwargs.
class _OneShotMarketConfig(StrategyConfig, frozen=True, kw_only=True):  # type: ignore[misc, call-arg]
    instrument_id: str
    bar_type: str
    fire_after_bars: int = 5


class _OneShotMarketStrategy(Strategy):  # type: ignore[misc]
    """Submit ONE MARKET BUY of ``_ORDER_QTY`` after a warmup, then idle.

    The strategy stashes a callable hook (``_pre_submit_hook``) that
    fires immediately before each ``submit_order`` call. The test
    installs a deferring shim via this hook so the bug can be
    reproduced. (We can't monkey-patch ``Strategy.submit_order`` —
    Strategy is a Cython class with cdef dispatch that bypasses Python
    attribute lookup.)
    """

    def __init__(self, config: _OneShotMarketConfig) -> None:
        super().__init__(config)
        self._bar_count = 0
        self._fired = False
        self.submitted_order: MarketOrder | None = None
        self._pre_submit_hook: Any = None  # set by test
        self._post_submit_hook: Any = None  # set by test

    def on_start(self) -> None:
        from nautilus_trader.model.identifiers import InstrumentId

        self._instrument_id = InstrumentId.from_str(self.config.instrument_id)
        self._bar_type = BarType.from_str(self.config.bar_type)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar) -> None:  # noqa: ARG002
        self._bar_count += 1
        if self._fired or self._bar_count < self.config.fire_after_bars:
            return
        instrument = self.cache.instrument(self._instrument_id)
        if instrument is None:
            return
        order = self.order_factory.market(
            instrument_id=self._instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(
                f"{{:.{instrument.size_precision}f}}".format(_ORDER_QTY)
            ),
            time_in_force=TimeInForce.GTC,
        )
        self.submitted_order = order
        if self._pre_submit_hook is not None:
            self._pre_submit_hook()
        try:
            self.submit_order(order)
        finally:
            if self._post_submit_hook is not None:
                self._post_submit_hook()
        self._fired = True


# ── Async msgbus shim ─────────────────────────────────────────────────────────
#
# In sandbox/live, ``msgbus.send("ExecEngine.process", event)`` enqueues
# the event on an asyncio queue (LiveExecutionEngine). In a backtest,
# the registered handler IS the sync ExecutionEngine.process which
# updates order state inline. To repro the sandbox bug we need the
# matching engine to see the order STILL in SUBMITTED state at the time
# it checks ``order.is_open_c()`` in the L1 exhausted-book slip-fill
# block (``apply_fills`` in ``engine.pyx:7300``).
#
# Strategy: wrap the registered ``ExecEngine.process`` handler to BUFFER
# events emitted while ``apply_fills`` is on the call stack. After
# ``apply_fills`` returns, drain the buffer to the real handler. This
# faithfully simulates the live async behavior for the specific code
# path that matters.


class _DeferringExecHandler:
    """Buffer OrderEvents while a `_capturing` flag is set, then drain.

    Mirrors LiveExecutionEngine's enqueue-then-async-process behavior
    for the single ``apply_fills`` call we care about.
    """

    def __init__(self, real_handler: Any) -> None:
        self._real = real_handler
        self._buffer: deque[Any] = deque()
        self.capturing = False
        self.deferred_count = 0
        self.passthrough_count = 0
        self.drained_count = 0

    def __call__(self, msg: Any) -> None:
        if self.capturing:
            self._buffer.append(msg)
            self.deferred_count += 1
        else:
            self._real(msg)
            self.passthrough_count += 1

    def drain(self) -> int:
        n = 0
        while self._buffer:
            self._real(self._buffer.popleft())
            n += 1
        self.drained_count += n
        return n


def _run_scenario(fill_model: FillModel | None) -> tuple[BacktestEngine, _OneShotMarketStrategy]:
    """Run a tiny backtest with the async-msgbus shim installed.

    Returns the engine + strategy after run completes. With the default
    ``FillModel()`` the submitted order should be partially filled and
    still in ``orders_open``; with ``BestPriceFillModel`` it should be
    fully filled.
    """
    from nautilus_trader.config import LoggingConfig

    instrument = TestInstrumentProvider.ethusdt_perp_binance()
    venue = instrument.venue
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    bars = _make_bars(instrument, bar_type, n=30)

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="ERROR"),
        ),
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(100_000, instrument.quote_currency)],
        default_leverage=Decimal(1),
        fill_model=fill_model,
        # IMPORTANT: matches SandboxExecutionClient behaviour
        # (sandbox hard-codes `use_message_queue=False` in
        # adapters/sandbox/execution.py:135). Without this the
        # BacktestEngine queues SubmitOrder for the next iterate and
        # the deferring shim has nothing to capture.
        use_message_queue=False,
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)

    strategy = _OneShotMarketStrategy(
        _OneShotMarketConfig(
            instrument_id=str(instrument.id),
            bar_type=str(bar_type),
            fire_after_bars=5,
        ),
    )
    engine.add_strategy(strategy)

    # Install the async-msgbus shim BEFORE engine.run() so the first
    # tick emission goes through the wrapped handler.
    #
    # The matching engine emits OrderFilled events synchronously from
    # within apply_fills(). To replicate the sandbox's async enqueue
    # behaviour we wrap the strategy's submit_order call: ARM the
    # deferrer right before submit_order (so all fill events emitted
    # while apply_fills is on the stack get BUFFERED), DISARM and DRAIN
    # immediately after submit_order returns.
    #
    # This faithfully mimics LiveExecutionEngine.process which calls
    # _evt_enqueuer.enqueue(event) — non-blocking, the event is handled
    # later by an asyncio task. The key point: order state has NOT yet
    # transitioned when apply_fills checks order.is_open_c().
    msgbus = engine.kernel.msgbus
    real_handler = engine.kernel.exec_engine.process
    shim = _DeferringExecHandler(real_handler)
    msgbus.deregister(endpoint="ExecEngine.process", handler=real_handler)
    msgbus.register(endpoint="ExecEngine.process", handler=shim)

    def _pre_submit() -> None:
        shim.capturing = True

    def _post_submit() -> None:
        shim.capturing = False
        shim.drain()

    strategy._pre_submit_hook = _pre_submit
    strategy._post_submit_hook = _post_submit

    engine.run()
    # Stash the shim on the strategy (BacktestEngine is a Cython class
    # without a __dict__).
    strategy._test_shim = shim
    return engine, strategy


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestPartialFillRepro:
    """Confirm the bug exists on the default FillModel.

    These tests are the executable form of the audit doc. If they start
    failing on a future NT version, NT has likely fixed the bug — see
    docs/SANDBOX_PARTIAL_FILL_AUDIT.md for the re-verification recipe.
    """

    def test_default_fillmodel_leaves_order_partially_filled_zombie(self) -> None:
        """Default FillModel + tiny L1 book + async msgbus → zombie order.

        Asserts:

        * The order is NOT fully filled (would be CLOSED status).
        * The filled quantity is < requested quantity.
        * The order remains in ``orders_open`` (matches the live symptom).
        """
        engine, strategy = _run_scenario(fill_model=None)  # NT default
        order = strategy.submitted_order
        assert order is not None, "Strategy never submitted the test order"

        # The bug: order is partially filled and zombie.
        assert order.status == OrderStatus.PARTIALLY_FILLED, (
            f"Expected PARTIALLY_FILLED zombie, got {order.status} — "
            "NT may have fixed the bug; revisit the audit doc."
        )
        assert order.filled_qty < order.quantity, (
            f"filled_qty={order.filled_qty} should be < quantity={order.quantity}"
        )
        # The order is in the cache's orders_open index (mirrors the
        # 8 stuck orders in the 2026-05-30 incident snapshot).
        open_orders = engine.cache.orders_open(instrument_id=order.instrument_id)
        assert order.client_order_id in {o.client_order_id for o in open_orders}, (
            "Order is not in orders_open — but it should be, as a zombie."
        )

    def test_bestpricefillmodel_fills_in_one_event(self) -> None:
        """The proposed workaround: BestPriceFillModel → full fill.

        ``BestPriceFillModel`` overrides ``get_orderbook_for_fill_simulation``
        to provide a synthetic order book with 1_000_000 units at the
        best bid/ask — so any reasonable order fills in one event,
        independent of the L1 book size derived from bar volume.

        Asserts the order is FILLED with filled_qty == quantity, and
        is NOT in orders_open.
        """
        engine, strategy = _run_scenario(fill_model=BestPriceFillModel())
        order = strategy.submitted_order
        assert order is not None, "Strategy never submitted the test order"

        assert order.status == OrderStatus.FILLED, (
            f"BestPriceFillModel should have filled the order; got {order.status}"
        )
        assert order.filled_qty == order.quantity, (
            f"filled_qty={order.filled_qty} != quantity={order.quantity}"
        )
        open_orders = engine.cache.orders_open(instrument_id=order.instrument_id)
        assert order.client_order_id not in {o.client_order_id for o in open_orders}, (
            "Order should not be in orders_open after a full fill."
        )


# ── Standalone runner (skip pytest infrastructure for quick iteration) ────────


if __name__ == "__main__":
    # Run as a script to eyeball the symptom:
    #   python tests/integration/test_sandbox_partial_fill.py
    print("Running default FillModel scenario (expect partial-fill zombie)...")
    eng, strat = _run_scenario(fill_model=None)
    o = strat.submitted_order
    assert o is not None
    print(f"  status={o.status} filled={o.filled_qty}/{o.quantity}")
    shim = strat._test_shim
    print(f"  shim: deferred={shim.deferred_count} passthrough={shim.passthrough_count} drained={shim.drained_count}")

    print("Running BestPriceFillModel scenario (expect full fill)...")
    eng2, strat2 = _run_scenario(fill_model=BestPriceFillModel())
    o2 = strat2.submitted_order
    assert o2 is not None
    print(f"  status={o2.status} filled={o2.filled_qty}/{o2.quantity}")
    shim2 = strat2._test_shim
    print(f"  shim: deferred={shim2.deferred_count} passthrough={shim2.passthrough_count} drained={shim2.drained_count}")
