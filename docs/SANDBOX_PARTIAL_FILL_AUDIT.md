# Sandbox Partial-Fill Audit (NT 1.227.0)

**Scope.** Why NT 1.227.0's `SandboxExecutionClient` partial-fills
MARKET orders against an L1 book and leaves them stuck in
`orders_open` indefinitely. What changes (project-side, NT-side, or
ops-side) eliminate the bug. How to re-verify after future NT
upgrades.

**Status.** Phase 2.5 blocker — first manifested on a 6-day live
trader run (`reports/incidents/2026-05-30_sandbox_partial_fill/`)
that destroyed -16.66% of the paper account. Root cause confirmed
in NT source. **Project-side workaround applied:**
[`src/adapters/patched_sandbox.py`](../src/adapters/patched_sandbox.py)
subclasses `SandboxExecutionClient` and installs `BestPriceFillModel`;
`scripts/run_sandbox.py` registers the patched factory. Re-verify
after every NT upgrade — see §7.

## 1. Symptom

`SandboxExecutionClient` partial-fills MARKET orders at small
fractions of the requested quantity. Observed fill ratios in the
6-day live trader run (from `reports/incidents/.../pg/order_fills.csv`):

| Position # | Requested qty | First fill | Fill ratio |
|---|---|---|---|
| 1-6 | ~1.0 ETH | full | 1.000 |
| 7 (first failure) | ~1.0 ETH | 0.2887 | 0.289 |
| later positions | ~1.0 ETH | 0.0560, 0.0779, 0.3845, 0.3975, 0.9895 | 0.06-0.99 |

Partial-filled orders stay in NT's `orders_open` index forever — no
`OrderFilled` completion event, no `OrderCanceled`. By incident
capture time, 8 zombie orders had accumulated. Effective trading-
position size decayed from ~1.0 ETH (intended $2000 notional) to
~0.08 ETH on the residual position.

Full evidence: `reports/incidents/2026-05-30_sandbox_partial_fill/`
(gitignored — PG dumps, Redis snapshot, container log).

## 2. Root cause

The bug is a race between the matching engine's synchronous fill
loop and the **live** execution-engine's asynchronous event queue.
It does not surface in pure `BacktestEngine` runs because there
the same handler is fully synchronous.

### 2.1 L1 book size is capped per synthetic tick

`SandboxExecutionClient` defaults to `book_type="L1_MBP"` (top-of-
book only). The matching engine populates this book by decomposing
each incoming live `Bar` into 4 synthetic ticks — one each for
open, high, low, close — via
[`engine.pyx::_process_trade_ticks_from_bar`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)
(line 4732).

Per-tick size comes from
[`data.pxd::compute_bar_quarter_sizes`](../.venv/Lib/site-packages/nautilus_trader/model/data.pxd)
(line 226):

```cython
cdef inline (QuantityRaw, QuantityRaw) compute_bar_quarter_sizes(
    QuantityRaw volume_raw,
    QuantityRaw min_size_raw,
):
    cdef QuantityRaw quarter_raw = volume_raw // 4
    # ... rounding to size_increment ...
    cdef QuantityRaw three_quarters = quarter_raw * 3
    if three_quarters >= volume_raw:
        close_raw = min_size_raw
    else:
        close_raw = volume_raw - three_quarters
    return (quarter_raw, close_raw)
```

So the L1 book level at any instant holds at most `bar.volume / 4`
units. For ETH 15m HL-sandbox bars with low simulated volume, the
close-tick level is on the order of 0.05-0.40 ETH — exactly the
ratios observed in the incident.

### 2.2 The L1 slip-fill safety net exists but is gated on order state

`apply_fills` in
[`engine.pyx:7299-7336`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)
has an explicit "exhausted book volume" fallback that slip-fills the
residual of a MARKET order at one tick worse than the last fill:

```cython
# Check MARKET order on exhausted book volume
if (
    order.is_open_c()                           # ← THIS GATE
    and self.book_type == BookType.L1_MBP
    and (
        order.order_type == OrderType.MARKET
        or order.order_type == OrderType.MARKET_IF_TOUCHED
        or order.order_type == OrderType.STOP_MARKET
        or order.order_type == OrderType.TRAILING_STOP_MARKET
    )
):
    # ... slip by one tick ...
    self.fill_order(
        order=order,
        last_px=fill_px,
        last_qty=order.leaves_qty,             # fill the rest
        ...
    )
```

`is_open_c()` (in
[`orders/base.pyx:428`](../.venv/Lib/site-packages/nautilus_trader/model/orders/base.pyx))
returns True only for `ACCEPTED | TRIGGERED | PENDING_CANCEL |
PENDING_UPDATE | PARTIALLY_FILLED`. **Not for `SUBMITTED`.**

MARKET orders bypass the `ACCEPTED` state in the sandbox: the
`use_market_order_acks` config flag defaults to False (`engine.pyx`
line 526), so `_process_market_order` calls `fill_market_order`
directly without emitting an `OrderAccepted` first
(`engine.pyx:5232`).

So before the first fill the order is in `SUBMITTED`. The matching
engine emits its first `OrderFilled` event for the partial fill. If
that event were processed synchronously, the order would transition
to `PARTIALLY_FILLED` (which IS in `is_open_c()`), the slip-fill
block would fire, and the order would complete.

### 2.3 Live execution engine processes order events asynchronously

`_generate_order_filled`
([`engine.pyx:8345`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx))
sends the fill event via the message bus:

```cython
self.msgbus.send(endpoint="ExecEngine.process", msg=event)
```

The handler registered at `ExecEngine.process` is the execution
engine's `process()` method. In a pure `BacktestEngine`, this is
[`execution/engine.pyx::ExecutionEngine.process`](../.venv/Lib/site-packages/nautilus_trader/execution/engine.pyx)
at line 880, which is fully synchronous — it calls `_handle_event`
immediately, which calls `order.apply(event)`, which calls
`order._filled(event)`, which transitions the FSM:

```cython
cdef void _filled(self, OrderFilled fill):
    if self.filled_qty._mem.raw + fill.last_qty._mem.raw < self.quantity._mem.raw:
        self._fsm.trigger(OrderStatus.PARTIALLY_FILLED)
    else:
        self._fsm.trigger(OrderStatus.FILLED)
```

So by the time `apply_fills` reaches the `is_open_c()` check, the
state has already moved to `PARTIALLY_FILLED`. The slip-fill block
fires. Backtests work fine.

In a sandbox / live `TradingNode`, the handler is
[`live/execution_engine.py::LiveExecutionEngine.process`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py)
at line 467:

```python
def process(self, event: OrderEvent) -> None:
    self._record_local_activity(event)
    self._evt_enqueuer.enqueue(event)
```

This is non-blocking — the event is queued on an asyncio queue
(`_evt_queue`) that's drained by an independent asyncio task
(`_run_evt_queue`, line 511). When `_generate_order_filled` returns
to `apply_fills` and the slip-fill block runs `order.is_open_c()`,
the order's FSM has **not yet transitioned** — it's still
`SUBMITTED`. Gate fails. Slip-fill skipped. Order is left with
`leaves_qty > 0`.

### 2.4 The MARKET order isn't tracked by the matching core

MARKET orders are taker liquidity — they fill and exit. The matching
engine only calls `_core.add_order(order)` for orders that need to
rest on the book (see `accept_order` at
[`engine.pyx:7932`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)).
MARKET orders skip `accept_order` entirely (line 5236 calls
`fill_market_order` directly). So once the async event finally
processes (state → `PARTIALLY_FILLED`), no future `iterate()` call
will revisit the order to top it up. It's a permanent zombie.

The order continues to sit in NT's cache `orders_open` index. The
strategy's next cross-gate decision generates a new opposite
MARKET order, which the netting engine resolves by capping at the
zombie's residual (`reduce_only` interaction) — producing the
"silent denial / next-bar resolve" pattern visible in the incident
log timeline (positions #7-#11).

## 3. Why this didn't surface in backtests

Two reasons:

1. `BacktestEngine.add_venue` defaults `use_message_queue=True`
   ([`engine.pyx:525`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)).
   SubmitOrder commands are placed on a deferred queue and processed
   on the next `iterate`. The matching engine's full bar-by-bar
   processing fills the order across multiple synthetic ticks before
   the slip block is even reached.
2. `BacktestEngine` uses `ExecutionEngine` (sync), not
   `LiveExecutionEngine` (async). Even if the slip block triggers,
   the state has already transitioned.

`SandboxExecutionClient` hard-codes `use_message_queue=False`
([`adapters/sandbox/execution.py:135`](../.venv/Lib/site-packages/nautilus_trader/adapters/sandbox/execution.py))
so it always processes commands immediately against the current L1
book state. Combined with the live async event queue, the bug
manifests.

## 4. Reproduction

Run [`tests/integration/test_sandbox_partial_fill.py`](../tests/integration/test_sandbox_partial_fill.py):

```bash
.venv/Scripts/python -m pytest tests/integration/test_sandbox_partial_fill.py -v
```

The test sets up a `BacktestEngine` with `use_message_queue=False`
(matching the sandbox config) and wraps the `ExecEngine.process`
msgbus endpoint with a shim that defers events during the strategy's
`submit_order` call. This faithfully simulates the live async
behaviour for the single call that matters.

Both tests pass on NT 1.227.0:

* `test_default_fillmodel_leaves_order_partially_filled_zombie` —
  default `FillModel()`, order ends up `PARTIALLY_FILLED` with
  `filled_qty=0.050/1.000`, still in `orders_open`.
* `test_bestpricefillmodel_fills_in_one_event` —
  `BestPriceFillModel()`, order ends up `FILLED`, not in
  `orders_open`.

The test can also be run standalone (no pytest) for quick inspection:

```bash
.venv/Scripts/python tests/integration/test_sandbox_partial_fill.py
```

## 5. Fix options

Three layers of fix, in priority order:

### 5.1 Project-side workaround — APPLIED

`BestPriceFillModel`
([`backtest/models/fill.pyx:170`](../.venv/Lib/site-packages/nautilus_trader/backtest/models/fill.pyx))
is a built-in NT `FillModel` subclass that overrides
`get_orderbook_for_fill_simulation` to return a synthetic book with
`1_000_000` units at the best bid and ask. Since `simulate_fills`
walks the simulated book, any reasonable order quantity fills in one
event — independent of the L1 book size derived from bar volume.

`SandboxExecutionClientConfig` does not expose a `fill_model` field
(the sandbox always uses `FillModel()` — hard-coded at
[`adapters/sandbox/execution.py:122`](../.venv/Lib/site-packages/nautilus_trader/adapters/sandbox/execution.py)).
The cleanest project-side workaround is to subclass
`SandboxExecutionClient` and swap the matching engine's fill model.

Applied in [`src/adapters/patched_sandbox.py`](../src/adapters/patched_sandbox.py)
— ~25 lines (including the matching factory). `scripts/run_sandbox.py`
registers `PatchedSandboxLiveExecClientFactory` in place of NT's
`SandboxLiveExecClientFactory`. Unit-tested in
[`tests/unit/test_patched_sandbox.py`](../tests/unit/test_patched_sandbox.py);
the bug + fix end-to-end behaviour stays pinned in
[`tests/integration/test_sandbox_partial_fill.py`](../tests/integration/test_sandbox_partial_fill.py).

**Tradeoff.** `BestPriceFillModel` is unrealistically optimistic for
backtests — it provides infinite liquidity. For paper trading on
live data, this is closer to truth than the default model (which
implicitly assumes only `bar.volume/4` of liquidity exists at top of
book at any instant). For a strategy submitting $2000 notional MARKET
orders on ETH, the real Hyperliquid book has far more than 1.0 ETH
of size at the touch — so `BestPriceFillModel` matches reality
better than the default. Slippage is still simulated through the
`prob_slippage` parameter on the base `FillModel` (subclass and
chain via `super().is_slipped()` if needed).

### 5.2 NT-side fix (upstream PR candidate)

Two reasonable upstream changes:

1. Add `OrderStatus.SUBMITTED` to `is_open_c()` for MARKET orders,
   or guard the slip-fill block on `order.filled_qty > 0` instead
   of `order.is_open_c()`. This is the minimal fix — but it changes
   semantics for any code path relying on `is_open_c()`.

2. Add a `fill_model` field to `SandboxExecutionClientConfig` and
   plumb it through to the `SimulatedExchange` constructor in
   `SandboxExecutionClient.__init__`. This is the safest fix —
   nothing existing changes, but users can opt into a different
   fill model.

Either change is a small patch. **Recommendation: file an upstream
issue with the repro test from this audit attached, and propose
fix #2** (`fill_model` config field). NT's existing built-in
`BestPriceFillModel` is the natural pair.

### 5.3 Operator-side: don't use the sandbox

Skip Stage A of Phase 2.5 (sandbox) and jump straight to Stage B
(HL testnet via `run_live.py` with `HL_TESTNET=true`). The
`HyperliquidExecClient` doesn't go through NT's simulator at all —
fills come from a real (testnet) venue with a real order book.
Loses the sandbox's "zero cost / instant turnaround" property but
sidesteps the bug entirely.

This is what the project's `PAPER_TRADING_GUIDE.md` already
recommends as Stage B; the value of Stage A is the no-real-money,
no-API-keys-needed shakedown. If the fix in 5.1 is applied, Stage A
remains useful. If not, skip Stage A.

## 6. When this matters

Any strategy that:

* Runs through `SandboxExecutionClient`, AND
* Submits MARKET orders (or other order types that decay to MARKET —
  STOP_MARKET, MARKET_IF_TOUCHED, TRAILING_STOP_MARKET), AND
* Requests a quantity larger than `bar.volume / 4` for the bar
  immediately preceding submission.

The third condition is the trigger. Bar volume varies by instrument
and timeframe; on ETH 15m HL-sandbox bars it's often < 0.5 ETH per
quarter-tick. On 1-day bars or high-volume pairs, the issue is
masked because the per-tick size is usually larger than typical
order sizes.

**Indicator the bug just bit you.** Two signatures in PG /Redis:

1. `SELECT client_order_id, status FROM ...` (or Redis
   `orders:O-...` keys) shows orders in `PARTIALLY_FILLED` state
   without a subsequent `OrderFilled` or `OrderCanceled` event.
2. `order_fills.csv` shows fill quantities that are exactly
   `bar.volume / 4` rounded down to size_increment for the bar
   right before the fill. Cross-check against the OHLCV catalog.

## 7. Re-verification recipe for future NT upgrades

> **Before evaluating a new NT release, read [`NT_UPGRADE_NOTES.md`](NT_UPGRADE_NOTES.md).**
> It documents the "Rust crate tree ≠ shipped Python wheel" trap — a
> fix landing in `crates/**/*.rs` does nothing for us until NT flips the
> v1 Cython API to the Rust core. 1.228.0 was evaluated and SKIPPED for
> exactly this reason: the matching-engine fix is in the Rust source but
> the shipped `.pyx` is unchanged and the repro test still passes.

After every NT version bump (`pip install nautilus_trader==<new>`):

1. Run `pytest tests/integration/test_sandbox_partial_fill.py -v`.

2. If `test_default_fillmodel_leaves_order_partially_filled_zombie`
   **still passes** — the bug is still present in upstream NT. Keep
   the workaround in place.

3. If that test **fails** (i.e., the order now fills fully even with
   default `FillModel`) — NT has likely fixed the bug. Verify the
   fix by:
   - Reading the new NT version's `apply_fills` in `backtest/engine.pyx`
     to see if the slip-fill block's gating changed (look for
     `is_open_c` → maybe replaced with `filled_qty > 0` or similar).
   - Checking `RELEASES.md` for mentions of "sandbox" / "L1" /
     "partial fill" / `BestPriceFillModel`.
   - Reading the new `SandboxExecutionClientConfig` for a new
     `fill_model` field.
   Then remove the workaround and update this doc.

4. If `test_bestpricefillmodel_fills_in_one_event` **fails** — the
   workaround mechanism has changed. Re-evaluate.

## 8. References

* Incident evidence: `reports/incidents/2026-05-30_sandbox_partial_fill/`
  (gitignored). PG dumps, Redis snapshot, full 5-day container log.
* Sandbox adapter:
  [`adapters/sandbox/execution.py`](../.venv/Lib/site-packages/nautilus_trader/adapters/sandbox/execution.py),
  particularly line 122 (hard-coded `FillModel()`) and line 135
  (`use_message_queue=False`).
* Sandbox config:
  [`adapters/sandbox/config.py`](../.venv/Lib/site-packages/nautilus_trader/adapters/sandbox/config.py)
  — no `fill_model` field.
* Bar → tick decomposition:
  [`backtest/engine.pyx::_process_trade_ticks_from_bar`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)
  line 4732.
* Quarter-size helper:
  [`model/data.pxd::compute_bar_quarter_sizes`](../.venv/Lib/site-packages/nautilus_trader/model/data.pxd)
  line 226.
* MARKET-order apply path:
  [`backtest/engine.pyx::apply_fills`](../.venv/Lib/site-packages/nautilus_trader/backtest/engine.pyx)
  line 7108 — slip-fill block at line 7299-7336, gated on
  `order.is_open_c()`.
* Order open-state predicate:
  [`model/orders/base.pyx::is_open_c`](../.venv/Lib/site-packages/nautilus_trader/model/orders/base.pyx)
  line 428.
* Live execution engine async queue:
  [`live/execution_engine.py::LiveExecutionEngine.process`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py)
  line 467.
* Built-in fill models:
  [`backtest/models/fill.pyx`](../.venv/Lib/site-packages/nautilus_trader/backtest/models/fill.pyx)
  — `BestPriceFillModel` at line 170, `OneTickSlippageFillModel`,
  `TwoTierFillModel`, `ProbabilisticFillModel`, `SizeAwareFillModel`,
  etc.
* Repro test: [`tests/integration/test_sandbox_partial_fill.py`](../tests/integration/test_sandbox_partial_fill.py).
