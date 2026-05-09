# Pipeline Verification Guide

Verification plan for confirming that backtesting, walk-forward validation,
and indicator calculations mirror live trading behavior. Work through each
layer in order — later layers depend on earlier ones being clean.

**Goal:** After completing all six layers, you can trust that when the
walk-forward says a strategy made $491 OOS across 4 folds, that number
reflects what would have happened if you had deployed it live on those dates,
and that live fills persist correctly to PostgreSQL for monitoring.

---

## Layer 1: Data matches TradingView

**What you're verifying:** The OHLCV bars in your ParquetDataCatalog are
identical to what TradingView shows for the same instrument and timeframe.

**How:**

1. Pick 5–10 bars scattered across your dataset — different years, different
   market conditions (trending, ranging, high-vol, low-vol).
2. For each bar, note the exact timestamp (e.g. `2024-06-15 12:00 UTC`).
3. Load the bar from your catalog:
   ```python
   bar = bars[idx]
   print(f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}")
   ```
4. Open TradingView → same instrument (BTCUSDT Perp on Binance) → same
   timeframe (4h) → navigate to that exact bar.
5. Compare all five values. They should match exactly or within the
   instrument's tick size.

**Common failure modes:**
- Timezone mismatch (your data is UTC but you're reading TradingView in local time)
- Bar boundary convention (some sources use bar-open timestamp, others use bar-close)
- Volume denomination (base currency vs quote currency vs contract count)

**Where to do this:** `verify_data.ipynb`

**Pass criteria:** All 5–10 sampled bars match TradingView OHLCV exactly.

---

## Layer 2: Indicators match TradingView

**What you're verifying:** NT's indicator calculations produce the same
values as TradingView's built-in indicators on the same data.

**How:**

1. Pick a bar where the indicator value is unambiguous — not a crossover
   point, just a random bar in the middle of a trend.
2. Compute the indicator from your bars using NT:
   ```python
   from nautilus_trader.indicators import SimpleMovingAverage
   sma = SimpleMovingAverage(25)
   for bar in bars[:target_idx + 1]:
       sma.handle_bar(bar)
   print(f"SMA(25) at bar {target_idx}: {sma.value}")
   ```
3. Also compute it manually with pandas as a cross-check:
   ```python
   closes = pd.Series([float(b.close) for b in bars[:target_idx + 1]])
   print(f"pandas SMA(25): {closes.rolling(25).mean().iloc[-1]}")
   ```
4. Open TradingView → add SMA(25) → hover over the same bar → note the value.
5. All three should match to several decimal places.

**Repeat for every indicator type your strategies use:**
- SMA (for SMACross)
- EMA (for EMACross, EMACrossATR)
- MACD + Signal EMA (for MACDRSI)
- RSI (for MACDRSI)
- ATR (for EMACrossATR)

**Common failure modes:**
- EMA seed: TradingView uses SMA for the first EMA value; verify NT does the same
- RSI calculation: Wilder's smoothing vs standard EMA — they produce different values
- OHLC source: your strategy uses `bar.close`, but TradingView might default to `hlc3`
- Off-by-one: indicator reading at bar N vs bar N-1

**Where to do this:** New `verify_signals.ipynb` notebook (see Layer 4).

**Pass criteria:** NT indicator, pandas manual calculation, and TradingView
all agree within rounding tolerance (< 0.01% difference).

---

## Layer 3: Signals fire on the correct bars

**What you're verifying:** Buy/sell signals in your backtest occur at the
exact same bars where you can visually see crossovers on TradingView.

**How:**

1. Open TradingView with the same instrument, timeframe, and indicator
   settings (e.g. SMA 25 and SMA 30 on BTC 4h).
2. Find 3–4 obvious crossover points where you can see the lines cross
   with your eyes. Note the exact bar timestamp for each.
3. Run a backtest with those params. Generate the HTML report:
   ```python
   from notebooks.charts import generate_backtest_html
   generate_backtest_html(bars, fills, positions, fast_period=25, slow_period=30, ...)
   ```
4. Open the HTML report. Find the same crossover points. The buy/sell
   markers should land on the exact same bars you identified on TradingView.

**What to watch for:**
- Marker is one bar late → possible lookahead bias (signal fires on bar N
  but the condition was true at bar N-1)
- Marker is one bar early → strategy might be using the current bar's close
  to make a decision that should wait for the bar to close
- Missing marker at a crossover → check if `indicators_initialized()` was
  still `False` at that point, or if the strategy has additional filters

**Where to do this:** New `verify_signals.ipynb` notebook (see Layer 4).

**Pass criteria:** All manually identified crossover points have corresponding
trade markers on the correct bar, with zero off-by-one discrepancies.

---

## Layer 4: Automated signal verification notebook

**What you're building:** A `verify_signals.ipynb` notebook that programmatically
confirms Layers 2 and 3 without manual TradingView inspection. Once built,
this is your regression test — re-run it after NT upgrades or strategy changes.

**Notebook structure:**

```
Cell 1: Config
  - Same pattern as validate_strategy.ipynb Cell 1
  - Set instrument, bar type, strategy params to verify

Cell 2: Load data
  - Load bars from catalog

Cell 3: Manual indicator calculation (pandas)
  - Compute SMA/EMA/MACD/RSI from bar closes using pandas
  - Store as a DataFrame with columns: ts, close, fast_ma, slow_ma, ...

Cell 4: NT indicator calculation
  - Instantiate NT indicator objects
  - Feed bars through handle_bar()
  - Store values in a parallel DataFrame

Cell 5: Compare indicators bar-by-bar
  - Merge pandas and NT DataFrames on timestamp
  - Compute absolute difference per bar
  - Assert max difference < tolerance (e.g. 1e-10)
  - Print: "✅ Indicators match" or "❌ N bars diverge"

Cell 6: Identify crossover points from manual calculation
  - Find bars where fast_ma crosses above/below slow_ma
  - Store as list of (timestamp, direction) tuples

Cell 7: Run backtest, extract fill timestamps
  - Run backtest with same params
  - Extract fills report
  - Parse fill timestamps and sides (BUY/SELL)

Cell 8: Compare crossovers to fills
  - For each manually identified crossover, check if a fill exists
    on the same bar (or within 1 bar if execution happens on next bar)
  - For each fill, check if a crossover exists on the same bar
  - Flag any unmatched crossovers or unexpected fills
  - Print: "✅ All signals matched" or "❌ N discrepancies"

Cell 9: Summary
  - Indicator match: ✅/❌
  - Signal match: ✅/❌
  - Unmatched crossovers: list
  - Unexpected fills: list
```

**Do this once per strategy type.** Once SMACross is verified, you trust
the SMA indicator and crossover logic. When you add MACDRSI, verify that
one separately (MACD, RSI, and the combined signal logic).

**Where to put it:** `notebooks/verify_signals.ipynb`

**Pass criteria:** Zero indicator divergences, zero signal mismatches.

---

## Layer 5: Walk-forward spot check

**What you're verifying:** The walk-forward machinery (slicing, warmup,
`score_from_ns`) faithfully wraps `run_single_backtest` — which you've
already verified in Layers 1–4.

**How:**

1. Take a completed walk-forward run. Pick one fold — e.g. Fold 1 with
   params fast=10, slow=200, test window 2023-09-25 → 2024-05-10.
2. Run a standalone backtest on exactly that test window with those params,
   including the warmup padding:
   ```python
   # Reproduce Fold 1's test slice
   test_start = next(i for i, b in enumerate(bars)
                     if pd.Timestamp(b.ts_event, unit="ns", tz="UTC").date()
                     == date(2023, 9, 25))
   warmup = 200
   test_bars = bars[test_start - warmup : test_start + 1369]

   eng = make_engine(VENUE, instrument, test_bars, STARTING_CAPITAL)
   strategy_factory(eng, {"fast": 10, "slow": 200})
   eng.run()
   ```
3. Generate the HTML report from this standalone backtest. Open it.
4. Count the trades. Does the count match the walk-forward's reported
   OOS positions for that fold?
5. Sum the PnL from the positions report, but only for positions opened
   after the `score_from_ns` boundary. Does it match the walk-forward's
   reported OOS PnL?
6. Visually inspect the HTML report against TradingView — do the trades
   make sense? Are the crossover entries where you'd expect?

**What this confirms:**
- Bar slicing is correct (right data in each fold)
- Warmup padding works (indicators are warm, first real signal is on time)
- `score_from_ns` filtering is correct (no warmup trades contaminating OOS)
- The numbers in the walk-forward summary table are real

**Bonus check for the fence-post:**
```python
# After the standalone backtest, check the earliest scored position
pos = eng.cache.position_snapshots() + eng.cache.positions()
score_boundary = bars[test_start].ts_event
scored = [p for p in pos if p.ts_opened >= score_boundary]
if scored:
    earliest = min(p.ts_opened for p in scored)
    print(f"Score boundary:   {score_boundary}")
    print(f"Earliest scored:  {earliest}")
    print(f"Match first test bar: {earliest >= score_boundary}")
```

**Where to do this:** Bottom of `verify_signals.ipynb` or a dedicated section
in `validate_strategy.ipynb`.

**Pass criteria:** Trade count and PnL match the walk-forward output exactly.
Earliest scored position is on or after the test boundary, never before.

---

## Layer 6: Persistence pipeline

**What you're verifying:** `PersistenceActor` writes fills, positions, and
account snapshots to PostgreSQL completely and correctly during paper/live
trading. No dropped events, no data corruption, no float columns.

**How:** Run `verify_persistence.ipynb` after a completed paper trading session.

**What it checks:**
1. Run lifecycle — `strategy_runs` has `stopped_at` populated (graceful shutdown)
2. Fill completeness — no NULLs in required fields, positive prices/quantities,
   no duplicate `client_order_id`s, monotonic timestamps
3. Position completeness — every closed position has PnL, open/close prices,
   `ts_opened < ts_closed`
4. Fill ↔ position cross-reference — fill counts align with position count,
   time ranges are consistent
5. Account snapshots — periodic (~60s), `balance_total == balance_free + balance_locked`,
   no negative balances, no large gaps
6. Data types — all financial columns are PostgreSQL `NUMERIC`, never `float4`/`float8`

**Prerequisites:** A completed paper trading run with graceful shutdown.
PostgreSQL running (`docker compose up -d postgres`).

**Where to do this:** `verify_persistence.ipynb`

**Pass criteria:** All six checks pass. Financial columns are NUMERIC. No
dropped fills or positions. Balance accounting is consistent.

---

## Run order

Run notebooks in this order — each depends on the previous:

| Step | Notebook | What it proves |
|------|----------|----------------|
| 1 | `verify_pipeline.ipynb` | NT can load data, run a strategy, produce fills |
| 2 | `verify_data.ipynb` | OHLCV data matches exchange, no corruption/gaps |
| 3 | `verify_signals.ipynb` | Indicators and trade entries are correct |
| 4 | `verify_persistence.ipynb` | Live fills/positions persist to PostgreSQL correctly |

Layers 5 (walk-forward spot check) is done inside `verify_signals.ipynb` or
`validate_strategy.ipynb`, not as a separate notebook.

---

## Verification schedule

| When | What to re-verify |
|------|-------------------|
| First time | All 6 layers, for each strategy type |
| After NT version upgrade | Layers 2, 3, 4 (indicators and signals may change) |
| After adding a new strategy | Layers 2, 3, 4 for the new strategy's indicators |
| After adding a new instrument | Layer 1 (data integrity for the new instrument) |
| After modifying `engine.py` | Layer 5 (walk-forward machinery) |
| After modifying data pipeline | Layer 1 (data integrity) |
| After modifying a strategy | Layers 3, 4 (signal logic) |
| After modifying `PersistenceActor` | Layer 6 (persistence pipeline) |
| After schema migration | Layer 6 (data types, column presence) |

---

## Future verification (build when needed)

| Notebook | When to build | What it verifies |
|----------|---------------|------------------|
| `verify_streaming.ipynb` | Phase 3b — StreamingActor + WebSocket | Events flow intact: NT → Redis Streams → WebSocket → frontend |
| `verify_live.ipynb` | First live deployment | Actual fill prices vs concurrent backtest on same bars (real slippage measurement) |

---

## Files

| File | Purpose | Status |
|------|---------|--------|
| `notebooks/verify_pipeline.ipynb` | Smoke test — NT loads data, runs, produces fills | ✅ Exists |
| `notebooks/verify_data.ipynb` | Layer 1 — OHLCV integrity, exchange spot-check | ✅ Created |
| `notebooks/verify_signals.ipynb` | Layers 2–5 — indicators, signals, walk-forward spot check | **Create** |
| `notebooks/verify_persistence.ipynb` | Layer 6 — PostgreSQL write completeness and types | ✅ Created |
| `VERIFICATION.md` | This document | ✅ This file |
