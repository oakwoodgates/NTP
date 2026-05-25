# Protective Stop Restart Audit (NT 1.227.0)

**Scope.** What happens to a `ProtectiveStopAware` strategy's reduce-only
stop orders across a container restart, and the defensive hardening
that makes the answer "the position stays protected, no double-stops"
across every realistic failure mode.

**Status.** Stage B prerequisite #4 — audit + defensive hardening
landed. Live reconciliation integration tests deferred until the
"reconciliation enablement" task (see [`docs/ROADMAP.md`](ROADMAP.md))
ships and `scripts/run_sandbox.py` flips `reconciliation=True`. The
hardening is reconciliation-state-agnostic: it works whether NT's live
reconciliation is on or off.

## 1. The stop-order lifecycle, end-to-end

`ProtectiveStopAware` (see [`src/core/protective_stop_mixin.py`](../src/core/protective_stop_mixin.py))
places a reduce-only `StopMarketOrder` at `entry × (1 ± stop_pct)` for
every position the strategy opens. Used by `MACross` to give isolated-
margin equivalence under cross-margin accounting (see
[`docs/LIQUIDATION_AND_SIZING.md`](LIQUIDATION_AND_SIZING.md)).

State lives in three places:

| Layer | What's stored | Survives container restart? |
|---|---|---|
| **Venue (Hyperliquid)** | The stop order itself, with a `venue_order_id` | Yes — orders stay live until filled/cancelled |
| **NT Redis cache** | Position records, order events, client⇄venue order ID mapping | Yes if Redis persists; **no if Redis is wiped or fresh droplet** |
| **NT in-process state** | `Position` and `Order` Python objects | No — rebuilt from cache on startup |
| **Mixin in-process state** | `_protective_order_ids: dict[PositionId, ClientOrderId]` | **No** — lost on every restart, rebuilt by `_protective_rehydrate` |

## 2. NT 1.227.0 live reconciliation — what it actually does

Source: [`nautilus_trader/live/execution_engine.py`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py)
+ [`nautilus_trader/live/reconciliation.py`](../.venv/Lib/site-packages/nautilus_trader/live/reconciliation.py).
NT exec-engine config default `reconciliation: bool = True` (see
[`nautilus_trader/live/config.py:177`](../.venv/Lib/site-packages/nautilus_trader/live/config.py)).

**Startup ordering** (`system/kernel.py::start_async`):

1. `_start_engines()` — engines come up but quiet
2. `_connect_clients()` + `await _await_engines_connected()`
3. `if self.exec_engine.reconciliation: await self._await_execution_reconciliation()`
4. `_initialize_portfolio()` + `await _await_portfolio_initialization()`
5. `_trader.start()` ← finally `Strategy.on_start()` fires

**Conclusion.** By the time `on_start` runs, the cache fully reflects venue
state — making `on_start` the correct hook for `_protective_rehydrate`.

**What reconciliation does for orders.** Calls
`client.generate_order_status_reports(instrument_id=None)` → HL returns
every open order. For each:

- If `client_order_id` is in the cache (via `venue_order_id` index) →
  status updated in place. Tags preserved.
- If not in cache → [`execution_engine.py::_generate_order`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py)
  (line ~3491) creates a new order. The strategy_id resolution is:
  - If a strategy has registered `external_order_claims:
    [instrument_id]` in its `StrategyConfig` → the order is "claimed"
    and routed to that strategy, **but the original `tags` are
    discarded and replaced with `None`**.
  - Otherwise → `strategy_id="EXTERNAL"`, tags set to `["VENUE"]`.

**What reconciliation does for positions.** Calls
`client.generate_position_status_reports`. For each report, if cache
position doesn't match venue, NT generates a synthetic `OrderFilled`
event (tagged `["RECONCILIATION"]`) to align. This synthetic fill flows
through the normal MessageBus and fires `PositionOpened` /
`PositionChanged` on the strategy. The position_id is
`report.venue_position_id` or `PositionId(f"{instrument.id}-EXTERNAL")`
([`reconciliation.py:510`](../.venv/Lib/site-packages/nautilus_trader/live/reconciliation.py)).

## 3. HL adapter capability check

[`nautilus_trader/adapters/hyperliquid/execution.py`](../.venv/Lib/site-packages/nautilus_trader/adapters/hyperliquid/execution.py)
implements all four reconciliation entry points:

- `generate_order_status_report(command)` — single-order lookup
- `generate_order_status_reports(command)` — bulk fetch open orders (line 337)
- `generate_fill_reports(command)` (line 372)
- `generate_position_status_reports(command)` (line 400)

All four are wired to the pyo3 client's `request_*` methods. **HL
reports reduce-only stops alongside regular open orders** (verified in
NT source — the report list is unfiltered by order type).

## 4. Failure modes enumerated

Each row describes the venue-vs-cache state at restart and the resulting
behaviour with the hardening landed in this audit.

### (a) Cache survives, HL still has stop + position

**Setup.** Normal `docker compose restart trader-eth`. Redis volume
intact. HL unchanged.

**Without hardening.** NT reconciliation matches order by `client_order_id`,
updates in place. No synthetic fills. `PositionOpened` is **not**
re-emitted. The mixin's `_protective_order_ids` dict is empty (in-memory
only). When the position later closes via strategy exit,
`on_position_closed → _protective_cancel_stop(pos_id)` looks up
`pos_id` in the empty dict, returns silently. **The reduce-only stop
remains open on HL.** On the next position, a new stop is placed —
two reduce-only stops on the same instrument at different prices.

**With hardening.** `MACross.on_start` → `super().on_start()` →
`ProtectiveStopAware.on_start` → `_protective_rehydrate(instrument_id)`
scans `cache.orders_open(instrument_id=...)` and `cache.positions_open`,
matches by side, rebuilds the map. `_protective_order_ids = {pos_id:
existing_cloid}`. Subsequent `on_position_closed` cancels the correct
order.

### (b) Cache wiped, HL has stop + position (fresh droplet / Redis loss)

**Setup.** `docker compose down --volumes`, redeploy. HL retains
state.

**Without hardening.** NT reconciliation queries HL → finds open
stop. Cache has no `client_order_id` mapping (Redis empty), so it
creates an EXTERNAL order. Either:

- `external_order_claims` not set → order gets `strategy_id="EXTERNAL"`,
  tag `"VENUE"`. The strategy doesn't see it.
- `external_order_claims=[instrument_id]` set → order routed to
  strategy, **tag stripped to `None`**.

Then position reconciliation: HL reports a non-zero position, cache
shows flat → synthetic `OrderFilled` (tag `"RECONCILIATION"`) generated
to align. The fill propagates through the cache: `Position` created,
`PositionOpened` event fires. **Strategy's
`on_position_opened` runs → mixin calls `_protective_issue_stop` →
submits a NEW stop on HL.** Two reduce-only stops on the same position,
likely at slightly different prices (the second uses
`event.last_px` which is the synthetic reconciliation price, not the
original fill).

**With hardening.** Two defences in series:

1. `on_start` → `_protective_rehydrate` runs *before* the synthetic
   `PositionOpened` fires? **No** — actually, the synthetic fill is
   processed during reconciliation phase (step 3 of kernel startup),
   which completes *before* `_trader.start()` calls `on_start` (step
   5). So by `on_start` time, the cache already has the position AND
   the reconciled stop order. Rehydration binds them.
2. Even if (1) misses (e.g. reconciliation completes asynchronously,
   or the `PositionOpened` is emitted late), `_protective_issue_stop`
   now checks `_protective_find_existing_stop` and returns early
   without submitting if a reduce-only `STOP_MARKET` on the close-side
   already exists in the cache. Tag-agnostic by design.

### (c) Stop fired during outage

**Setup.** Container down. HL's stop triggers. Position closes server-
side. Container restarts.

**Without hardening.** HL reports zero open positions, zero open
orders. Cache (if persisted) shows the prior position as "open" but
HL says "flat" → synthetic close fill generated. `PositionClosed`
event fires. Mixin's `on_position_closed` → `_protective_cancel_stop`
on empty dict → silent no-op. **Cache state is consistent. No orphan
stops. Strategy correctly sees flat.**

**Data caveat.** The actual stop fill happened on HL while our
`PersistenceActor` was down. The fill is **not** in our `order_fills`
table — Grafana dashboards will under-count trades for that interval.
This is a known monitoring-only gap, not a trading-state bug.

**With hardening.** Same outcome, plus `_protective_rehydrate` logs
`positions=0 stops=0 bound=0 unbound=0` for operator visibility.

### (d) Operator manually cancelled stop during outage

**Setup.** Operator opens HL UI during outage, cancels the protective
stop intentionally (or accidentally). Position remains open.

**Without hardening.** HL reports the position but no stop. Cache
position survives if Redis persisted; if not, synthetic
`PositionOpened` fires after reconciliation. The mixin's
`on_position_opened` submits a fresh stop. **Recovery.** OK in the
cache-wiped case, but in the cache-survived case there's no event to
trigger replacement — the position runs unprotected until the strategy
emits a `PositionChanged` event (e.g. partial fill, scale-in).

**With hardening.** `_protective_rehydrate` finds the position, finds
no matching stop, logs `PROTECTIVE STOP REHYDRATE: ... has no matching
reduce-only STOP_MARKET on the close-side — protection gap.` This
makes the gap visible. The fix is operator-intervention or a follow-up
auto-replacement task (intentionally not in this PR — auto-replacement
on every restart has its own risks if rehydration runs before
reconciliation completes for any reason).

### (e) Two protective stops already on HL (operator error / prior bug)

**Setup.** A prior buggy run left two reduce-only stops on the same
position. Restart happens.

**Without hardening.** Both get reconciled into the cache.
`_protective_issue_stop` would add a third on the next `PositionOpened`.

**With hardening.** Rehydration binds the first one found,
logs the others as `PROTECTIVE STOP REHYDRATE: ... unbound reduce-only
STOP_MARKET ... possible orphaned stop from a prior run or a duplicate.
Not auto-cancelled; operator review.` Subsequent `_protective_issue_stop`
calls also see the first existing stop and idempotently bind. **Net
effect: no NEW duplicates added; existing duplicates remain for
operator cleanup.**

### (f) HL has stop, NT cache has orphan order missing venue_order_id index

**Setup.** Edge case from a partial Redis backup restore — order is in
cache but the `venue_order_id` index is corrupted.

NT's reconciliation handles this via `_find_order_by_venue_order_id`
([`execution_engine.py:3063`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py))
which falls back to a linear scan of cached orders. Reconciliation
proceeds normally. The mixin's rehydration runs against the (now
correctly-indexed) cache. **No additional hardening needed.**

### (g) Hedging-mode multi-position-per-instrument

**Status: not currently a project concern.** All project strategies
are NETTING (single position per instrument). The side-only matching
heuristic in `_protective_rehydrate` is sufficient.

If hedging is adopted in the future, two changes are needed:

1. `_protective_issue_stop` must pass `position_id=position_id` to
   `submit_order` so the order is bound to the correct position
   server-side.
2. `_protective_rehydrate` must match by `order.position_id` rather
   than by side.

Both are documented in the mixin docstring as "what rehydration
deliberately does NOT do."

## 5. Hardening summary (this PR)

Changes in [`src/core/protective_stop_mixin.py`](../src/core/protective_stop_mixin.py):

1. **`_protective_find_existing_stop(instrument_id, close_side)`** —
   tag-agnostic lookup of an open reduce-only `STOP_MARKET` order on
   the matching close-side. Survives reconciliation tag-stripping.

2. **`_protective_rehydrate(instrument_id)`** — called from `on_start`.
   Scans cache, matches positions to stops by side, rebuilds
   `_protective_order_ids`. Emits an audit summary log line and
   per-anomaly warnings for protection gaps + unbound stops.

3. **Idempotency check in `_protective_issue_stop`** — if a matching
   reduce-only `STOP_MARKET` already exists in cache, bind to it
   instead of submitting a duplicate. Trigger-price discrepancy logged
   as a warning (existing stop kept to avoid an unprotected window).

4. **`on_start` override** — runs rehydration after `super().on_start()`.
   Defensive: no-op when the mixin is disabled, when the strategy has
   no `config.instrument_id` attribute, or when the cache is empty.

Changes in [`src/strategies/ma_cross.py`](../src/strategies/ma_cross.py):

5. **`MACross.on_start` calls `super().on_start()` first** — required
   by the cooperative-super convention. Without it, the rehydration
   hook would never run.

Tests in [`tests/unit/test_protective_stop_mixin.py`](../tests/unit/test_protective_stop_mixin.py)
cover: idempotency (no duplicate submit; warning on trigger drift),
rehydration (one-to-one bind, protection gap, orphan stop, flat
account, multi-position side-matching), cooperative-super chain
including the new `on_start` hop.

## 6. What's deliberately out of scope

- **Live integration tests against HL testnet.** Gated on the
  reconciliation enablement task. The hardening here is unit-tested
  with stubs that mimic the cache + log interfaces; the integration
  story requires `reconciliation=True` everywhere and a testnet wallet
  in the operator's env. Plumbing-only test scaffolds will land with
  that task.
- **Auto-replacement of a missing stop.** Logged as a warning but not
  auto-fixed. Auto-fix on every restart can interact badly with NT's
  reconciliation timing if HL is slow to acknowledge the new order.
  Operator review is the deliberate choice.
- **Doc updates to `PAPER_TRADING_GUIDE.md` / `STRATEGY_ENTRY_RULES.md`.**
  Deferred — the operator-facing restart story changes once
  reconciliation is enabled across both sandbox and live runners.
  When that lands, those docs get the restart playbook section.

## 7. References

- NT 1.227.0 live exec engine — [`live/execution_engine.py`](../.venv/Lib/site-packages/nautilus_trader/live/execution_engine.py),
  particularly `_reconcile_order_report` (line 2985), `_generate_order`
  (line ~3470), `_reconcile_position_report_netting` (line 2443).
  (Line numbers captured at the 1.226 → 1.227 upgrade — re-verify with
  `grep -n` if a future bump shifts them.)
- NT 1.227.0 reconciliation helpers — [`live/reconciliation.py`](../.venv/Lib/site-packages/nautilus_trader/live/reconciliation.py),
  `create_inferred_order_filled_event` (line 434), position_id fallback
  (line 510).
- NT 1.227.0 kernel startup — [`system/kernel.py::start_async`](../.venv/Lib/site-packages/nautilus_trader/system/kernel.py)
  line 999 (reconciliation completes before trader.start).
- HL adapter execution — [`adapters/hyperliquid/execution.py`](../.venv/Lib/site-packages/nautilus_trader/adapters/hyperliquid/execution.py),
  lines 297, 337, 372, 400.
- `LiveExecEngineConfig` — [`live/config.py:177`](../.venv/Lib/site-packages/nautilus_trader/live/config.py)
  `reconciliation: bool = True` default.
- `StrategyConfig.external_order_claims` — [`trading/config.py:90`](../.venv/Lib/site-packages/nautilus_trader/trading/config.py).
