# Bar-Only Backtesting Gotchas (NautilusTrader 1.225.0)

Lessons learned from adapting NT example strategies to work with OHLCV bar data from `ParquetDataCatalog`. NT's example strategies are written for tick data; several order types silently fail in bar-only backtests, and the engine itself doesn't enforce margin.

---

## 1. MarketIfTouched (MIT) orders never trigger

**Symptom:** Strategy submits MIT orders every bar, all end up CANCELED, zero fills, zero positions. No errors logged.

**Root cause — two independent failures:**

### Path A: `emulation_trigger="NO_TRIGGER"` (exchange-managed)

MIT goes to `SimulatedExchange`. The matching core's `is_touch_triggered()` checks bid/ask prices, but bid/ask are **never initialized from OHLCV bar data**. They're only set from `QuoteTick` or `OrderBook` events, which don't exist in bar-only backtests. The trigger check returns `False` every time.

### Path B: `emulation_trigger="LAST_PRICE"` or `"BID_ASK"` (emulated)

MIT is managed by `OrderEmulator`, which subscribes to `TradeTick` (for `LAST_PRICE`) or `QuoteTick` (for `BID_ASK`) events on the message bus. But `BacktestEngine.process_bar()` decomposes bars into 4 synthetic `TradeTick`s (O, H, L, C) that are processed **internally** by the `SimulatedExchange`. These synthetic ticks are **never published to the message bus**. The `OrderEmulator` receives no data and never evaluates triggers.

### Evidence

Debug script running 100 daily BTC bars: 81 MIT orders submitted, all CANCELED, 0 fills. Trigger prices were clearly crossed by subsequent bars (e.g., SELL trigger=7851.70, next bar close=8195.38).

### Fix: Manual trigger checking + MARKET orders

The standard approach for breakout strategies in bar-based backtesting:

```python
# State variables
self._pending_side: OrderSide | None = None
self._pending_trigger: Price | None = None

def on_bar(self, bar: Bar) -> None:
    # 1. Check pending trigger from PREVIOUS bar
    if self._pending_side is not None and self._pending_trigger is not None:
        triggered = False
        if self._pending_side == OrderSide.BUY:
            triggered = bar.high >= self._pending_trigger
        else:
            triggered = bar.low <= self._pending_trigger

        if triggered and self.portfolio.is_flat(self.config.instrument_id):
            # Enter via MARKET order
            order = self.order_factory.market(...)
            self.submit_order(order)
            self._pending_side = None
            self._pending_trigger = None
            return

    # 2. Set trigger for NEXT bar (only when flat)
    if not self.portfolio.is_flat(self.config.instrument_id):
        self._pending_side = None
        self._pending_trigger = None
        return

    offset = self.atr.value * self.config.entry_offset_atr
    if self.fast_ema.value >= self.slow_ema.value:
        self._pending_side = OrderSide.BUY
        self._pending_trigger = self.instrument.make_price(bar.high + offset)
    else:
        self._pending_side = OrderSide.SELL
        self._pending_trigger = self.instrument.make_price(bar.low - offset)
```

**Trade-off:** MIT fills at the trigger price; MARKET fills at the current bar's price. In bar backtesting this is an acceptable approximation — you can't get intra-bar fill precision from bar data anyway.

### Affected order types

This applies to any order type that relies on trigger-price evaluation:
- `MarketIfTouchedOrder`
- `LimitIfTouchedOrder`

It does **not** affect:
- `MarketOrder` — always fills immediately
- `LimitOrder` — managed by SimulatedExchange matching core differently
- `TrailingStopMarketOrder` with `NO_TRIGGER` — see section 3

---

## 2. OrderEmulator limitations with bar data

The `OrderEmulator` is always instantiated in `BacktestEngine` (unconditionally in `kernel.py`), but it is **useless for bar-only backtests** because:

1. It subscribes to `TradeTick` or `QuoteTick` events on the **message bus**
2. Synthetic ticks from bar decomposition are processed **internally** by `SimulatedExchange` and never published to the bus
3. The emulator receives zero data events and never evaluates any trigger conditions

This means setting `emulation_trigger` to `LAST_PRICE` or `BID_ASK` on any order type won't help in bar-only backtests. The emulator simply sits idle.

**When does emulation work?** Only when actual `TradeTick` or `QuoteTick` data is loaded into the backtest (via `QuoteTickDataWrangler` or similar). This is how NT's own example scripts work — they use tick data, not bar data from a catalog.

---

## 3. TrailingStopMarket works with NO_TRIGGER in bar backtests

Unlike MIT orders, `TrailingStopMarketOrder` with `emulation_trigger="NO_TRIGGER"` works correctly in bar-only backtests. This is because:

- The `SimulatedExchange` uses `is_stop_triggered()` in `matching_core.pyx`, which checks against **trade ticks from bar decomposition internally**
- These synthetic ticks (O, H, L, C) are fed to the matching core directly, which updates the trailing offset and evaluates the stop trigger
- The key difference: MIT trigger evaluation requires initialized bid/ask; stop trigger evaluation uses the internal trade tick stream

Proven by `ma_cross_trailing_stop.py` which uses `TrailingStopMarketOrder` with `NO_TRIGGER` and produces correct fills in bar backtests.

---

## 4. NT event ordering: OrderFilled before PositionOpened

When an entry order fills, NT fires events in this order:

1. `OrderFilled`
2. `PositionOpened` (or `PositionChanged`)

**Gotcha:** If you clear your entry order reference in the `OrderFilled` handler:

```python
# WRONG — breaks PositionOpened handler
def on_event(self, event):
    if isinstance(event, OrderFilled):
        if self.entry and event.client_order_id == self.entry.client_order_id:
            self.entry = None  # Cleared too early!

    elif isinstance(event, PositionOpened):
        if self.entry is not None:  # Always False now!
            self._submit_trailing_stop(...)  # Never reached
```

**Fix:** Only clear trailing stop references in `OrderFilled`. Keep entry references alive for `PositionOpened`:

```python
# CORRECT — matches ema_cross_trailing.py pattern
def on_event(self, event):
    if isinstance(event, OrderFilled):
        # Only clear TRAILING STOP reference, not entry
        if self.trailing_stop and event.client_order_id == self.trailing_stop.client_order_id:
            self.trailing_stop = None

    elif isinstance(event, (PositionOpened, PositionChanged)):
        if self.trailing_stop is not None:
            return  # Already managing
        if self.entry and event.opening_order_id == self.entry.client_order_id:
            self._submit_trailing_stop(...)
```

NT's own example (`ma_cross_stop_entry.py`) handles both entry and trailing stop fills in a single `OrderFilled` block — but it submits the trailing stop in `OrderFilled` too, so the ordering doesn't matter. Our pattern (submitting in `PositionOpened`) is safer because the position is guaranteed to exist at that point.

---

## 5. Quote tick guard blocks trailing stop submission

NT's example trailing stop strategy checks `cache.quote_tick()` before submitting trailing stops:

```python
last_quote = self.cache.quote_tick(self.config.instrument_id)
if not last_quote:
    self.log.warning("Cannot submit trailing stop: no quotes yet")
    return
```

In bar-only backtests, no `QuoteTick` data exists in the cache (quotes come from `subscribe_quote_ticks`, but synthetic quotes from bar decomposition are processed internally — same issue as section 2). The guard silently returns and the trailing stop is **never submitted**. The position stays open with no exit mechanism.

**Fix:** Remove the quote check. The trailing stop only needs ATR for its offset calculation, not a current quote price. See `ma_cross_trailing_stop.py` for the fix applied to our codebase (quote check removed entirely).

---

## 6. Debugging strategy: empirical over theoretical

When a strategy produces 0 fills, don't trust source code analysis alone. NT's Cython internals have complex interactions between the SimulatedExchange, OrderEmulator, and message bus that are hard to reason about from reading code.

**Effective approach:**
1. Add debug logging to `on_bar`, `on_event`, and order submission methods
2. Run a short backtest (50-100 bars) with `log_level="INFO"`
3. Check: Are orders being submitted? What status do they reach? Are events firing?
4. Create a standalone debug script (not a notebook) for faster iteration

**Red flags that indicate bar-data incompatibility:**
- Orders submitted but all end up `CANCELED` (MIT/LIT trigger never satisfied)
- Orders in `EMULATED` status that never transition (emulator receiving no data)
- Trailing stops that never trigger (less common — usually works with `NO_TRIGGER`)

---

## 7. Margin is not enforced on `MARGIN` accounts (and there's no liquidation engine)

**Symptom:** Backtest with `STARTING_CAPITAL=100`, `TRADE_SIZE=500`, `LEVERAGE=20`. Strategy continues opening positions after equity goes negative. Sweep rows show `final_balance > 0` and positive PnL even though `min_balance` was deeply negative — synthetic profits accruing on top of an insolvent account.

**Root cause — three deactivated checks:**

- `risk/engine.pyx:678–679`:
  ```cython
  if account.is_margin_account:
      return True  # TODO: Determine risk controls for margin
  ```
  RiskEngine short-circuits the risk check for any margin account. CASH accounts get balance-and-position checks; MARGIN does not.
- `accounting/manager.pyx:580–586` has a commented-out `AccountMarginExceeded` raise with the comment: *"causes issues in live trading with more complex margin requirements."*
- `accounting/accounts/margin.pyx:559–566` clamps `free` balance to 0 rather than raising when over-margined: *"We intentionally do not raise as this condition can occur transiently when the venue and client state are out-of-sync."*

Three independent sites chose graceful degradation over enforcement. Exhaustive grep over `liquidat | margin_call | maintenance_margin | force_close | bankrupt | insolven` returns **zero matches** in `backtest/` or `risk/` — confirming no liquidation logic anywhere in the simulator.

This is a deliberate NT policy choice driven by live-trading-fidelity concerns. Unlikely to change before NT 2.0.

### Project-side fix: the liquidation simulator

Built into the project as a strategy mixin + actor + halt callback. Opt in by passing a `LiquidationConfig(enabled=True)` plus the matching `VenueConfig` to `make_engine`. The simulator:

- Places a reduce-only `StopMarketOrder` at the cross-margin liquidation price for every open position (per-strategy via `LiquidationAware` mixin).
- Watches account equity on `AccountState` events and halts the `RiskEngine` when equity falls below the IM+fee floor for a min-size entry (`AccountAliveMonitor` actor).
- Adds six telemetry columns to sweep results: `liquidated_positions`, `liquidated_account`, `liquidated_at_ts`, `denied_post_halt`, `liq_slippage_avg/max_pct`, `total_fees`.

See `docs/LIQUIDATION_AND_SIZING.md` for architecture, terminology, and the notebook configuration pattern. The `ma_cross.ipynb` notebook is the canonical example.

### Bar-fill caveat for liquidation stops (and protective stops)

NT's bar matching engine fills `StopMarketOrder`s at the **trigger price** during synthetic-tick processing — even when the bar's wick gaps far past the trigger. Real venues fill at "next available price", which on a big-gap day is materially worse. Liquidation losses in the simulator are systematically best-case. The `liq_slippage_*` sweep columns measure this: currently always `0.0%`, confirming NT's optimistic fill behavior.

The same caveat applies to the **protective stop** (`ProtectiveStopAware` mixin, `src/core/protective_stop_mixin.py`) — it submits the same `StopMarketOrder` order type with the `protective_stop` tag. In real trading, gap-through events will fill at worse prices than the trigger, so the realised loss can exceed the intended risk budget. Empirically observed in SOL 1d EMA(10/40) backtests: at `STOP_PCT = 0.05` (target $100 risk) the max single loser was -$151.50, ~50% over budget — the cross-margin liq stop fired after price gapped past the protective stop. See `docs/LIQUIDATION_AND_SIZING.md` §"Protective stop loss" → "Bar-backtest caveat" for the full discussion.

### Live-mode caveat

The simulator must be **disabled in live trading** (`run_live.py`). Real venues handle liquidation themselves; running our reduce-only stops on top of HL's order book would interact unpredictably with the venue's own forced close.

Use `liquidation_for_environment(config, env)` from `src.backtesting.engine` to map a single user config to the right per-environment value (BACKTEST: unchanged, SANDBOX: `halt_on_account_liquidation=False`, LIVE: `None`). `make_engine` itself rejects non-BACKTEST environments — it physically only constructs a `BacktestEngine`.

`run_live.py` also sets `liquidation=None` explicitly on strategy configs (defensive — even though that's the field default).

---

## 8. Bad-bar / wick-driven liquidation produces apparent over-loss

**Symptom:** A sweep heatmap shows cells where `total_pnl ≤ −starting_capital` — the account ended past zero by hundreds of dollars. The first reaction is "did one trade lose more than its margin allowed?"

**Diagnosis (ETH 1d EMA 10/40 stop=10%, $1k capital, $2k notional, 20× leverage, observed during phase 3a).**

22 cells across the 12×12 grid all converged on `total_pnl ≈ −1,193`. Drilling into the `fast=5, slow=35` combo:

```
trade #1  opened 2020-03-09  closed 2020-03-14  side=SHORT
          entry=$199.54      fill=$320.98      pnl=-$1,219
          cause=liquidation  (61% adverse on a 5-day position)
```

ETH bar on 2020-03-13 (Black Thursday +1):

```
open=$106.97  high=$323.00  low=$84.23  close=$134.13   range=223%
```

Binance perp ETH had a **$323 wick** on 2020-03-13 — a brief liquidation cascade that lasted milliseconds in real life but persists forever in the saved bar. The strategy was SHORT at $199.54; the `LiquidationAware` mixin's cross-margin stop trigger sat at ~$298. The bar's `high=$323` triggered the stop. NT's bar-only fill engine filled at $320.98 (close to the bar's high) — far past the trigger.

**On a real exchange (Hyperliquid), the same scenario:**

- Exchange-side liquidation caps loss at account equity (~$0)
- HL's mark price (used for liquidation triggers) typically smooths Binance's spot wicks
- Even if HL did liquidate, you can't lose more than your margin

**What this means for sweep interpretation:**

- Treat `total_pnl ≤ −starting_capital` rows / cells as "**wiped out**", not as additional loss magnitude
- The exact number past zero is bar-fill noise — backtest is rendering a wick scenario pessimistically
- Information value is "this combo died on this combination of data"; ignore the precise final number

**This is not a strategy bug, runner bug, or simulator bug.** It's the documented bar-only-backtest behaviour from sections 1, 5, 7 above, applied to a specific bar with an outlier wick. The root cause is NT filling triggered orders at adverse-to-trigger prices when bars gap, which is the same modeling limitation behind the optimistic-fill caveat at the end of section 7.

**Phase 2.6** (per [`ROADMAP.md`](ROADMAP.md)) is the path to quantify this: paper-vs-backtest comparison framework will measure how often these wick scenarios produce predicted losses that real-life liquidations would have capped, and translate that into an "accuracy haircut" metric.
