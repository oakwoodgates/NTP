# Notebooks

Research notebooks for strategy development, backtesting, and validation.

## Layout

```
notebooks/
  backtest/           — per-strategy backtest + sweep notebooks
                        (ema_cross.ipynb is the v2 reference notebook)
  verify/             — data-pipeline + signal verification
  compare_sweeps.ipynb    — cross-instrument / cross-timeframe comparison
  validate_strategy.ipynb — walk-forward, plateau, bootstrap validation
  review_live_run.ipynb   — post-run analysis of live/paper trades
  charts.py           — shared plotting helpers
  utils.py            — shared notebook utilities
```

## Notebook structure convention (v2)

`notebooks/backtest/ema_cross.ipynb` is the canonical reference.  When
adding a new backtest notebook (or migrating an existing one), follow
this structure:

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
### 4.1 Spotlight params guide
### 4.2 Run sweep
### 4.3 PnL heatmap
### 4.4 Liquidation diagnostics
### 4.5 Sortable HTML sweep table

## 5. Interactive trade chart (TVLC)

## 6. Robustness checks
### 6.1 Alt-instrument sanity check
### 6.2 Fee sensitivity

## 7. Save & cleanup
### 7.1 Save notebook snapshot
### 7.2 Cleanup
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

## File naming conventions

For HTML reports the notebook generates:

| Generator             | Output                                              | Behavior              |
|-----------------------|-----------------------------------------------------|-----------------------|
| `run_sweep`           | `data/sweeps/{SWEEP_NAME}.parquet`                  | overwrites on re-run  |
| `generate_sweep_html` | `reports/sweeps/{SWEEP_NAME}_sweep.html`            | overwrites on re-run  |
| `generate_backtest_html` (TVLC) | `reports/charts/{RESULT_NAME}_chart_{ts}.html` | snapshot, accumulates |
| `generate_v2_tearsheet` | `reports/tearsheets/{RESULT_NAME}_tearsheet_{ts}.html` | snapshot, accumulates |

`SWEEP_NAME` and `RESULT_NAME` are derived in Cell 1.1 — see the comment
block above the assignments for the convention.

## Snapshotting a notebook run

Two distinct workflows: **interactive exploration** and **producing a
shareable snapshot**.

### 1. Interactive exploration

Open the notebook in VS Code / Cursor / JupyterLab and click "Run All"
(or step through cells).  Inline outputs appear in the editor.  The
"snapshot" cell at the end of the notebook (section 7.1 in the v2
template) is documentation only — it doesn't try to save anything,
because save-from-inside-the-notebook hits a chicken-and-egg race
(cells aren't autosaved to disk until *after* they've executed, but
the save cell needs to run *during* execution).

### 2. Snapshot for sharing / archiving

After you're happy with an interactive run, drop to a terminal and run:

**Bash / Git Bash / WSL:**
```bash
./scripts/snapshot-notebook.sh notebooks/backtest/ema_cross.ipynb
```

**PowerShell:**
```powershell
.\scripts\snapshot-notebook.ps1 notebooks\backtest\ema_cross.ipynb
```

The wrapper re-executes the notebook headless via
`jupyter nbconvert --execute` and writes timestamped snapshots to:

```
reports/notebooks/<category>/<basename>_<UTC_TIMESTAMP>.ipynb   (executed copy)
reports/html/<category>/<basename>_<UTC_TIMESTAMP>.html         (rendered HTML)
```

Where `<category>` is the parent directory name of the input notebook
(so `notebooks/backtest/ema_cross.ipynb` → `category=backtest`).

The headless run takes 1–2 minutes for the v2 reference notebook
(~27 code cells, sweep + tearsheet generation).  In exchange you get a
real shareable artifact instead of the empty-snapshot trap.

### Why two workflows instead of one?

`nbconvert --execute` is a separate process from your editor's kernel.
It manages its own kernel start/stop and writes the notebook atomically
when execution completes — so every cell's output is captured.

An in-notebook save cell, by contrast, has to call `shutil.copy()` on
the .ipynb file mid-execution, when the on-disk copy hasn't been
autosaved yet.  Result: the snapshot has whatever was on disk *before*
you clicked Run All — usually the previous run's outputs or a blank
notebook.

The two-workflow split makes the right thing easy and the wrong thing
unavailable.  If you want headless reproducible runs (CI, scheduled
jobs), the same wrapper script is what you'd call.

## ⚠ Avoiding the "jumbled cells" problem

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
