# Batch Backtest Runner

`scripts/batch_backtest.py` runs a fixed cross-product of `(asset, interval, stop_pct)`
combos overnight, produces all the artifacts you'd review interactively
in `notebooks/backtest/ma_cross.ipynb`, and writes a master index linking
them together. Use it when you want N runs done by morning, not when
you're exploring one config interactively.

## When to use which

| Want to... | Use |
|---|---|
| Explore one specific config interactively, eyeball charts, iterate | `notebooks/backtest/ma_cross.ipynb` (or specialised variant) |
| Run a fixed grid of configs and review afterwards | `scripts/batch_backtest.py` |
| Validate one combo's robustness across 8 checks | `notebooks/validate_strategy.ipynb` |
| Compare sweep results across instruments / timeframes | `notebooks/compare_sweeps.ipynb` |

## Quick start

```
python scripts/batch_backtest.py
```

Defaults from `src/config/settings.py`: BTC/ETH/SOL × 4h/1d × 5%/10% stop,
EMA `MA_TYPE`, fast=10 / slow=40 single-config plus full 12×12 grid sweep.
12 combos, ~14 minutes wall-clock.

## CLI

```
--assets BTC ETH SOL              # default from settings.default_assets
--intervals 1d 4h                  # default from settings.default_intervals
--stop-pcts 0.05 0.10              # default from [settings.default_stop_pct]
--ma-type EMA                      # one of: EMA SMA HMA DEMA AMA VIDYA
--fast-ma 10                       # primary single-config fast period
--slow-ma 40                       # primary single-config slow period
--data-source BINANCE_PERP         # default from settings.data_source
--exec-venue HYPERLIQUID_PERP      # default from settings.exec_venue
--starting-capital 1000            # default from settings.starting_capital
--leverage 20                      # default from settings.leverage
--catalog-path data/catalog        # default = project root + /data/catalog
--dry-run                          # list combos and exit; no execution
```

CLI flags > settings > class defaults. So `STARTING_CAPITAL=500 python
scripts/batch_backtest.py` works (env-var override), and `python
scripts/batch_backtest.py --starting-capital 500` works (CLI override).

## What you get per combo

For each `(asset, interval, stop)` combo, the script produces:

- **TVLC interactive chart** at `--fast-ma`/`--slow-ma` (single-config
  backtest):
  `reports/charts/{run_id}/{strategy}_{asset}_{venue}_{interval}_stop{N}_chart_*.html`
- **v2 tearsheet** for the same single-config:
  `reports/tearsheets/{run_id}/{strategy}_{asset}_{venue}_{interval}_stop{N}_tearsheet_*.html`
- **Sweep parquet** (full fast × slow grid):
  `data/sweeps/{run_id}/{strategy}_{asset}_{venue}_{interval}_stop{N}.parquet`
- **PnL heatmap PNG** for the sweep:
  `reports/sweeps/{run_id}/{strategy}_{asset}_{venue}_{interval}_stop{N}_heatmap.png`
- **Sortable sweep HTML** with the heatmap embedded below the table:
  `reports/sweeps/{run_id}/{strategy}_{asset}_{venue}_{interval}_stop{N}.html`

After all combos finish:

- **Master index HTML** linking every artifact, sorted by profit factor:
  `reports/batch/{run_id}/index.html`
- **Incremental results JSON** (written after each combo so a mid-run
  crash doesn't lose progress):
  `reports/batch/{run_id}/results.json`

`{run_id}` is a UTC timestamp (e.g. `20260510_182838`) — different
invocations don't collide.

## Reading the master index

The table is sorted by profit factor descending. For each combo you
see:

| Column | Meaning |
|---|---|
| Asset / Interval / Stop | The combo identifier |
| Trades | Total closed positions |
| PnL | Total realized PnL (USDC) |
| Win Rate | Closed positions where realized_pnl > 0 |
| PF | Profit Factor: gross_wins / abs(gross_losses) |
| Max DD | Maximum drawdown as % of running peak equity |
| Stops/Strat | Count split: protective_stop closes vs strategy_exit closes |
| Sweep n | How many param combos in the sweep |
| Best PnL | Best total_pnl across the sweep grid |
| Artifacts | Click-through links to chart / tear / sweep |

**Treat `total_pnl ≤ −starting_capital` rows as "wiped out", not as
additional loss magnitude.** Bar-only backtest fills can produce
single-trade losses past the nominal stop on gappy bars (see
[`BAR_BACKTESTING_GOTCHAS.md`](BAR_BACKTESTING_GOTCHAS.md)). The exact
magnitude past zero is bar-fill noise; the information value is "this
combo died."

## What the script doesn't do (use the notebook for these)

- **Notebook snapshots / HTML exports** — these are inherently
  notebook-derived (`save_notebook_snapshot()` reads the `.ipynb` file
  on disk). The batch runner doesn't have a notebook, so no snapshot.
  If you need that, run via `notebooks/backtest/ma_cross.ipynb`
  manually.
- **Walk-forward analysis** — different concept (sliding-window
  train/test). Use `run_walk_forward()` directly.
- **Random-entry baselines, regime breakdown, bootstrap CIs** — these
  are in the v2 tearsheet which the script does generate; if you want
  them as standalone outputs use the notebook.

## Strategies supported

Currently MACross only (the script imports `src.strategies.ma_cross.MACross`
directly). Adding BBMeanRev / DonchianBreakout / MACDRSI is a
straightforward extension — pull strategy + grid imports from the
right module based on `--strategy`. Mechanical, not done yet.

## Troubleshooting

**`KeyError` on first combo.** Catalog probably doesn't have data for
that asset/interval. Verify with:

```
ls data/catalog/data/bar | grep {asset}USDT
```

**Master-index links 404.** Should not happen post-PR #18. If it does,
check `os.path.relpath` is computing correctly from the index file's
directory to the artifact.

**`gh pr` complains about missing branch.** Unrelated to the runner;
see [`CONFIG.md`](CONFIG.md) for branch hygiene.

**Single-trade loss exceeds nominal stop magnitude.** Bar-fill
artifact, not a runner bug. See [`BAR_BACKTESTING_GOTCHAS.md`](BAR_BACKTESTING_GOTCHAS.md)
section on triggered orders against gappy bars.
