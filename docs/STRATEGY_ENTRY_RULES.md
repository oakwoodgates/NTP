# Strategy Entry Rules

How `MACross` decides when to enter, why it's structured this way, and
the contract every other strategy in the project should follow.

## The principle

> **Trade events, not states.**

A strategy's signal is a *transition* — a fresh cross of two MAs, a
fresh penetration of a band, a fresh confirmation of an indicator
flip. It is **not** the same as the *state* "MA1 < MA2 right now."

Implementations that poll the state ("on every bar, if state is true
and position is flat, enter") look like they trade crosses but
actually re-enter on every bar where the state happens to be true.
After a stop-out the state is still true, so they re-enter
immediately into the same losing setup. This was the bug in
`MACross` before the cross-gate fix landed: three SHORT entries in
three consecutive bars after the EMA crossed bearish, each stopped
at -101 USDC.

## What MACross does now

Every entry path in `MACross.on_bar` is gated on a fresh cross:

```python
def on_bar(self, bar):
    ...
    signal, should_act = self._cross_gate_decision(
        fast_value=self.fast_ma.value,
        slow_value=self.slow_ma.value,
        last_signal=self._last_signal,
        bootstrap_pending=self._bootstrap_pending,
    )
    if not should_act:
        return
    self._last_signal = signal
    self._bootstrap_pending = False
    # ... act on signal
```

The strategy keeps `self._last_signal` as `+1` (long), `-1` (short),
or `0` (no signal yet / fresh reset). On each bar it computes the
current signal direction; only fires when the new value differs from
`_last_signal`.

`_last_signal` **persists across exits.** When a position closes —
strategy cross-back, protective stop, liquidation, take-profit,
account halt — the signal direction is unchanged. The next entry
waits for a genuine MA transition.

## Why one rule covers all exit causes

Same gate handles every kind of exit:

| Exit cause | What the gate does |
|---|---|
| Strategy cross-back | Sets `_last_signal` to new direction; next entry on the cross after that |
| Protective stop fired | `_last_signal` unchanged; waits for next genuine cross |
| Liquidation simulator fired | Same |
| Take-profit hit | Same |
| Trailing stop hit | Same |
| Time stop / manual close | Same |
| Account-level halt | Same |

There's no "exit-cause-aware" branch. The strategy doesn't need to
know *why* the position closed; it only needs to know whether the
signal has freshly transitioned. This is what makes the rule
defensible and predictable.

The deep reason: a stop-out is **information** that the market
disagrees with the signal at the trigger price. Re-entering on the
unchanged signal denies that information. Wait for a new statement
from the market — i.e., a new cross — before placing a fresh bet.

## Cross-gate state persists across container restarts

`_last_signal` is in-process Python state — gone when the container
dies. Without a persistence story this would re-create the original
bug under a different name: every container restart would reset the
strategy to `_last_signal=0`, then the first observed signal post-
restart would be treated as a fresh cross and acted upon — closing or
flipping a position that the prior run had already committed to.

The strategy now uses NT's `on_save` / `on_load` hooks
(`nautilus_trader/common/actor.pyx`) to write the cross-gate state to
the cache database (Redis in production, in-memory in tests) on
graceful shutdown and read it back on the next start:

| State | Why it's persisted |
|---|---|
| `_last_signal` | Without this, the first post-restart signal looks like a fresh cross — re-arms the gate against the position we just reconciled |
| `_bootstrap_pending` | Set by `bootstrap_on_deploy=True` and consumed by the first bar; persisted as `False` after firing so a restart doesn't re-trigger it |
| `_protective_order_ids` (mixin) | Maps reconciled positions back to their reduce-only protective stops so `on_position_closed` can cancel cleanly |
| `_liq_order_ids` (mixin) | Same, for the cross-margin liquidation stops |

The runners (`scripts/run_sandbox.py`, `scripts/run_live.py`) enable
this by setting `save_state=True` + `load_state=True` on
`TradingNodeConfig` and calling `node.trader.load()` manually after
`add_strategy` (NT's kernel autoload runs only against
`config.strategies`, but we add strategies imperatively, so the
autoload skips us).

### When state is saved

NT's `kernel.stop_async()` calls `trader.save()` after stopping the
trader but before disconnecting clients (`system/kernel.py`). That
means `on_save` fires on every graceful shutdown — Ctrl+C in dev,
`docker stop` / SIGTERM in container, `node.stop()` from a wrapper.

It does **not** fire on hard kill (SIGKILL, OOM, host crash). In
those cases the state is whatever was last persisted by the prior
graceful shutdown, which can be stale. That's a known limitation — a
hard kill in the middle of a position open would leave Redis with the
pre-open state and `_last_signal=0` on next start. The same issue
exists for NT's own order/position cache, so a hard kill always
implies a reconcile-and-re-evaluate cycle on next start.

### When state is loaded

`node.trader.load()` is called once after all strategies and actors
are registered, before `node.run()`. NT's `cache.load_strategy` is a
silent no-op when no prior state exists (first run, fresh Redis), so
the load step is idempotent — safe to call even on the very first
container start.

If the state schema changes between builds (added key, renamed key,
changed encoding), the load code is defensive: missing keys fall
back to the post-`__init__` defaults, malformed values log a warning
and reset to defaults. A state-load error must never stop the trader
from running.

### Restart from a reconciled position

The intended scenario:

1. Strategy was long BTC, `_last_signal=+1`, position open on HL.
2. Operator restarts the container (`docker compose restart`).
3. SIGTERM → graceful shutdown → `on_save` writes
   `_last_signal=+1` to Redis.
4. Container starts → `on_load` reads `_last_signal=+1`. NT's exec
   reconciliation pulls the open BTC long back into the cache.
5. First post-restart bar with a LONG signal: gate says
   "same direction, no action" — position stays untouched.
6. Eventually a SHORT signal appears: gate says "new direction,
   act" — strategy closes the long and enters short, exactly as
   if the restart had never happened.

Without `on_save` / `on_load`, step 5 would see
`_last_signal=0 != +1` → `should_act=True` → `_last_signal` gets
written to +1 on the same bar. No order is submitted (we're already
long), but the gate has "consumed" a transition that wasn't real.
The bug becomes visible on the bar AFTER that: any flip is
genuinely missed because we've already credited ourselves with
acting on the long signal.

## Bootstrap on deploy (the legitimate exception)

When you deploy a strategy mid-trend and want it to catch the current
move rather than wait for the next cross, set
`bootstrap_on_deploy=True` in `MACrossConfig`. The first observed
signal then counts as a synthetic cross. Default is `False` (wait
for a real transition).

In code (notebook / backtest):

```python
MACrossConfig(
    instrument_id=instrument_id,
    bar_type=bar_type,
    ma_type="EMA",
    fast_period=10,
    slow_period=40,
    bootstrap_on_deploy=True,   # only set this for live mid-trend deploy
)
```

At runtime (sandbox / live runners):

```bash
# In .env (or .env.{asset} for multi-instrument)
BOOTSTRAP_ON_DEPLOY=true        # only for live mid-trend deploy
```

The sandbox + live runners read `settings.bootstrap_on_deploy` and pass
it into `MACrossConfig`. Default is `false` so paper trading and
verification flows wait for real crosses, keeping backtest ↔ paper
signal alignment honest.

This handles every "fresh start" case uniformly — initial deploy,
restart after crash, parameter swap mid-run. Once the bootstrap fires
once, the gate reverts to normal "fresh transition required" mode.

For backtests the bootstrap shouldn't be needed — the engine starts
flat and the first cross fires naturally (since `_last_signal=0` and
the first observed signal is non-zero).

## What MACross does NOT do (and why)

These are deliberate omissions, not features waiting to be added.

### No cooldown timer after a stop-out

There's no "wait N bars after a stop before re-arming." The cross-gate
already gives this for free — the next entry waits for a fresh
transition, which is timer-equivalent on stable signals and
substantially better on choppy ones (no arbitrary parameter to tune).

### No cause-aware re-entry rules

Code like "if last close was a stop, suppress re-entry; otherwise
re-enter on signal-state-true" is a separate semantic that complicates
the strategy and adds branches that interact in subtle ways. The
unified gate is simpler and equivalent.

### No reverse-on-stop ("I got stopped, flip the signal")

That's a different strategy (mean-reversion / counter-trend) wearing
the cross name. Build it as its own strategy class.

### No N×ATR re-entry threshold

Adds an ATR period to tune, a multiplier to tune, and a logic branch
invisible to anyone reading the code. The cross-gate is simpler and
just as effective.

## What counts as a "signal" in this codebase

For the cross-gate principle to apply uniformly, we need to be clear
on what *is* and *is not* a signal:

| Mechanism | Category | Is a signal? |
|---|---|:-:|
| MA cross | Signal generator | Yes |
| RSI / MACD / Bollinger threshold | Signal generator | Yes |
| Donchian breakout | Signal generator | Yes |
| ATR (used as multiplier) | Risk-management scaling | No |
| Protective stop | Risk-management | No |
| Bracket order | Order structure | No |
| Take-profit | Planned exit | No |
| Trailing stop | Risk-management | No |
| Liquidation event | Forced exit | No |

**Signal = generates a NEW edge claim → place a position.**
**Risk management = closes an existing trade → don't generate a
fresh signal.**

Stops, brackets, take-profits are about *managing a trade you already
took*. They don't tell you anything new about market direction; they
tell you "this trade is over." The strategy waits for the next signal
generator before opening a new position.

## Contract for new strategies

When adding a new strategy to the project, follow the same pattern:

1. Identify the *signal-generator* event (cross, breakout, threshold
   penetration, divergence, etc.).
2. Track the last acted-upon signal direction in instance state.
3. Gate every entry on a fresh transition.
4. Reset the state on `on_reset` so sweep iterations start clean.
5. Optional: support a `bootstrap_on_deploy` flag for mid-trend live
   start-up.
6. Test the gate in a unit test:
   - Same-direction bar after a stop-out doesn't re-enter
   - Opposite-direction transition triggers a flip
   - `on_reset` clears state

`MACross._cross_gate_decision` is the reference implementation. It's
a pure function — call it from your `on_bar` and act on the
`(new_signal, should_act)` tuple it returns.

## Footnote: pyramiding

If you actually want *pyramiding* (scale into a continuing trend),
that's a different strategy ("MA cross with pyramid-into-trend"). It
needs:

- A separate trigger for adding to the position (e.g., ATR-pull-back
  within an active trend, time-since-entry, equity-up confirmation)
- Position-size management for the additions
- Different stop logic per leg

Build it as its own strategy class with its own signal definition.
Don't mix it into a vanilla cross strategy.
