# Liquidation Simulator and Equity-Aware Sizing

## What this is and why it exists

NautilusTrader 1.227.0's `SimulatedExchange` does **not enforce margin** for `MARGIN` accounts and has **no liquidation engine** — verified end to end (unchanged from 1.225 — re-checked at the 1.226 and 1.227 upgrades; **re-confirmed at the 1.228 evaluation**, see note below):

- `risk/engine.pyx:678–679` short-circuits the risk check for margin accounts with `# TODO: Determine risk controls for margin`. Returns `True` unconditionally.
- `accounting/manager.pyx:580–586` has a commented-out `AccountMarginExceeded` raise with the TODO comment: *"Until the platform can accurately track account equity and cross-margin requirements this condition check is inaccurate and causes issues in live trading with more complex margin requirements."*
- `accounting/accounts/margin.pyx:559–566` clamps `free` to 0 rather than raising when over-margined.
- Exhaustive grep for `liquidat | margin_call | maintenance_margin | force_close | bankrupt | insolven` returns **zero matches** in `backtest/` or `risk/` — only in exchange adapters parsing inbound liquidation events from real venues.

This is a deliberate policy choice (graceful degradation over enforcement) driven by live-trading-fidelity concerns. It is **unlikely to change upstream** before NT 2.0.

> **1.228.0 evaluated — native liquidation is a dead field in the v1 API.** 1.228 added `BacktestVenueConfig.liquidation_enabled` / `liquidation_trigger_ratio` and a margin-enforcement path in `crates/risk/.../mod.rs`. **None of it is reachable from the shipped Python wheel:** `BacktestEngine.add_venue()` (what `make_engine` calls) has no `liquidation_enabled` parameter, the field is consumed nowhere in any `.pyx`, and `SimulatedExchange` has no liquidation method at runtime (`risk/engine.pyx` still has the `# TODO` short-circuit). It's a forward-declaration for NT v2. The mixin stays. See [`NT_UPGRADE_NOTES.md`](NT_UPGRADE_NOTES.md) §1.228 finding 3. `tests/integration/test_native_liquidation_engine.py` is the ready-to-flip A/B test that auto-activates the day the native engine becomes real.

The behavioral consequence: a backtest with insufficient equity will fill orders through insolvency, post negative balances, "recover mathematically", and report PnL that would be impossible in live trading. This project closes the gap with its own simulator.

## What it does

Two events flow through the project's `MessageBus`:

| Event | Trigger | Effect |
|---|---|---|
| `PositionLiquidated` | The mixin's reduce-only stop fills — the position is force-closed at the cross-margin liquidation price | Strategy goes flat. Can re-enter on next signal *if* the account is still alive. |
| `AccountLiquidated` | Account equity falls below the IM+fee floor required to open a minimum-size entry | RiskEngine is set to `HALTED`. All subsequent `SubmitOrder` / `SubmitOrderList` commands are denied. Resting orders (including the mixin's already-submitted liq stops) keep firing. Cancels still work. |

Two events are emitted because they have different semantics: position liquidation is recoverable; account liquidation is terminal for the run. Under cross margin with non-trivial gross leverage they tend to fire together (one liq drains equity below the alive floor), but the distinction generalizes correctly to multi-position scenarios.

## Terminology

The codebase uses these terms consistently. Inline notebook prose may use the informal column.

| Informal | Canonical term |
|---|---|
| "starting capital" / "account size" | **starting equity** (or just `equity`, never `balance`) |
| "position size" / "trade size" in dollars | **notional** / **notional exposure** |
| "5% move against me" | **adverse excursion** (point estimate of MAE) |
| "leverage" (the venue's offer) | **venue leverage** — what the venue allows; sets IM |
| "how big I'm actually playing" | **gross leverage** = notional / equity. Always `≤ venue_leverage` |
| "the venue's collateral lock" | **initial margin (IM)** = `notional / venue_leverage` |
| "the minimum to stay alive" | **maintenance margin (MM)** = `notional × mm_rate` |
| "spare room" | **free margin** = `equity − maintenance_margin_used` |
| "risk per trade" / "1R" | **risk budget** = `notional × stop_pct_protective` |
| "a strategy-placed stop" | **protective stop** (reduce-only at the risk-based exit price) |
| "the venue force-closes" | **liquidation** (force-close at the maintenance-margin price) |
| "position closes" | **flatten** / **stop fills** / **goes flat** |
| "position flips" | **close-and-reverse** (flatten current, open opposite) |

Three distinctions worth internalising:

- **Venue leverage ≠ gross leverage.** Venue leverage (20×) is what the exchange allows. Gross leverage (notional / equity) is what you actually deploy. Three knobs (notional, equity, venue_leverage) — any pair determines the third.
- **Initial margin ≠ risk budget.** IM is what the venue locks. Risk budget is what you lose if your protective stop fills. They coincide only when `stop_pct_protective × venue_leverage = 1`.
- **Protective stop ≠ liquidation stop.** Different stop_pct values, different purposes. Protective is the trader's choice; liquidation is determined by sizing under cross margin (`equity / notional − mm_rate`).

## Architecture

### File map

| File | Role |
|---|---|
| `src/core/liquidation.py` | `LiquidationConfig` (msgspec), `PositionLiquidated`/`AccountLiquidated` events, topic constants, `compute_liquidation_price`, `is_account_alive` |
| `src/core/liquidation_mixin.py` | `LiquidationAware` strategy mixin. Submits and maintains the per-position reduce-only stop at the cross-margin liq price. |
| `src/core/protective_stop_mixin.py` | `ProtectiveStopAware` strategy mixin. Submits and maintains a fixed-pct reduce-only stop at `entry × (1 ± stop_pct)`. Composes with `LiquidationAware`. |
| `src/core/sizing.py` | `SizingConfig` (msgspec), `compute_notional`, resolvers (`resolve_min_trade_notional`, `resolve_sizing_from_strategy_config`) |
| `src/actors/account_alive.py` | `AccountAliveMonitor` actor + `AccountAliveMonitorConfig`. Subscribes to `AccountState`, fires the halt callback. |
| `src/backtesting/engine.py` | `make_engine` wiring (BACKTEST-only via `environment` param), `resolve_strategy_liquidation_config` helper, `liquidation_for_environment` per-env adjuster, `_LiquidationCounters` (sweep telemetry), sweep schema columns. |
| `src/core/venues.py` | Per-venue `mm_rate` defaults (HL=0.005, Binance Futures=0.004, Spot=0). |

### Why a mixin and not a single Actor

Two NT 1.227.0 facts forced this split:

1. **Actors cannot submit orders.** Only `Strategy` has `order_factory` / `submit_order`. (`common/actor.pyx`.)
2. **NETTING positions are keyed by `{instrument_id}-{strategy_id}`.** A separate "liquidation strategy" submitting reduce-only orders against another strategy's position is rejected at `backtest/engine.pyx:5037–5049` ("REDUCE_ONLY would have increased position") because the liquidation strategy has no position under its own `strategy_id`.

So position-liquidation order submission **must** originate from the trading strategy itself. Account-alive halt has no such constraint — it can run in an Actor and use NT's HALTED state.

### Inheritance order is non-negotiable

Strategies must inherit mixin-first:

```python
class MACross(LiquidationAware, Strategy):    # ✓ correct
class MACross(Strategy, LiquidationAware):    # ✗ silently broken
```

NT calls typed handlers (`on_position_opened`, etc.) by name. `Strategy` defines those as **concrete no-op stubs** (not abstract methods) — `trading/strategy.pyx:755–801`. With `Strategy` first in MRO, Python's attribute lookup finds the no-op stubs first and the mixin's overrides are silently shadowed. With the mixin first, MRO hits the overrides before the stubs.

This is a one-line bug to introduce when migrating a strategy. The integration test for that strategy will surface it (the mixin produces no `PositionLiquidated` events even when liquidation conditions occur).

#### `LiquidationAware.on_start` does not call `super().on_start()` (PR #44)

Almost every other mixin event handler MUST call `super()` to keep the cooperative chain alive (see [`CLAUDE.md`](../CLAUDE.md) gotcha about cooperative `super()` chains). `on_start` is the deliberate exception. `LiquidationAware.on_start` is a no-op that terminates the chain before reaching NT's base `Strategy.on_start` stub, which logs a misleading `"handler was called when not overridden"` warning whenever a downstream mixin chains up to it. Without this terminator, every paper-trading startup logs the warning once per stacked-mixin strategy, polluting the log with a noise that looks like a wiring bug.

Regression tests at [`tests/unit/test_liquidation_mixin.py::TestOnStartTerminator`](../tests/unit/test_liquidation_mixin.py) pin the behavior. When composing a new mixin with `LiquidationAware`:
- Place the new mixin BEFORE `LiquidationAware` in the MRO if it needs `on_start`; its `super().on_start()` resolves to `LiquidationAware.on_start` (the no-op terminator) and the chain stops there cleanly.
- If you must place it AFTER `LiquidationAware` in the MRO, follow the same no-super-on-start pattern (and add a regression test mirroring the one above).

### Cross-margin liquidation price

```
mm_rate      = venue_config.mm_rate   (per-venue value from src/core/venues.py)
notional     = abs(position.quantity) × entry_price
liq_distance = equity / notional − mm_rate
liq_price    = entry × (1 − liq_distance)   # long
             = entry × (1 + liq_distance)   # short
```

`equity` is read at the time the stop is placed (i.e., `PositionOpened` time). The mixin recomputes on `PositionChanged` (close-and-reverse). Multi-instrument equity-pool drift (where another open position's PnL changes this position's liquidation distance) is **not modeled in v1**.

| Scenario | Equity | Notional | IM as % of equity | Liq @ adverse |
|---|---|---|---|---|
| Conservative (target model) | $1000 | $2000 | 10% | ~49.5% |
| Old default (deprecated) | $100 | $500 | 25% | ~19.5% |
| Max gross leverage | $100 | $2000 | 100% | ~4.5% |

### Account-alive predicate

```
floor_im       = min_trade_notional / venue_leverage
fee_buffer     = min_trade_notional × fee_rate × 2 × alive_trades_buffer
account_alive  = equity ≥ (floor_im + fee_buffer)
```

Static floor (no per-strategy hook) — both fixed and equity-fraction sizing converge to this floor in the regime where the predicate matters. See unit tests in `tests/unit/test_liquidation.py::TestIsAccountAlive`.

### NT's `LeveragedMarginModel` doesn't store mm_rate the way it sounds

`accounting/margin_models.pyx` `LeveragedMarginModel.calculate_margin_maint`:

```
MM = (notional / leverage) × instrument.margin_maint
```

So `instrument.margin_maint = 0.005` does **not** produce MM = 0.5% of notional. It produces MM = 0.025% of notional at 20× leverage. The field semantics don't match its name.

The simulator does **not** use `instrument.margin_maint` for any computation. `mm_rate` lives on `VenueConfig` instead, with `LiquidationConfig.mm_rate` available as a per-run override. The instrument's `margin_maint` is left at NT's default (1.0) — display values are correct under `LeveragedMarginModel`, and margin enforcement is disabled anyway, so the field is inert.

### MessageBus subscription quirk (load-bearing)

NT's `MessageBus` (`common/component.pyx`) caches the resolved subscriber list per concrete topic on first publish. Subsequent subscriptions to a wildcard pattern (e.g. `events.account.*`) added **after** that cache is populated do not get attached, and re-resolution only fires when the cached list is empty.

Because adding the venue with `starting_balances` triggers an initial `AccountState` publish *before* the actor's `on_start` runs, an actor-side subscription registered in `on_start` silently never fires.

The fix: subscribe **before `engine.run()`**. `make_engine`'s `_register_account_alive_monitor` registers the actor's handler directly on `engine.kernel.msgbus` after `add_actor`. The actor's `on_start` no longer subscribes (kept as a no-op log line for symmetry).

If you ever debug "actor X registered but never receives event Y", this is the first place to look.

### `min_trade_notional` resolution order

Used by both the sizing floor and the account-alive predicate. `make_engine` resolves in order:

1. `LiquidationConfig.min_trade_notional` — explicit override.
2. `SizingConfig.min_notional`.
3. `SizingConfig.fixed_notional` (when `mode == "fixed"`).
4. `instrument.min_notional`.
5. Raise — user must set one of the above.

## Sizing

`SizingConfig` is independent of liquidation but feeds the alive-predicate floor.

```python
# Fixed-notional (back-compat)
SIZING = SizingConfig(mode="fixed", fixed_notional=Decimal("2000"))

# Equity-fraction
SIZING = SizingConfig(
    mode="equity_frac",
    risk_frac=Decimal("0.10"),     # 10% of equity at risk per trade
    stop_pct=Decimal("0.05"),       # 5% protective stop
    min_notional=Decimal("50"),     # never size below $50
)
```

The strategy reads its sizing via `compute_notional(equity, sizing, instrument)` at entry. Strategies still accept the legacy `trade_notional: Decimal` field — when set and `sizing` is `None`, the strategy builds a fixed-mode `SizingConfig` from `trade_notional` for back-compat.

The equity-fraction formula (`notional = (risk_frac × equity) / stop_pct`) keeps IM as a fixed fraction of equity (~10% under target params), so the account is never IM-constrained until the dynamic notional collapses to the `min_notional` floor.

## Protective stop loss (`ProtectiveStopAware`)

The `LiquidationAware` mixin places its stop at the **cross-margin liquidation price** — typically far from entry (e.g. ~49.5% with $1000 equity / $2000 notional / 20× leverage / 0.5% mm_rate).  That's the venue-enforced floor; it does NOT cap per-trade loss to your intended risk budget.

`ProtectiveStopAware` fills that gap.  It maintains a fixed-pct reduce-only stop at `entry × (1 ± stop_pct)` for every open position, independent of any other exit logic the strategy has.  The two mixins compose:

```python
class MACross(ProtectiveStopAware, LiquidationAware, Strategy):
    # mixins first; reverse order silently disables them
```

Both stops are reduce-only on the same position; whichever triggers first reduces the position and NT's reduce-only logic cancels the other on fill.

### Isolated-margin equivalence under cross margin

The headline use case for the protective stop is replicating isolated-margin behavior on a cross-margin account.  Recall that initial margin (IM) and risk budget coincide only when:

```
stop_pct_protective × venue_leverage = 1
```

So at 20× leverage the magic number is `stop_pct = 0.05`.  At that setting, the worst-case loss per trade equals the IM committed:

```
risk = notional × stop_pct = notional / leverage = IM
```

Concrete: $1000 equity, $2000 notional, 20× leverage:

| Stop mechanism | Trigger | Worst-case loss |
|---|---|---|
| Cross-margin liq stop (`LiquidationAware` only) | ~49.5% adverse | ~$990 (whole account) |
| Protective stop @ `stop_pct=0.05` | 5% adverse | $100 (= IM) |
| Protective stop @ `stop_pct=0.025` | 2.5% adverse | $50 |

Setting `stop_pct = 1/leverage` is the cleanest way to backtest "what if my position got isolated-margin-liquidated" without modifying the engine or fighting NT's cross-margin model.

### Config

`MACrossConfig` exposes `stop_pct: float | None = None` directly.  Strategies that want protective stops opt in by:

1. Adding `ProtectiveStopAware` to their MRO (mixin first)
2. Adding `stop_pct: float | None = None` to their Config
3. Calling `self._init_protective_stop(config.stop_pct)` from `__init__`

Notebook usage:

```python
# Cell 1.1
STOP_PCT: float | None = 0.05    # 5% — isolated-margin equivalent at 20× lev
# or None to disable (only the cross-margin liq stop fires)

# Cell 2.3
config = MACrossConfig(
    ...,
    liquidation=LIQ_RESOLVED,
    stop_pct=STOP_PCT,
)
```

### Bar-backtest caveat

The protective stop is a NT `StopMarketOrder` (reduce-only), so it triggers on bar OHLC crossings in NT's `SimulatedExchange`.  Same fill optimism as the cross-margin liq stop: NT fills at the trigger price even when the bar wicks past it (no gap modeling).  In live trading on a gap-down day, fills will be worse than the trigger and the realised loss may exceed the intended risk budget by 10–50%.

This is the same caveat that applies to the cross-margin liq stop — see `docs/BAR_BACKTESTING_GOTCHAS.md`.

### Identifying protective-stop fills in analysis

Orders are tagged `["protective_stop"]` for downstream identification.  In a notebook, after the run:

```python
from src.core import PROTECTIVE_STOP_TAG

protective_fills = [
    o for o in engine.cache.orders()
    if o.is_filled and PROTECTIVE_STOP_TAG in (o.tags or [])
]
print(f"Protective stops fired: {len(protective_fills)}")
```

`notebooks/backtest/ma_cross_stop_loss.ipynb` includes a §4.6 close-cause table that splits position closes by source (natural exit / protective stop / liq stop) using this pattern.

## Configuring a notebook

```python
# Cell 1: imports + config
from src.core import (
    TOPIC_ACCOUNT_LIQUIDATED, TOPIC_POSITION_LIQUIDATED,
    LiquidationConfig, SizingConfig,
    bar_type_str, get_venue_config, with_venue_config,
)
from src.backtesting.engine import resolve_strategy_liquidation_config

VENUE_CFG = get_venue_config("HYPERLIQUID_PERP")
LIQUIDATION = LiquidationConfig(
    enabled=True,
    halt_on_account_liquidation=True,
    # mm_rate, fee_rate, min_trade_notional left as None — make_engine
    # resolves them from VenueConfig / SizingConfig / instrument.
)
SIZING = None  # or SizingConfig(...)

# Cell 2: resolve the config once for both make_engine and the strategy.
LIQ_RESOLVED = resolve_strategy_liquidation_config(
    user=LIQUIDATION,
    venue_config=VENUE_CFG,
    instrument=instrument,
    sizing=SIZING or SizingConfig(mode="fixed", fixed_notional=TRADE_NOTIONAL),
)

# Cell 3: pass to make_engine.
engine = make_engine(
    VENUE, instrument, bars, STARTING_CAPITAL,
    leverage=LEVERAGE,
    liquidation=LIQ_RESOLVED,
    venue_config=VENUE_CFG,
    sizing=SIZING,
)

# Cell 4: pass the same resolved config into the strategy.
config = MACrossConfig(
    instrument_id=instrument.id,
    bar_type=BarType.from_str(BAR_TYPE_STR),
    sizing=SIZING,
    trade_notional=TRADE_NOTIONAL,    # back-compat fallback
    ma_type="EMA", fast_period=10, slow_period=40,
    liquidation=LIQ_RESOLVED,
)
```

For sweeps, pass the same `LIQ_RESOLVED` to both the strategy factory and `run_sweep`:

```python
results_df = run_sweep(
    venue=VENUE, instrument=instrument, bars=bars,
    starting_capital=STARTING_CAPITAL,
    param_combos=combos,
    strategy_factory=ma_factory,
    liquidation=LIQ_RESOLVED,
    venue_config=VENUE_CFG,
    sizing=SIZING,
    # ... other args
)
```

See `notebooks/backtest/ma_cross.ipynb` for the canonical pattern.

## Sweep schema additions

When `liquidation` is enabled, every row of the sweep DataFrame gains:

| Column | Type | Meaning |
|---|---|---|
| `liquidated_positions` | int | Count of `PositionLiquidated` events. 0 for clean survivors. |
| `liquidated_account` | bool | Whether `AccountLiquidated` fired. Latched once-per-run. |
| `liquidated_at_ts` | str \| None | ISO timestamp of the account-liquidation event, or None. |
| `denied_post_halt` | int | Count of `OrderDenied` events with reason `TradingState.HALTED`. Sanity check that halt is enforcing. |
| `liq_slippage_avg_pct` | float \| NaN | Average trigger-vs-fill slippage as % of entry (positive = worse than trigger). |
| `liq_slippage_max_pct` | float \| NaN | Worst single-event slippage in the run. |
| `total_fees` | float \| NaN | Sum of commissions across all positions in settlement currency. |

When `liquidation` is `None` or `enabled=False`, the columns are still present (zero / false / NaN) so saved sweeps remain schema-compatible.

## Trustworthiness checks

The notebook's Cell 12b runs five sanity checks every time:

1. **Schema completeness** — every row has populated liquidation columns.
2. **`min_balance` / `liquidated_account` consistency** — if equity went sub-zero but the actor never fired, the actor missed a breach.
3. **Halt enforcement** — every dead combo should have `denied_post_halt > 0` (the strategy keeps signaling, RiskEngine HALTED rejects).
4. **Fee model cross-check** — `total_fees / num_positions` should be roughly `2 × notional × taker_fee` (round-trip per position). 10% tolerance allows for price-drift between qty calc and fill on a trending asset.
5. **Liquidation slippage** — distribution of `trigger_price` vs `fill_price`. Currently `0.0%` for all events — NT's bar matching engine fills stops at the trigger price exactly, even when the bar's wick gaps through. This is an optimism in the simulator (see "What it doesn't model" below).

If any of these checks fails on a future run, that's a regression worth investigating.

## Live mode and sandbox

The simulator is **backtest-only by design**. Live and sandbox runs use `TradingNode` directly via `scripts/run_live.py` and `scripts/run_sandbox.py` — `make_engine` is not in those code paths and now defensively rejects non-BACKTEST environments.

The strategy mixin behavior is governed by the `liquidation` field on the strategy's config. When `None`, the mixin no-ops (no reduce-only stops placed). The runner scripts must set this appropriately per environment.

### Per-environment helper

Use `liquidation_for_environment(config, environment)` from `src.backtesting.engine` to map a single user config to the right per-environment value:

```python
from nautilus_trader.common import Environment
from src.backtesting.engine import liquidation_for_environment

USER_LIQ = LiquidationConfig(enabled=True, halt_on_account_liquidation=True)

# In a backtest notebook:
backtest_liq = liquidation_for_environment(USER_LIQ, Environment.BACKTEST)
# → unchanged: full simulator with halt

# In run_sandbox.py:
sandbox_liq = liquidation_for_environment(USER_LIQ, Environment.SANDBOX)
# → halt_on_account_liquidation=False (don't kill a paper-trading session)

# In run_live.py:
live_liq = liquidation_for_environment(USER_LIQ, Environment.LIVE)
# → None (venue handles its own liquidation)
```

### What the helper produces

| Environment | What you want | Helper output |
|---|---|---|
| `BACKTEST` | Full simulator: per-position liq stops + account halt | Original config unchanged |
| `SANDBOX` | Simulate liquidation against live data, but don't HALT a long-running paper session | Copy with `halt_on_account_liquidation=False` |
| `LIVE` | Simulator OFF — the venue handles its own liquidation | `None` |

Why off in live: the mixin would place real reduce-only stops on the venue's order book. Hyperliquid does its own liquidation; our stops could fire at the wrong price, fight HL's own forced close, or land in unintended interactions. In sandbox, simulated liquidation is appropriate (NT's `SimulatedExchange` runs against live data and has the same no-enforcement bug), but you typically don't want to halt a paper-trading session that's collecting longitudinal data.

### Defensive default in `run_live.py`

Strategy configs in `run_live.py` set `liquidation=None` explicitly even though that's also the field default. Belt-and-braces: if a user later wires up `liquidation_for_environment` in `run_live.py` and forgets to pass `Environment.LIVE`, the default still keeps the simulator off.

## What it doesn't model

- **Gap-risk slippage on stop fills.** NT's bar matching engine fills `StopMarketOrder`s at the trigger price during synthetic-tick processing — even when the bar's wick gaps far past it. Real venues fill at "next available price", which on a big-gap day is significantly worse. Liquidation losses are systematically *best-case* in this simulator.
- **Isolated margin.** NT 1.227.0 has no isolated/cross toggle. `MarginAccount` is always cross. Locked design decision.
- **Multi-instrument cross-margin equity drift.** The mixin computes `liq_price` once at position open, using equity at that moment. Other positions' PnL won't move this position's liquidation distance. Single-instrument-per-strategy v1 only.
- **Liquidation cascades / auto-deleveraging.** Real venues run ADL when a liquidation can't be absorbed; we don't model that.
- **Funding fees.** Out of scope.
- **Pre-trade margin rejection.** NT's RiskEngine bypasses margin checks for `MARGIN` accounts (`risk/engine.pyx:678`). The simulator doesn't restore that behavior — it relies on the alive predicate firing at the next AccountState event after the breach.

## Two known optimisms (net effect for the user)

1. **Liquidation losses are too small.** Stops fill at trigger; real fills would be worse on gap days. A combo that lost $990 in the simulator might lose $1,400 live.
2. **Sweep "best params" can mask survivor bias.** Combos that survived the dataset's stress events look stable; a different historical period (or no liquidation simulator at all) would tell you a different story. Always filter `liquidated_account == False` before ranking by PnL, and treat the dead-combo proportion as a regime risk.

For research-grade comparison between parameter combos these optimisms wash out (every combo gets the same simulator). For "would this strategy survive in live" they don't — you should mentally add 30–50% to liquidation losses on big-gap days.

## References

- Source files listed in "Architecture → File map" above.
- Tests: `tests/unit/test_liquidation.py`, `test_liquidation_mixin.py`, `test_engine_resolution.py`, `test_sizing.py`.
- Bar-fill behavior for stops: `docs/BAR_BACKTESTING_GOTCHAS.md` §3.
- Returns-stat caveat that interacts with liquidated rows: `docs/ANALYZER_RETURNS_CAVEAT.md`.
