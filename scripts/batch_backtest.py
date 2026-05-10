"""Headless batch runner for MACross backtests across an instrument/interval/stop grid.

Use this when you want to run a fixed cross-product of configurations
overnight and come back to a directory of artifacts.  For interactive,
single-config exploration use ``notebooks/backtest/ma_cross.ipynb``.

What you get per (asset, interval, stop) combo
----------------------------------------------

1. **Single-config backtest** at ``fast=FAST_MA``, ``slow=SLOW_MA``:
   - TVLC interactive chart  ->  ``reports/charts/{run_dir}/...html``
   - v2 tearsheet            ->  ``reports/tearsheets/{run_dir}/...html``

2. **Parameter sweep** over the fast x slow grid:
   - Sweep parquet           ->  ``data/sweeps/{run_dir}/...parquet``
   - PnL heatmap PNG         ->  ``reports/sweeps/{run_dir}/heatmap_*.png``
   - Sortable sweep HTML
     (with the heatmap embedded above the table)
                              ->  ``reports/sweeps/{run_dir}/...html``

After all combos finish, a top-level ``reports/batch/{run_dir}/index.html``
links every artifact and shows a summary table sorted by profit factor.

CLI
---

::

    python scripts/batch_backtest.py \\
        --assets BTC ETH SOL \\
        --intervals 1d 4h \\
        --stop-pcts 0.05 0.10

Defaults match the typical MACross EMA 10/40 setup at 20x leverage.
``--dry-run`` lists the combos without running anything.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decimal import Decimal

# Headless matplotlib — must happen before any pyplot import elsewhere.
import matplotlib

matplotlib.use("Agg")  # noqa: E402


# Make ``notebooks/`` importable so we can reuse charts.py + utils.py
# (the same code paths the notebooks use, no duplication).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "notebooks"))

import charts  # type: ignore[import-not-found]  # noqa: E402, I001
import utils  # type: ignore[import-not-found]  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.identifiers import Venue  # noqa: E402

from src.backtesting import make_engine, run_sweep  # noqa: E402
from src.backtesting.engine import resolve_strategy_liquidation_config  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.core import (  # noqa: E402
    LiquidationConfig,
    TOPIC_ACCOUNT_LIQUIDATED,
    TOPIC_POSITION_LIQUIDATED,
    bar_type_str,
    get_venue_config,
)
from src.strategies.ma_cross import (  # noqa: E402
    MA_FAST_GRIDS,
    MA_SLOW_GRIDS,
    MA_SPOTLIGHTS,
    MACross,
    MACrossConfig,
)

# Strategy-pick defaults — only the values that aren't yet captured in
# settings (these are MA-cross-specific picks for the *single-config*
# backtest part of each combo; the sweep iterates over the grid).
DEFAULT_MA_TYPE = "EMA"
DEFAULT_FAST_MA = 10
DEFAULT_SLOW_MA = 40


# ── Per-combo run record ─────────────────────────────────────────────────


@dataclass
class ComboResult:
    """Everything the master summary needs to know about one combo."""
    asset: str
    interval: str
    stop_pct: float
    instrument_id: str
    n_trades: int
    total_pnl: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float
    n_protective_stop: int
    n_strategy_exit: int
    sweep_combos: int
    sweep_best_pnl: float
    chart_path: Path | None = None
    tearsheet_path: Path | None = None
    sweep_html_path: Path | None = None
    heatmap_path: Path | None = None
    error: str | None = None
    duration_s: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


# ── Single-combo runner ──────────────────────────────────────────────────


def run_combo(
    *,
    asset: str,
    interval: str,
    stop_pct: float,
    data_source: str,
    exec_venue: str,
    ma_type: str,
    fast_ma: int,
    slow_ma: int,
    fast_grid: list[int],
    slow_grid: list[int],
    starting_capital: int,
    trade_notional: Decimal,
    leverage: int,
    catalog_path: str,
    out_dirs: dict[str, Path],
) -> ComboResult:
    """Run single-config backtest + sweep for one combo. Returns ComboResult."""
    started = time.time()
    instrument_id = utils.make_instrument_id(asset, data_source)
    bar_t_str     = bar_type_str(instrument_id, interval)
    sweep_name    = f"{ma_type}_{asset}_{exec_venue}_{interval}_stop{int(stop_pct * 100)}"

    print(f"\n=== {asset} {interval} stop={stop_pct:.0%}  →  {sweep_name} ===")

    venue_cfg = get_venue_config(exec_venue)
    venue     = Venue(get_venue_config(data_source).nt_venue)

    # 1. Load data once for both runs.
    instrument, bars = utils.load_backtest_data(
        catalog_path=catalog_path,
        instrument_id=instrument_id,
        bar_type_str=bar_t_str,
    )
    if not bars:
        return _empty_result(asset, interval, stop_pct, instrument_id,
                             error="no bars in catalog")

    settings = get_settings()
    liq_cfg = LiquidationConfig(
        enabled=settings.liquidation_enabled,
        halt_on_account_liquidation=settings.liquidation_enabled,
        min_trade_notional=settings.liquidation_min_trade_notional,
    )
    liq_resolved = resolve_strategy_liquidation_config(liq_cfg, venue_cfg, instrument)

    bar_type = BarType.from_str(bar_t_str)

    # ── Single-config: chart + tearsheet ────────────────────────────────
    eng = make_engine(
        venue=venue, instrument=instrument, bars=bars,
        starting_capital=starting_capital, leverage=leverage,
        venue_config=venue_cfg, liquidation=liq_cfg,
    )
    position_liqs: list[Any] = []
    account_liqs: list[Any] = []
    eng.kernel.msgbus.subscribe(topic=TOPIC_POSITION_LIQUIDATED, handler=position_liqs.append)
    eng.kernel.msgbus.subscribe(topic=TOPIC_ACCOUNT_LIQUIDATED, handler=account_liqs.append)
    eng.add_strategy(MACross(MACrossConfig(
        instrument_id=instrument.id, bar_type=bar_type,
        ma_type=ma_type, fast_period=fast_ma, slow_period=slow_ma,
        trade_notional=trade_notional,
        liquidation=liq_resolved,
        stop_pct=stop_pct or None,
    )))
    eng.run()

    fills        = eng.trader.generate_order_fills_report()
    positions_rp = eng.trader.generate_positions_report()
    account_rp   = eng.trader.generate_account_report(venue)
    positions    = eng.cache.position_snapshots() + eng.cache.positions()
    exits        = utils.classify_position_exits(positions, eng)
    acct_liq_evt = utils.find_account_liq_culprit(account_liqs, positions, account_rp)

    chart_path = charts.generate_backtest_html(
        bars, fills, positions_rp,
        fast_period=fast_ma, slow_period=slow_ma, ma_type=ma_type,
        instrument_label=str(instrument.id), bar_label=interval,
        starting_capital=float(starting_capital),
        result_filename=f"{sweep_name}_chart",
        exit_classification=exits,
        account_liq_event=acct_liq_evt,
    )
    # generate_backtest_html writes to reports/charts; move to per-run dir.
    chart_path = _move_into(chart_path, out_dirs["charts"])

    tearsheet_path = charts.generate_v2_tearsheet(
        positions=positions, account_report=account_rp, bars=bars,
        starting_capital=float(starting_capital), currency="USDC",
        instrument_label=str(instrument.id), bar_interval=interval,
        strategy_label=f"{ma_type}Cross({fast_ma}/{slow_ma}) stop={stop_pct:.0%}",
        leverage=leverage,
        fee_rate=float(venue_cfg.taker_fee),
        output_dir=out_dirs["tearsheets"],
        filename=f"{sweep_name}_tearsheet",
        exit_classification=exits,
        account_liq_event=acct_liq_evt,
    )

    # Stats for the master index.
    n_trades = sum(1 for p in positions if p.is_closed)
    closed = [p for p in positions if p.is_closed]
    pnls = [float(p.realized_pnl.as_decimal()) for p in closed if p.realized_pnl]
    total_pnl = sum(pnls)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = (len(wins) / len(pnls)) if pnls else 0.0
    gross_loss = abs(sum(losses))
    pf = (sum(wins) / gross_loss) if gross_loss else (None if not wins else float("inf"))
    if not account_rp.empty:
        eq = account_rp["total"].astype(float)
        peak = eq.cummax()
        max_dd_pct = float(((peak - eq) / peak).max()) if (peak > 0).any() else 0.0
    else:
        max_dd_pct = 0.0
    n_pstop = int((exits["close_cause"] == "protective_stop").sum()) if not exits.empty else 0
    n_strat = int((exits["close_cause"] == "strategy_exit").sum()) if not exits.empty else 0

    eng.dispose()

    # ── Sweep ─────────────────────────────────────────────────────────────
    eng2 = make_engine(
        venue=venue, instrument=instrument, bars=bars,
        starting_capital=starting_capital, leverage=leverage,
        venue_config=venue_cfg, liquidation=liq_cfg,
    )
    combos = [
        {"fast": f, "slow": s} for f in fast_grid for s in slow_grid if f < s
    ]

    def factory(engine: Any, params: dict[str, Any]) -> None:
        engine.add_strategy(MACross(MACrossConfig(
            instrument_id=instrument.id, bar_type=bar_type,
            ma_type=ma_type, fast_period=params["fast"], slow_period=params["slow"],
            trade_notional=trade_notional,
            liquidation=liq_resolved,
            stop_pct=stop_pct or None,
        )))

    sweep_df = run_sweep(
        venue=venue, instrument=instrument, bars=bars,
        starting_capital=starting_capital, leverage=leverage,
        param_combos=combos, strategy_factory=factory,
        strategy_name=f"{ma_type}Cross", instrument_id=instrument_id,
        bar_interval=interval, sweep_name=sweep_name,
        sweep_dir=out_dirs["sweeps_data"],
        venue_config=venue_cfg, liquidation=liq_cfg,
        verbose=False,
    )
    eng2.dispose()

    sweep_best_pnl = float(sweep_df["total_pnl"].max()) if not sweep_df.empty else 0.0

    # Heatmap PNG.
    heatmap_path = out_dirs["sweeps_html"] / f"{sweep_name}_heatmap.png"
    charts.plot_pnl_heatmap(
        sweep_df,
        row_col="slow",
        col_col="fast",
        title=f"PnL — {ma_type}Cross  {asset} {interval}  stop={stop_pct:.0%}",
        save_to=heatmap_path,
        show=False,
    )

    # Sweep HTML with the heatmap embedded.
    sweep_html_path = charts.generate_sweep_html(
        sweep_df,
        output_dir=out_dirs["sweeps_html"],
        filename=sweep_name,
        title=f"{ma_type}Cross  {asset} {interval}  stop={stop_pct:.0%}",
        heatmap_path=heatmap_path,
    )

    elapsed = time.time() - started
    print(f"  done in {elapsed:.1f}s — {n_trades} trades, total_pnl={total_pnl:,.0f}, "
          f"sweep best={sweep_best_pnl:,.0f}")

    return ComboResult(
        asset=asset, interval=interval, stop_pct=stop_pct,
        instrument_id=instrument_id, n_trades=n_trades,
        total_pnl=total_pnl, win_rate=win_rate,
        profit_factor=pf, max_drawdown_pct=max_dd_pct,
        n_protective_stop=n_pstop, n_strategy_exit=n_strat,
        sweep_combos=len(combos), sweep_best_pnl=sweep_best_pnl,
        chart_path=chart_path, tearsheet_path=tearsheet_path,
        sweep_html_path=sweep_html_path, heatmap_path=heatmap_path,
        duration_s=elapsed,
    )


def _empty_result(
    asset: str,
    interval: str,
    stop_pct: float,
    instrument_id: str,
    *,
    error: str,
) -> ComboResult:
    return ComboResult(
        asset=asset, interval=interval, stop_pct=stop_pct,
        instrument_id=instrument_id,
        n_trades=0, total_pnl=0.0, win_rate=0.0, profit_factor=None,
        max_drawdown_pct=0.0, n_protective_stop=0, n_strategy_exit=0,
        sweep_combos=0, sweep_best_pnl=0.0, error=error,
    )


def _move_into(src: Path, dest_dir: Path) -> Path:
    """Move ``src`` into ``dest_dir/`` keeping the filename. Returns new path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.rename(dest)
    return dest


# ── Master index ─────────────────────────────────────────────────────────


def write_master_index(results: list[ComboResult], out_path: Path, run_id: str) -> None:
    """Write a top-level summary HTML linking every per-combo artifact."""
    import os  # noqa: PLC0415 — local import keeps this helper self-contained
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Use os.path.relpath so the link from index.html resolves correctly
    # to artifacts under reports/{charts,tearsheets,sweeps}/<run_id>/...,
    # regardless of the directory layout.  The previous implementation
    # hard-coded "../../../{rel}" which dropped the leading "reports/"
    # segment and broke every link.
    def _link(p: Path | None, label: str) -> str:
        if p is None:
            return "—"
        rel = os.path.relpath(p.resolve(), out_path.parent.resolve())
        # Use forward slashes so it works in browsers on every OS.
        return f'<a href="{rel.replace(os.sep, "/")}">{label}</a>'

    rows = []
    # Sort by profit-factor desc, treating None as 0.
    for r in sorted(results, key=lambda x: (x.profit_factor or 0.0), reverse=True):
        cls = "good" if r.total_pnl > 0 else "bad" if r.total_pnl < 0 else ""
        pf_str = (
            "—" if r.profit_factor is None else
            "∞" if r.profit_factor == float("inf") else
            f"{r.profit_factor:.2f}"
        )
        rows.append(f"""<tr class="{cls}">
  <td>{r.asset}</td>
  <td>{r.interval}</td>
  <td class="num">{r.stop_pct:.0%}</td>
  <td class="num">{r.n_trades}</td>
  <td class="num">{r.total_pnl:,.0f}</td>
  <td class="num">{r.win_rate:.1%}</td>
  <td class="num">{pf_str}</td>
  <td class="num">{r.max_drawdown_pct:.1%}</td>
  <td class="num">{r.n_protective_stop} / {r.n_strategy_exit}</td>
  <td class="num">{r.sweep_combos}</td>
  <td class="num">{r.sweep_best_pnl:,.0f}</td>
  <td>{_link(r.chart_path, "chart")} · {_link(r.tearsheet_path, "tear")} · {_link(r.sweep_html_path, "sweep")}</td>
</tr>""")

    total = len(results)
    finished = sum(1 for r in results if r.error is None)
    failed = total - finished
    total_time = sum(r.duration_s for r in results)

    html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Batch backtest — {run_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f1116; color: #d1d4dc; margin: 24px; }}
  h1 {{ color: #fff; font-size: 18px; }}
  .meta {{ color: #888; font-size: 12px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #2a2d36; }}
  th {{ background: #1a1d24; color: #fff; text-align: left; font-weight: 600; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.good td {{ background: rgba(38,166,154,0.08); }}
  tr.bad td {{ background: rgba(239,83,80,0.08); }}
  tr:hover td {{ filter: brightness(1.2); }}
  a {{ color: #2962ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>Batch backtest — MACross</h1>
<div class="meta">run_id: <b>{run_id}</b> · {total} combos · {finished} ok / {failed} failed · total {total_time:.0f}s · sorted by profit factor desc</div>
<table>
<thead><tr>
  <th>Asset</th><th>Interval</th><th class="r">Stop</th>
  <th class="r">Trades</th><th class="r">PnL</th><th class="r">Win Rate</th>
  <th class="r">PF</th><th class="r">Max DD</th>
  <th class="r">Stops/Strat</th>
  <th class="r">Sweep n</th><th class="r">Best PnL</th>
  <th>Artifacts</th>
</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>"""
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"\n=== Master index → {out_path}")


# ── CLI ─────────────────────────────────────────────────────────────────


def main() -> int:
    # All defaults pulled from get_settings() so the same .env that
    # configures sandbox/live also configures this batch runner. CLI
    # flags override settings; settings override hard-coded defaults.
    settings = get_settings()
    project_root = Path(__file__).resolve().parent.parent
    default_catalog = project_root / "data" / "catalog"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets",     nargs="+", default=settings.default_assets)
    parser.add_argument("--intervals",  nargs="+", default=settings.default_intervals)
    parser.add_argument("--stop-pcts",  nargs="+", type=float,
                        default=[settings.default_stop_pct] if settings.default_stop_pct else [0.05])
    parser.add_argument("--ma-type",    default=DEFAULT_MA_TYPE)
    parser.add_argument("--fast-ma",    type=int, default=DEFAULT_FAST_MA)
    parser.add_argument("--slow-ma",    type=int, default=DEFAULT_SLOW_MA)
    parser.add_argument("--data-source", default=settings.data_source)
    parser.add_argument("--exec-venue", default=settings.exec_venue)
    parser.add_argument("--starting-capital", type=int, default=settings.starting_capital)
    parser.add_argument("--leverage",   type=int, default=settings.leverage)
    parser.add_argument("--catalog-path", default=str(default_catalog))
    parser.add_argument("--dry-run", action="store_true",
                        help="List combos and exit without running.")
    args = parser.parse_args()

    combos = [
        (asset, interval, stop)
        for asset in args.assets
        for interval in args.intervals
        for stop in args.stop_pcts
    ]
    print(f"Batch: {len(combos)} combos planned "
          f"({len(args.assets)} assets x {len(args.intervals)} intervals "
          f"x {len(args.stop_pcts)} stops)")
    for c in combos:
        print(f"  {c[0]}  {c[1]}  stop={c[2]:.0%}")
    if args.dry_run:
        return 0

    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_root = _PROJECT_ROOT / "reports" / "batch" / run_id
    out_dirs = {
        "charts":     _PROJECT_ROOT / "reports" / "charts" / run_id,
        "tearsheets": _PROJECT_ROOT / "reports" / "tearsheets" / run_id,
        "sweeps_html": _PROJECT_ROOT / "reports" / "sweeps" / run_id,
        "sweeps_data": _PROJECT_ROOT / "data" / "sweeps" / run_id,
        "batch_root": run_root,
    }
    for d in out_dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    results: list[ComboResult] = []
    # Sweep grids come from src.strategies.ma_cross — single source of
    # truth shared with notebooks/backtest/ma_cross.ipynb so the script
    # and the notebook always sweep the same combos for a given MA type.
    fast_grid = MA_FAST_GRIDS[args.ma_type]
    slow_grid = MA_SLOW_GRIDS[args.ma_type]
    spotlights = MA_SPOTLIGHTS[args.ma_type]
    print(f"\nGrid for {args.ma_type}: "
          f"{len(fast_grid)} fast x {len(slow_grid)} slow "
          f"({sum(1 for f in fast_grid for s in slow_grid if f < s)} valid combos), "
          f"{len(spotlights)} spotlights")

    for asset, interval, stop in combos:
        try:
            r = run_combo(
                asset=asset, interval=interval, stop_pct=stop,
                data_source=args.data_source, exec_venue=args.exec_venue,
                ma_type=args.ma_type, fast_ma=args.fast_ma, slow_ma=args.slow_ma,
                fast_grid=fast_grid, slow_grid=slow_grid,
                starting_capital=args.starting_capital,
                trade_notional=settings.trade_notional,
                leverage=args.leverage,
                catalog_path=args.catalog_path,
                out_dirs=out_dirs,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e!r}")
            r = _empty_result(
                asset, interval, stop,
                utils.make_instrument_id(asset, args.data_source),
                error=repr(e),
            )
        results.append(r)
        # Persist incremental progress in case we crash mid-run.
        json_path = run_root / "results.json"
        json_path.write_text(json.dumps(
            [_serialise(r) for r in results], indent=2,
        ), encoding="utf-8")

    write_master_index(results, run_root / "index.html", run_id)
    return 0


def _serialise(r: ComboResult) -> dict[str, Any]:
    """ComboResult -> JSON-friendly dict (Path objects -> strings)."""
    d = r.__dict__.copy()
    for k in ("chart_path", "tearsheet_path", "sweep_html_path", "heatmap_path"):
        if d[k] is not None:
            d[k] = str(d[k])
    return d


if __name__ == "__main__":
    sys.exit(main())
