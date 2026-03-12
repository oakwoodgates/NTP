"""Backtest engine helpers — shared infrastructure for notebook workflows.

Wraps NT's BacktestEngine setup and parameter sweep boilerplate so that
notebooks only need to provide strategy-specific configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import Instrument


# ── Default sweep output directory ───────────────────────────────────────────
_DEFAULT_SWEEP_DIR = "data/sweeps"


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
        Starting balance in USDC.
    log_level
        NT log level. Default ``"ERROR"`` to avoid stdout flooding.

    """
    engine = BacktestEngine(config=BacktestEngineConfig(
        logging=LoggingConfig(log_level=log_level),
    ))
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(starting_capital, USDC)],
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
        Starting balance in USDC.
    params
        Sweep parameters (e.g. ``{"fast": 10, "slow": 50}``).
        Passed through to the returned dict as-is.
    add_strategy
        Callback that receives the engine and must call
        ``engine.add_strategy(...)`` with the desired strategy.
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

        if acct is None:
            row["error"] = "no account"
            row.update(
                total_pnl=np.nan, total_pnl_pct=np.nan,
                num_positions=len(pos), final_balance=np.nan,
                min_balance=np.nan,
            )
        else:
            a.calculate_statistics(acct, pos)
            balance = float(acct.balance_total(USDC))
            row.update(
                total_pnl=float(a.total_pnl(USDC)),
                total_pnl_pct=float(a.total_pnl_percentage(USDC)),
                num_positions=len(pos),
                final_balance=balance,
                error="",
            )

            # Detect if equity ever hit zero during the run
            acct_report = eng.trader.generate_account_report(venue)
            if not acct_report.empty:
                min_bal = acct_report["total"].astype(float).min()
                row["min_balance"] = min_bal
                if min_bal <= 0:
                    row["error"] = "liquidated"
            else:
                row["min_balance"] = balance

            for stats_name, stats_fn in [
                ("general", a.get_performance_stats_general),
                ("PnL", lambda: a.get_performance_stats_pnls(USDC)),
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
        Starting balance in USDC.
    param_combos
        List of parameter dicts, e.g.
        ``[{"fast": 10, "slow": 50}, {"fast": 10, "slow": 75}, ...]``.
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy to the
        engine using the given params.  Example::

            def ema_factory(eng, params):
                cfg = EMACrossConfig(
                    instrument_id=instrument.id,
                    bar_type=BarType.from_str(BAR_TYPE_STR),
                    trade_size=TRADE_SIZE,
                    fast_ema_period=params["fast"],
                    slow_ema_period=params["slow"],
                )
                eng.add_strategy(EMACross(cfg))

    strategy_name
        Human-readable strategy label, e.g. ``"EMACross"``.
    instrument_id
        Instrument string, e.g. ``"BTC-USD-PERP.HYPERLIQUID"``.
    bar_interval
        Bar interval string, e.g. ``"1h"`` or ``"5m"``.
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
    import time
    from datetime import datetime, timezone
    from pathlib import Path

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
            add_strategy=lambda eng, p=params: strategy_factory(eng, p),
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
    swept_at = datetime.now(tz=timezone.utc)

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
    safe_instrument = instrument_id.replace("/", "-")
    filename = f"{strategy_name}_{safe_instrument}_{bar_interval}.parquet"
    out_path = Path(sweep_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    if verbose:
        print(
            f"\n✓ Sweep complete — {total} combos in {elapsed:.1f}s"
            f"\n✓ Saved → {out_path}"
        )

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
        e.g. ``"EMACross · BTC-USD-PERP.HYPERLIQUID · 5m"``.

    """
    from pathlib import Path

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
