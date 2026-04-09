"""Backtest engine helpers — shared infrastructure for notebook workflows.

Wraps NT's BacktestEngine setup and parameter sweep boilerplate so that
notebooks only need to provide strategy-specific configuration.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

if TYPE_CHECKING:
    from collections.abc import Callable

    from nautilus_trader.common.component import LogGuard
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import Instrument


# ── Default sweep output directory ───────────────────────────────────────────
_DEFAULT_SWEEP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "sweeps"


# ── Rust LogGuard singleton ──────────────────────────────────────────────────
# NT 1.225.0's Rust logging subsystem panics if re-initialized after the
# LogGuard is freed (i.e. after an engine.dispose()).  BacktestNode solves
# this by capturing the guard once and keeping it alive (node.py:369-374).
# We mirror that pattern here so that make_engine() can create unlimited
# fresh engines without triggering the Rust panic.
_log_guard: LogGuard | None = None


def _ensure_log_guard(log_level: str = "ERROR") -> None:
    """Initialize the Rust logger once and capture the LogGuard."""
    global _log_guard
    if _log_guard is not None:
        return
    init_engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level=log_level)),
    )
    _log_guard = init_engine.kernel.get_log_guard()
    init_engine.dispose()


def _native_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convert numpy scalars to Python native types."""
    return {k: v.item() if hasattr(v, "item") else v for k, v in params.items()}


def make_engine(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    log_level: str = "ERROR",
) -> BacktestEngine:
    """Create a configured BacktestEngine with venue, instrument, and data.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in the instrument's settlement currency.
    log_level
        NT log level. Default ``"ERROR"`` to avoid stdout flooding.

    """
    _ensure_log_guard(log_level)
    engine = BacktestEngine(config=BacktestEngineConfig())
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(starting_capital, instrument.settlement_currency)],
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    return engine


def run_single_backtest(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    params: dict[str, Any],
    add_strategy: Callable[[BacktestEngine], None],
    score_from_ns: int | None = None,
    log_level: str = "ERROR",
) -> dict[str, Any]:
    """Run one backtest and return a flat dict of results.

    Creates a fresh engine, runs the backtest, extracts stats, detects
    liquidation, and disposes the engine. Designed for parameter sweeps.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in the instrument's settlement currency.
    params
        Sweep parameters (e.g. ``{"fast": 10, "slow": 50}``).
        Passed through to the returned dict as-is.
    add_strategy
        Callback that receives the engine and must call
        ``engine.add_strategy(...)`` with the desired strategy.
    score_from_ns
        If provided, only positions opened at or after this nanosecond
        timestamp are included in PnL scoring.  Used by walk-forward
        analysis to exclude trades that fire during the warmup period
        (bars prepended for indicator initialization).  When ``None``
        (the default), all positions are scored.
    log_level
        NT log level.

    Returns
    -------
    dict[str, Any]
        Contains all keys from *params* plus ``total_pnl``,
        ``total_pnl_pct``, ``num_positions``, ``final_balance``,
        ``min_balance``, ``error``, and analyzer performance stats.

    """
    eng = make_engine(venue, instrument, bars, starting_capital, log_level)
    add_strategy(eng)

    row: dict[str, Any] = {**params}

    try:
        eng.run()

        a = eng.portfolio.analyzer
        acct = eng.cache.account_for_venue(venue)
        pos = eng.cache.position_snapshots() + eng.cache.positions()

        # Filter to only positions opened in the scoring window
        if score_from_ns is not None:
            pos = [p for p in pos if p.ts_opened >= score_from_ns]

        if acct is None:
            row["error"] = "no account"
            row.update(
                total_pnl=np.nan, total_pnl_pct=np.nan,
                num_positions=len(pos), final_balance=np.nan,
                min_balance=np.nan,
            )
        else:
            a.calculate_statistics(acct, pos)
            currency = instrument.settlement_currency
            balance = float(acct.balance_total(currency))

            # Pull account report once — used for both score_capital
            # lookup and liquidation detection below.
            acct_report = eng.trader.generate_account_report(venue)

            # When scoring a subset of positions, derive PnL from
            # those positions — not the account (which includes warmup).
            # Use the account balance at the scoring boundary as the
            # capital base for pct calculation, not starting_capital.
            if score_from_ns is not None and pos:
                scored_pnl = sum(
                    float(p.realized_pnl.as_decimal())
                    for p in pos
                    if p.realized_pnl is not None
                )

                # Look up account balance at the scoring start so
                # total_pnl_pct reflects actual capital, not the
                # original starting_capital (which doesn't account
                # for warmup trades).
                score_capital = float(starting_capital)
                if not acct_report.empty:
                    score_ts = pd.Timestamp(score_from_ns, unit="ns", tz="UTC")
                    prior = acct_report.loc[acct_report.index <= score_ts]
                    if not prior.empty:
                        score_capital = float(prior["total"].iloc[-1])

                row.update(
                    total_pnl=scored_pnl,
                    total_pnl_pct=(
                        scored_pnl / score_capital * 100
                        if score_capital > 0
                        else 0.0
                    ),
                    num_positions=len(pos),
                    final_balance=balance,
                    error="",
                )
            else:
                row.update(
                    total_pnl=float(a.total_pnl(currency)),
                    total_pnl_pct=float(a.total_pnl_percentage(currency)),
                    num_positions=len(pos),
                    final_balance=balance,
                    error="",
                )

            # Detect if equity ever hit zero during the run
            if not acct_report.empty:
                min_bal = acct_report["total"].astype(float).min()
                row["min_balance"] = min_bal
                if min_bal <= 0:
                    row["error"] = "liquidated"
            else:
                row["min_balance"] = balance

            for stats_name, stats_fn in [
                ("general", a.get_performance_stats_general),
                ("PnL", lambda: a.get_performance_stats_pnls(currency)),
                ("returns", a.get_performance_stats_returns),
            ]:
                try:
                    for k, v in stats_fn().items():
                        row[k] = v
                except Exception as e:
                    print(f"  Warning: {stats_name} stats failed for {params}: {e}")

    except Exception as e:
        row["error"] = str(e)
        row.update(
            total_pnl=np.nan, total_pnl_pct=np.nan,
            num_positions=0, final_balance=np.nan, min_balance=np.nan,
        )
    finally:
        eng.dispose()

    return row


# ── Sweep orchestration + persistence ────────────────────────────────────────


def run_sweep(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    param_combos: list[dict[str, Any]],
    strategy_factory: Callable[[BacktestEngine, dict[str, Any]], None],
    *,
    strategy_name: str,
    instrument_id: str,
    bar_interval: str,
    save_sweep: bool = True,
    sweep_dir: str | Path = _DEFAULT_SWEEP_DIR,
    log_level: str = "ERROR",
    verbose: bool = True,
) -> pd.DataFrame:
    """Run a parameter sweep, persist results to Parquet, return DataFrame.

    Iterates over *param_combos*, calling ``run_single_backtest`` for each.
    Adds metadata columns so the saved file is self-describing.  Writes
    to ``sweep_dir`` with a deterministic filename based on strategy,
    instrument, and bar interval (re-running overwrites the previous
    sweep for the same combination).

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add to each engine.
    bars
        Bar data to feed to each engine.
    starting_capital
        Starting balance in the instrument's settlement currency.
    param_combos
        List of parameter dicts, e.g.
        ``[{"fast": 10, "slow": 50}, {"fast": 10, "slow": 75}, ...]``.
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy to the
        engine using the given params.  Example::

            def ma_factory(eng, params):
                cfg = MACrossConfig(
                    instrument_id=instrument.id,
                    bar_type=BarType.from_str(BAR_TYPE_STR),
                    trade_notional=TRADE_NOTIONAL,
                    ma_type="EMA",
                    fast_period=params["fast"],
                    slow_period=params["slow"],
                )
                eng.add_strategy(MACross(cfg))

    strategy_name
        Human-readable strategy label, e.g. ``"MACross-EMA"``.
    instrument_id
        Instrument string, e.g. ``"BTC-USD-PERP.HYPERLIQUID"``.
    bar_interval
        Bar interval string, e.g. ``"1h"`` or ``"5m"``.
    save_sweep
        Whether to save the sweep to a Parquet file.
    sweep_dir
        Directory for Parquet output.  Created if it doesn't exist.
    log_level
        NT log level passed to each engine.
    verbose
        Print per-combo progress lines.

    Returns
    -------
    pd.DataFrame
        One row per param combo with all stats, plus metadata columns
        prefixed with ``_`` (``_strategy``, ``_instrument_id``, etc.).

    """

    total = len(param_combos)
    results: list[dict[str, Any]] = []
    t0 = time.monotonic()

    for i, params in enumerate(param_combos, 1):
        row = run_single_backtest(
            venue=venue,
            instrument=instrument,
            bars=bars,
            starting_capital=starting_capital,
            params=params,
            add_strategy=lambda eng, p=params: strategy_factory(eng, p),  # type: ignore[misc]
            log_level=log_level,
        )
        results.append(row)

        if verbose:
            pnl = row.get("total_pnl", float("nan"))
            pnl_pct = row.get("total_pnl_pct", float("nan"))
            npos = row.get("num_positions", 0)
            err = f"  !! {row['error']}" if row.get("error") else ""
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            print(
                f"  [{i}/{total}] {param_str}  "
                f"PnL={pnl:>10.2f} PnL%={pnl_pct:>7.2f}%"
                f"  positions={npos}{err}"
            )

    elapsed = time.monotonic() - t0

    # ── Build DataFrame with metadata ────────────────────────────────────
    df = pd.DataFrame(results)

    # Data date range from the bars themselves
    data_start = pd.Timestamp(bars[0].ts_event, unit="ns", tz="UTC")
    data_end = pd.Timestamp(bars[-1].ts_event, unit="ns", tz="UTC")
    swept_at = datetime.now(tz=UTC)

    df.insert(0, "_strategy", strategy_name)
    df.insert(1, "_instrument_id", instrument_id)
    df.insert(2, "_bar_interval", bar_interval)
    df.insert(3, "_starting_capital", starting_capital)
    df.insert(4, "_data_start", data_start.isoformat())
    df.insert(5, "_data_end", data_end.isoformat())
    df.insert(6, "_swept_at", swept_at.isoformat())

    # ── Persist to Parquet ────────────────────────────────────────────────
    # Deterministic name: re-running the same strategy+instrument+interval
    # overwrites the previous file.  Timestamp is NOT in the filename —
    # _swept_at inside the file records when it was generated.
    if save_sweep:
        safe_instrument = instrument_id.replace("/", "-")
        filename = f"{strategy_name}_{safe_instrument}_{bar_interval}.parquet"
        out_path = Path(sweep_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_path, index=False)
        print(f"✓ Saved → {out_path}")

    if verbose:
        print(f"✓ Sweep complete — {total} combos in {elapsed:.1f}s")

    return df


def load_sweeps(
    sweep_dir: str | Path = _DEFAULT_SWEEP_DIR,
    *,
    strategy: str | None = None,
    instrument_id: str | None = None,
    bar_interval: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Load saved sweep Parquet files into a dict keyed by label.

    Parameters
    ----------
    sweep_dir
        Directory containing sweep Parquet files.
    strategy
        If provided, only load sweeps matching this strategy name.
    instrument_id
        If provided, only load sweeps matching this instrument.
    bar_interval
        If provided, only load sweeps matching this bar interval.

    Returns
    -------
    dict[str, pd.DataFrame]
        Keys are human-readable labels derived from filename,
        e.g. ``"MACross-EMA · BTC-USD-PERP.HYPERLIQUID · 5m"``.

    """
    sweep_path = Path(sweep_dir)
    if not sweep_path.exists():
        print(f"No sweep directory found at {sweep_path}")
        return {}

    files = sorted(sweep_path.glob("*.parquet"))
    if not files:
        print(f"No Parquet files found in {sweep_path}")
        return {}

    sweeps: dict[str, pd.DataFrame] = {}
    for f in files:
        df = pd.read_parquet(f)

        # Filter by metadata columns if requested
        if strategy and df["_strategy"].iloc[0] != strategy:
            continue
        if instrument_id and df["_instrument_id"].iloc[0] != instrument_id:
            continue
        if bar_interval and df["_bar_interval"].iloc[0] != bar_interval:
            continue

        # Build a readable label from metadata
        strat = df["_strategy"].iloc[0]
        inst = df["_instrument_id"].iloc[0]
        interval = df["_bar_interval"].iloc[0]
        label = f"{strat} · {inst} · {interval}"
        sweeps[label] = df

    print(f"Loaded {len(sweeps)} sweep(s) from {sweep_path}")
    return sweeps


# ── Walk-forward analysis ────────────────────────────────────────────────────


def run_walk_forward(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    param_combos: list[dict[str, Any]],
    strategy_factory: Callable[[BacktestEngine, dict[str, Any]], None],
    *,
    train_pct: float = 0.50,
    test_pct: float = 0.125,
    select_by: str = "total_pnl",
    warmup_bars: int = 200,
    log_level: str = "ERROR",
    verbose: bool = True,
) -> pd.DataFrame:
    """Sliding-window walk-forward analysis.

    Splits *bars* into train/test windows, slides by ``test_pct`` each
    fold.  For each fold: runs a full parameter sweep on the training
    window, selects the best combo by *select_by*, then runs that combo
    on the out-of-sample test window.

    Both training and test slices are prepended with up to *warmup_bars*
    extra bars so that indicators are fully initialized before the scored
    region begins.  For both slices, any trades that fire during the
    warmup period are excluded from results via *score_from_ns*,
    preventing warmup trades from influencing parameter selection or
    OOS scoring.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add to each engine.
    bars
        Full bar dataset — sliced internally per fold.
    starting_capital
        Starting balance in the instrument's settlement currency.
    param_combos
        Parameter grid to sweep each fold (same as ``run_sweep``).
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy.
    train_pct
        Fraction of total bars for each training window. Default 0.50.
    test_pct
        Fraction of total bars for each test window. Default 0.125.
        The window slides by this amount each fold, so test windows
        are non-overlapping.
    select_by
        Column name to maximize when selecting best in-sample params.
        Default ``"total_pnl"``.
    warmup_bars
        Number of extra bars prepended to each training and test window
        so that indicators are fully initialized before the scored region
        begins.  Default 200 — covers most MA/oscillator periods.
        Override in the notebook when your slowest indicator needs more
        (or fewer).
    log_level
        NT log level.
    verbose
        Print fold-level progress.

    Returns
    -------
    pd.DataFrame
        One row per fold with columns: ``fold``, ``train_start``,
        ``train_end``, ``test_start``, ``test_end``, ``best_*`` (selected
        params), ``in_sample_pnl``, ``oos_pnl``, ``oos_pnl_pct``,
        ``oos_positions``, and selected analyzer stats.

    Example
    -------
    With 10,000 bars, ``train_pct=0.50``, ``test_pct=0.125``::

        Fold 1: train bars[0:5000],     test bars[5000:6250]
        Fold 2: train bars[1250:6250],  test bars[6250:7500]
        Fold 3: train bars[2500:7500],  test bars[7500:8750]
        Fold 4: train bars[3750:8750],  test bars[8750:10000]

    """
    import time

    total_bars = len(bars)
    train_size = int(total_bars * train_pct)
    test_size = int(total_bars * test_pct)
    step_size = test_size  # non-overlapping test windows
    n_combos = len(param_combos)

    if train_size + test_size > total_bars:
        msg = (
            f"train_pct ({train_pct}) + test_pct ({test_pct}) = {train_pct + test_pct} "
            f"exceeds 1.0 — not enough data for even one fold."
        )
        raise ValueError(msg)

    if verbose:
        n_folds_est = (total_bars - train_size - test_size) // step_size + 1
        print(
            f"Walk-forward: {total_bars:,} bars, "
            f"train={train_size:,} ({train_pct:.0%}), "
            f"test={test_size:,} ({test_pct:.1%}), "
            f"~{n_folds_est} folds × {n_combos} combos"
        )

    folds: list[dict[str, Any]] = []
    fold_num = 0
    start = 0
    t0 = time.monotonic()

    while start + train_size + test_size <= total_bars:
        fold_num += 1

        # ── Training slice with warmup padding ──────────────────────
        train_warmup_start = max(0, start - warmup_bars)
        train_slice = bars[train_warmup_start : start + train_size]
        train_score_from_ns = bars[start].ts_event

        # ── Test slice with warmup padding ──────────────────────────
        # Prepend bars from the end of the training window so indicators
        # are initialized when the real test region begins.  Trades
        # during warmup are excluded via score_from_ns below.
        test_start_idx = start + train_size
        test_warmup_start = max(0, test_start_idx - warmup_bars)
        test_slice = bars[test_warmup_start : test_start_idx + test_size]
        test_score_from_ns = bars[test_start_idx].ts_event

        train_start_ts = pd.Timestamp(train_score_from_ns, unit="ns", tz="UTC")
        train_end_ts = pd.Timestamp(train_slice[-1].ts_event, unit="ns", tz="UTC")
        test_start_ts = pd.Timestamp(test_score_from_ns, unit="ns", tz="UTC")
        test_end_ts = pd.Timestamp(test_slice[-1].ts_event, unit="ns", tz="UTC")

        if verbose:
            print(
                f"\n── Fold {fold_num} ──\n"
                f"  Train: {train_start_ts:%Y-%m-%d} → {train_end_ts:%Y-%m-%d}"
                f"  ({len(train_slice):,} bars, {warmup_bars} warmup)\n"
                f"  Test:  {test_start_ts:%Y-%m-%d} → {test_end_ts:%Y-%m-%d}"
                f"  ({len(test_slice):,} bars, {warmup_bars} warmup)"
            )

        # ── Sweep on training data (no per-combo output) ────────────
        train_results: list[dict[str, Any]] = []
        for params in param_combos:
            row = run_single_backtest(
                venue=venue,
                instrument=instrument,
                bars=train_slice,
                starting_capital=starting_capital,
                params=params,
                add_strategy=lambda eng, p=params: strategy_factory(eng, p),  # type: ignore[misc]
                score_from_ns=train_score_from_ns,
                log_level=log_level,
            )
            train_results.append(row)

        train_df = pd.DataFrame(train_results)
        valid = train_df[
            train_df[select_by].notna() & (train_df["error"].fillna("") == "")
        ]

        if valid.empty:
            if verbose:
                errors = train_df["error"].dropna()
                errors = errors[errors != ""]
                first_err = errors.iloc[0] if not errors.empty else "unknown"
                print("  ⚠ No valid results in training — skipping fold")
                print(f"    First error: {first_err}")
            start += step_size
            continue

        best_idx = valid[select_by].idxmax()
        param_keys = list(param_combos[0].keys())
        best_params = _native_params({k: train_df.loc[best_idx, k] for k in param_keys})
        best_train_pnl = float(train_df.loc[best_idx, "total_pnl"])

        if verbose:
            param_str = ", ".join(f"{k}={v}" for k, v in best_params.items())
            print(f"  Best in-sample: {param_str}  (PnL={best_train_pnl:,.2f})")

        # ── Test best params on out-of-sample data ──────────────────
        oos_row = run_single_backtest(
            venue=venue,
            instrument=instrument,
            bars=test_slice,
            starting_capital=starting_capital,
            params=best_params,
            add_strategy=lambda eng, p=best_params: strategy_factory(eng, p),  # type: ignore[misc]
            score_from_ns=test_score_from_ns,
            log_level=log_level,
        )

        oos_pnl = oos_row.get("total_pnl", float("nan"))
        if verbose:
            oos_pnl_pct = oos_row.get("total_pnl_pct", float("nan"))
            oos_npos = oos_row.get("num_positions", 0)
            print(
                f"  Out-of-sample: PnL={oos_pnl:,.2f}  "
                f"PnL%={oos_pnl_pct:.2f}%  positions={oos_npos}"
            )
            if oos_row.get("error"):
                print(f"    ⚠ OOS error: {oos_row['error']}")

        fold_result: dict[str, Any] = {
            "fold": fold_num,
            "train_start": train_start_ts.isoformat(),
            "train_end": train_end_ts.isoformat(),
            "test_start": test_start_ts.isoformat(),
            "test_end": test_end_ts.isoformat(),
            "train_bars": len(train_slice),
            "test_bars": len(test_slice),
        }
        for k, v in best_params.items():
            fold_result[f"best_{k}"] = v
        fold_result.update({
            "in_sample_pnl": best_train_pnl,
            "oos_pnl": oos_pnl,
            "oos_pnl_pct": oos_row.get("total_pnl_pct", float("nan")),
            "oos_positions": oos_row.get("num_positions", 0),
            "oos_error": oos_row.get("error", ""),
        })

        # Pull through any analyzer stats that made it into the OOS row
        for stat_key in [
            "Win Rate", "Profit Factor", "Sharpe Ratio (252 days)",
            "Max Drawdown", "Avg Winner", "Avg Loser", "Expectancy",
        ]:
            if stat_key in oos_row:
                fold_result[f"oos_{stat_key}"] = oos_row[stat_key]

        folds.append(fold_result)
        start += step_size

    elapsed = time.monotonic() - t0

    if not folds:
        print("No folds completed. Check data length vs train_pct / test_pct.")
        return pd.DataFrame()

    result_df = pd.DataFrame(folds)

    if verbose:
        profitable = int((result_df["oos_pnl"] > 0).sum())
        total_folds = len(result_df)
        total_oos_pnl = result_df["oos_pnl"].sum()

        print(f"\n{'─' * 50}")
        print(f"Walk-Forward Summary  ({elapsed:.1f}s)")
        print(f"{'─' * 50}")
        print(f"  Folds:          {total_folds}")
        print(
            f"  OOS profitable: {profitable}/{total_folds}"
            f"  ({profitable / total_folds * 100:.0f}%)"
        )
        print(f"  Total OOS PnL:  {total_oos_pnl:,.2f}")

        # Param stability check
        param_cols = [c for c in result_df.columns if c.startswith("best_")]
        if param_cols:
            all_same = all(result_df[c].nunique() == 1 for c in param_cols)
            if all_same:
                vals = ", ".join(
                    f"{c.removeprefix('best_')}={result_df[c].iloc[0]}"
                    for c in param_cols
                )
                print(f"  Params:         STABLE ({vals} every fold)")
            else:
                print("  Params:         UNSTABLE (different params per fold)")
                for c in param_cols:
                    unique = sorted(result_df[c].unique())
                    print(f"    {c}: {unique}")

    return result_df
