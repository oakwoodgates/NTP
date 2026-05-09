# Notebooks

Research notebooks for strategy development, backtesting, and validation.

## Layout

```
notebooks/
  backtest/                — per-strategy backtest + sweep notebooks
                             (ema_cross.ipynb is the v2 reference notebook)
  verify/                  — data-pipeline + signal verification
  compare_sweeps.ipynb     — cross-instrument / cross-timeframe comparison
  validate_strategy.ipynb  — walk-forward, plateau, bootstrap, regime, fee
                             sensitivity, yearly concentration → 8-check
                             go / no-go verdict per (instrument, combo)
  validate_all.ipynb       — consolidator: reads every reports/validate/
                             *_verdict.json and renders a strategy-level
                             comparison matrix + per-check failure-rate
  review_live_run.ipynb    — post-run analysis of live/paper trades
  charts.py                — shared plotting helpers (public)
  utils.py                 — shared notebook utilities (public)
  _compare_helpers.py      — notebook-private helpers for compare_sweeps
                             (build_stability_df, short_sweep_label)
  _validate_helpers.py     — notebook-private helpers for validate_strategy
                             (STRATEGIES registry, make_strategy_factory,
                              get_param_grid, plateau_scores,
                              short_params_tag, parse_pnl, …)
```

The leading-underscore `_module.py` files are **notebook-private** —
not part of the project's public API.  They hold code extracted from
the notebooks to keep cells short, tested in `tests/unit/test_*_helpers.py`
but not imported anywhere outside `notebooks/`.

## Notebook structure convention (v2)

`notebooks/backtest/ema_cross.ipynb` is the canonical reference for
**backtest** notebooks; `compare_sweeps.ipynb` and
`validate_strategy.ipynb` are the canonical references for the
**analysis** workflow notebooks.  All three use the same conventions
(markdown section headers, stable kebab-case cell IDs, all-tuneables-in-
cell-1, save snapshot + scratchpad at the end).  Section structures
below.

### Backtest notebook (per-strategy)

```
# Backtest — <strategy name>          (H1 title + 1-paragraph blurb)

## 1. Setup
### 1.1 Imports & shared config       (one code cell — all tuneables)
### 1.2 Load data + resolve liq config

## 2. Single-config backtest
### 2.1 Configure engine
### 2.2 Subscribe to liquidation events
### 2.3 Add strategy + run
### 2.4 Reports
### 2.5 Run diagnostics
### 2.6 Calculate analyzer stats

## 3. Single-config analysis
### 3.1 NT tearsheet (DISABLED stub)  — see Cell 3.9 for v2 replacement
### 3.2 Price chart with MA overlay + trade markers
### 3.3 Equity & drawdown (event-time)
### 3.4 Summary statistics
### 3.5 Trade distributions
### 3.6 Per-year breakdown
### 3.7 Regime breakdown (ADX-tagged)
### 3.8 Comparison baselines
### 3.9 v2 tearsheet (single-file archive)

## 4. Parameter sweep
### 4.1 Run sweep
### 4.2 PnL heatmap
### 4.3 Liquidation diagnostics
### 4.4 Sortable HTML sweep table

## 5. Interactive trade chart (TVLC)

## 6. Robustness checks
### 6.1 Alt-instrument sanity check
### 6.2 Fee sensitivity

## 7. Save & cleanup
### 7.1 Save notebook snapshot
### 7.2 Cleanup
```

### Compare-sweeps notebook (`compare_sweeps.ipynb`)

```
# Compare Parameter Sweeps                   (H1 + 1-paragraph blurb)

## 1. Setup
### 1.1 Imports & shared config              (filters, PARAM_COLS, RANK_BY)
### 1.2 Load sweeps + filter                 (load_sweeps_filtered)

## 2. Best params per sweep
### 2.1 Best params table                    (v2 metric columns)
### 2.2 Sortable HTML cross-sweep table      (generate_cross_sweep_html)

## 3. Side-by-side PnL heatmaps              (plot_pnl_heatmap, exclude_kinds)

## 4. Parameter stability across sweeps
### 4.1 Stability table                      (per-combo aggregation +
                                              cv_pnl_pct = std/|mean|)
### 4.2 Average PnL% heatmap                 (full-coverage combos)

## 5. Single-sweep deep dive

## 6. Save & cleanup
### 6.1 Save snapshot                        (category="compare")
### 6.2 Cleanup

## 7. Scratchpad
```

### Validate-strategy notebook (`validate_strategy.ipynb`)

```
# Validate Strategy                          (H1 + 1-paragraph blurb)

## 1. Setup
### 1.1 Imports & shared config              (strategy selector, leverage,
                                              filters, OVERRIDE_PARAMS,
                                              fold sizes, bootstrap iters,
                                              snapshot flag)
### 1.2 Load data + sweep                    (load_backtest_data +
                                              load_sweeps_filtered;
                                              picks best by total_pnl_pct
                                              OR uses OVERRIDE_PARAMS)

## 2. Plateau detection
### 2.1 Plateau scoring                      (3×3 neighbour-profitability,
                                              with survival-rate accounting)
### 2.2 Heatmap with liquidated cells flagged

## 3. Walk-forward analysis
### 3.1 Run walk-forward                     (run_walk_forward, train/test pct)
### 3.2 Per-fold results table
### 3.3 In-sample vs OOS chart
### 3.4 Stitched OOS equity curve            (plot_walkforward_oos_equity)

## 4. Single-config performance
### 4.1 Run a single backtest at best params (captures positions, fills,
                                              account_report, trade_pnls)
### 4.2 Price chart with trade markers       (plot_ma_cross)
### 4.3 Equity & drawdown                    (plot_equity_curve)
### 4.4 Per-year breakdown                   (performance_by_year)
### 4.5 Trade distributions                  (plot_trade_distributions)

## 5. Bootstrap analysis
### 5.1 Bootstrap PnL CI                     (bootstrap_total_pnl, with
                                              IID caveat)
### 5.2 Bootstrap PnL distribution chart     (plot_bootstrap_pnl)
### 5.3 Bootstrap max-drawdown CI            (bootstrap_max_drawdown +
                                              plot_bootstrap_drawdown)

## 6. Rolling performance                    (rolling_performance — splits
                                              active vs inactive windows)

## 7. Fee sensitivity                        (run_fee_sweep,
                                              plot_fee_sensitivity)

## 8. Regime breakdown                       (tag_regimes,
                                              performance_by_regime,
                                              + Wilson CI on win-rate)

## 9. Go / no-go assessment                  (print_validation_verdict —
                                              8 checks: plateau, walk-forward,
                                              param-stability, bootstrap,
                                              rolling, fee, regime, yearly
                                              concentration.  Persists JSON
                                              to reports/validate/.)

## 10. Save & cleanup
### 10.1 Save snapshot                       (category="validate")
### 10.2 Cleanup

## 11. Scratchpad
```

### Validate-all notebook (`validate_all.ipynb`)

```
# Validate All — strategy-level verdict matrix  (H1 + 1-paragraph blurb)

## 1. Setup
### 1.1 Imports & filters                    (FILTER_INSTRUMENT,
                                              FILTER_INTERVAL,
                                              LATEST_PER_PICK)
### 1.2 Load all verdict JSONs               (load_verdict_jsons)

## 2. Comparison matrix                      (build_verdict_matrix —
                                              row per (instrument, pick),
                                              cols are check icons)

## 3. Per-check failure rate across runs     (% red flag per check, sorted)

## 4. Single-run drill-down                  (DRILL_INDEX picker)

## 5. Save & cleanup
### 5.1 Save snapshot                        (category="validate_all")
### 5.2 Cleanup

## 6. Scratchpad
```

### Conventions

- **Markdown cells carry the structure**, not numbered comments.
  Jupyter's table-of-contents extension auto-builds nav from `## H2` /
  `### H3`.
- **Each code cell has a markdown header above it** explaining what it
  does in 1–2 sentences.  Don't restate the obvious; do flag non-obvious
  semantics ("event-time, NOT daily MTM").
- **Stable kebab-case cell IDs** — `id="run-backtest"`, not auto-generated
  hex.  These help diff/merge tools when the notebook is edited
  collaboratively.
- **One concept per cell.**  If a cell needs a sub-heading inside it,
  consider splitting.
- **No "Cell N:" comments inside code cells.**  They go stale instantly
  when cells are added/removed.
- **Notebook-private helpers go in `_<notebook>_helpers.py`.**
  Functions extracted purely to keep cells short — not reusable across
  notebooks — live in a `_module.py` (leading underscore = private)
  next to the notebook that uses them.  Tested in
  `tests/unit/test_<notebook>_helpers.py`.  Truly reusable helpers go
  in `notebooks/utils.py` or `notebooks/charts.py` (no prefix).
- **Suppress Jupyter auto-display of return values with a trailing
  semicolon.**  `print_validation_verdict(...)` and
  `save_notebook_snapshot(...)` both return values that aren't useful
  to display in cell output (already printed in formatted form +
  written to disk).  End the call with `;` to suppress the auto-echo.

## File naming conventions

| Generator | Output | Behavior |
|---|---|---|
| `run_sweep` | `data/sweeps/{SWEEP_NAME}.parquet` | overwrites on re-run |
| `generate_sweep_html` | `reports/sweeps/{SWEEP_NAME}_sweep.html` | overwrites on re-run |
| `generate_cross_sweep_html` | `reports/sweeps/{filename}.html` | overwrites on re-run |
| `generate_backtest_html` (TVLC) | `reports/charts/{RESULT_NAME}_chart_{ts}.html` | snapshot, accumulates |
| `generate_v2_tearsheet` | `reports/tearsheets/{RESULT_NAME}_tearsheet_{ts}.html` | snapshot, accumulates |
| `save_notebook_snapshot` | `reports/notebooks/<category>/{RESULT_NAME}_{ts}.ipynb` + `.html` | snapshot, accumulates |
| `print_validation_verdict` (with `verdict_path=`) | `reports/validate/{RESULT_NAME}_verdict.json` | overwrites per RESULT_NAME |

### `RESULT_NAME` skeleton

Both backtest and validate notebooks build `RESULT_NAME` from the
same skeleton:

```
{prefix}_{strategy}_{ASSET}_{EXEC_VENUE}_{interval}[_{params_tag}]
```

| Notebook | prefix | Example |
|---|---|---|
| Backtest (per-strategy) | `(none)` | `MACross-EMA_BTC_HYPERLIQUID_PERP_1d_f10_s40` |
| Validate (auto-pick) | `validate_` | `validate_MACross-EMA_BTC_HYPERLIQUID_PERP_1d` |
| Validate (override) | `validate_` | `validate_MACross-EMA_BTC_HYPERLIQUID_PERP_1d_f10_s20` |
| Validate-all | `(none)` | `validate_all` |

The `params_tag` uses **first-letter-of-each-word compaction**:
`fast` → `f`, `slow` → `s`, `bb_period` → `bp`, `bb_std` → `bs`,
`dc_period` → `dp`, `atr_sl` → `as`.  See `_validate_helpers.short_param_key`
(asserted unique within every registered strategy's grid).

## Strategy validation workflow

The full per-strategy validation flow has three stages and writes
into three different `reports/` subtrees.

### 1. Sweep + compare (across instruments)

```bash
# In each backtest notebook (e.g. notebooks/backtest/ema_cross.ipynb):
#   Run All → run_sweep() writes data/sweeps/{strategy}_{instr}_{interval}.parquet

# Then in compare_sweeps.ipynb:
#   Run All → reads every sweep parquet, surfaces:
#     - Best per sweep (cross-instrument-fair via total_pnl_pct rank)
#     - Sortable HTML cross-sweep table (DataTables.js)
#     - Cross-sweep robust combos: profitable across all instruments
#       (low cv_pnl_pct = stable; high = sign-flipping)
```

### 2. Validate (per-instrument)

For each instrument you want to validate, edit cell 1.1 of
`validate_strategy.ipynb`:

```python
ASSET = "BTC"            # or "ETH", "SOL", ...
OVERRIDE_PARAMS = None   # auto-pick by total_pnl_pct
# or:
OVERRIDE_PARAMS = {"fast": 10, "slow": 20}   # validate a specific combo
```

Run All → 8-check verdict prints to cell output AND drops a JSON to
`reports/validate/{RESULT_NAME}_verdict.json`.  The `RESULT_NAME` tag
distinguishes auto-pick from override runs.

Common pattern: validate the **per-instrument best** (auto-pick) AND
the **cross-instrument robust pick** (override) on each instrument.
That's 2N runs for N instruments — for BTC/ETH/SOL it's 6 runs total.

### 3. Validate-all (across instruments + picks)

`validate_all.ipynb` reads every `reports/validate/*_verdict.json` and
renders a comparison matrix (one row per run, columns are check
icons) plus a per-check failure-rate analysis.  Use this to answer
strategy-level questions:

- *"Does the cross-sweep robust pick beat per-instrument bests on
  every instrument I tested?"*
- *"Which check is the most consistent red flag — strategy-level
  signal vs instrument-specific noise?"*
- *"Which combo passes on more instruments than others?"*

Re-runs are cheap (no compute — just reads JSONs).  Snapshot lands in
`reports/notebooks/validate_all/`.

## Snapshotting a notebook run

Two paths, both supported.

### Option A — Single-click "Run All" (interactive)

Click "Run All" in your editor.  The notebook's section 7.1 cell calls
``save_notebook`` + ``save_notebook_html`` at the end and writes a
snapshot to `reports/notebooks/<category>/{RESULT_NAME}_snapshot.ipynb`
+ `reports/html/<category>/{RESULT_NAME}_snapshot.html`.

**Required setting:** your editor must autosave cells as they finish.
Otherwise the save cell reads a stale on-disk file (cells aren't
flushed yet) and you get an empty/old snapshot.

- **VS Code / Cursor:** Settings → search `files.autoSave` → set to
  `afterDelay`.  Default 1000ms is fine.
- **JupyterLab:** autosave is enabled by default (every 2 minutes —
  bump the frequency in advanced settings for short runs).
- **Classic Jupyter:** autosave on (every 2 minutes default).

**Caveat:** the snapshot captures every cell's output *except* the
save cell's own "Saved → ..." message (the kernel can't autosave a
cell while it's running).  The kernel still printed the message in
your editor — only the .ipynb / .html on disk lacks it.  Acceptable
trade-off for a single Run All workflow.

### Option B — Headless via wrapper script (CI / reproducibility)

After (or instead of) an interactive run, drop to a terminal:

**Bash / Git Bash / WSL:**
```bash
./scripts/snapshot-notebook.sh notebooks/backtest/ema_cross.ipynb
```

**PowerShell:**
```powershell
.\scripts\snapshot-notebook.ps1 notebooks\backtest\ema_cross.ipynb
```

The wrapper re-executes the notebook from a **fresh kernel** via
`jupyter nbconvert --execute` and writes timestamped snapshots to:

```
reports/notebooks/<category>/<basename>_<UTC_TIMESTAMP>.ipynb   (executed copy)
reports/html/<category>/<basename>_<UTC_TIMESTAMP>.html         (rendered HTML)
```

Differences from Option A:

- ✅ Captures the save cell's own output too (no in-notebook save)
- ✅ Reproducible — fresh kernel, no in-memory state from prior runs
- ✅ The path to use for CI / scheduled jobs / shared snapshots
- ❌ Adds 1–2 minutes (full re-run) — Option A reuses the kernel state

### Which one should I use?

| Situation | Use |
|---|---|
| Day-to-day "did my change improve the strategy?" | A (Run All) |
| Sharing a result with a colleague / archive of a milestone | B (script) |
| CI / scheduled job | B (script) |
| Notebook completes in <30s and you want max iteration speed | A |
| You hit a "stale snapshot" problem with A | B

## ⚠️ Avoiding the "jumbled cells" problem

When external tooling (Python scripts, Claude Code, etc.) edits a notebook
file while Jupyter or VS Code has it open, Jupyter's autosave can
**clobber your edits** by writing its in-memory copy back over the changed
file, sometimes merging cell ranges incorrectly.

**The fix:**

> **Close the notebook in your editor before running notebook-modifying
> scripts.**  Reopen and restart the kernel after the script has finished.

This is the only 100% reliable way to avoid the race.  Stable cell IDs
help diff/merge tools recover from collisions but don't prevent the race.

## Running a notebook from a worktree

The project's editable install (`pip install -e .`) hardcodes `src` to
the directory where it was last installed.  When you run a notebook from
a worktree, `src` may resolve to the *main* project's source tree, not
the worktree's.  Two ways to fix:

1. **Quick (all worktrees share one venv).**  Re-point the editable
   install at the worktree:

   ```bash
   cd .claude/worktrees/<branch>
   <venv>/bin/pip install -e .
   ```

   When done, switch back: `cd <main project> && pip install -e .`.

2. **Clean (worktree has its own venv).**  Inside the worktree:

   ```bash
   python -m venv .venv
   .venv/bin/pip install -e .[dev]
   ```

   Then point Jupyter / VS Code at the worktree's `.venv` for the kernel.

## See also

- [`docs/ANALYZER_RETURNS_CAVEAT.md`](../docs/ANALYZER_RETURNS_CAVEAT.md) —
  why Sharpe / Sortino / Volatility are deliberately suppressed.
- [`docs/BAR_BACKTESTING_GOTCHAS.md`](../docs/BAR_BACKTESTING_GOTCHAS.md) —
  bar-data quirks (MIT/LIT trigger, no margin enforcement, etc.).
- [`docs/LIQUIDATION_AND_SIZING.md`](../docs/LIQUIDATION_AND_SIZING.md) —
  the in-project liquidation simulator.
