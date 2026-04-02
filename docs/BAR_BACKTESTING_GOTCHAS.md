# Bar-Only Backtesting Gotchas (NautilusTrader 1.224.0)

Lessons learned from adapting NT example strategies to work with OHLCV bar data from `ParquetDataCatalog`. NT's example strategies are written for tick data; several order types silently fail in bar-only backtests.

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

Proven by `ema_cross_trailing.py` which uses `TrailingStopMarketOrder` with `NO_TRIGGER` and produces correct fills in bar backtests.

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

NT's own example (`ema_cross_stop_entry.py`) handles both entry and trailing stop fills in a single `OrderFilled` block — but it submits the trailing stop in `OrderFilled` too, so the ordering doesn't matter. Our pattern (submitting in `PositionOpened`) is safer because the position is guaranteed to exist at that point.

---

## 5. Quote tick guard blocks trailing stop submission

NT's example `ema_cross_trailing_stop.py` checks `cache.quote_tick()` before submitting trailing stops:

```python
last_quote = self.cache.quote_tick(self.config.instrument_id)
if not last_quote:
    self.log.warning("Cannot submit trailing stop: no quotes yet")
    return
```

In bar-only backtests, no `QuoteTick` data exists in the cache (quotes come from `subscribe_quote_ticks`, but synthetic quotes from bar decomposition are processed internally — same issue as section 2). The guard silently returns and the trailing stop is **never submitted**. The position stays open with no exit mechanism.

**Fix:** Remove the quote check. The trailing stop only needs ATR for its offset calculation, not a current quote price. See `ema_cross_trailing.py` lines 236-239 (commented out) for the fix applied to our codebase.

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
