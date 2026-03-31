"""Post-backtest analysis tools — rolling performance, regime tagging, fee sensitivity.

Operates on positions and bars from completed backtests. All functions return
DataFrames suitable for inline notebook display or further plotting in charts.py.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable

    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import CryptoPerpetual


# ── Shared helper ────────────────────────────────────────────────────────────


def _positions_to_pnl_df(positions: list[Any]) -> pd.DataFrame:
    """Convert NT Position objects to a DataFrame with timestamps and PnL.

    Parameters
    ----------
    positions
        Position objects from a completed backtest. Use
        ``engine.cache.position_snapshots() + engine.cache.positions()``.

    Returns
    -------
    pd.DataFrame
        Columns: ts_opened (UTC Timestamp), ts_closed (UTC Timestamp),
        pnl (Decimal), duration_ns (int). Sorted by ts_opened.

    """
    if not positions:
        return pd.DataFrame(
            columns=["ts_opened", "ts_closed", "pnl", "duration_ns"],
        )

    rows: list[dict[str, Any]] = []
    for p in positions:
        pnl = p.realized_pnl.as_decimal() if p.realized_pnl is not None else Decimal(0)
        rows.append({
            "ts_opened": pd.Timestamp(p.ts_opened, unit="ns", tz="UTC"),
            "ts_closed": pd.Timestamp(p.ts_closed, unit="ns", tz="UTC"),
            "pnl": pnl,
            "duration_ns": p.duration_ns,
        })

    df = pd.DataFrame(rows)
    return df.sort_values("ts_opened").reset_index(drop=True)


# ── Shared stats computation ─────────────────────────────────────────────────


def _compute_window_stats(
    pnls: list[Decimal],
    starting_capital: float,
) -> dict[str, Any]:
    """Compute summary stats for a group of position PnLs.

    Returns a dict with: pnl, pnl_pct, num_positions, win_rate,
    avg_winner, avg_loser, profit_factor.
    """
    n = len(pnls)
    if n == 0:
        return {
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "num_positions": 0,
            "win_rate": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "profit_factor": np.nan,
        }

    total_pnl = float(sum(pnls))
    winners = [float(p) for p in pnls if p > 0]
    losers = [float(p) for p in pnls if p < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))

    return {
        "pnl": total_pnl,
        "pnl_pct": total_pnl / starting_capital * 100 if starting_capital > 0 else 0.0,
        "num_positions": n,
        "win_rate": len(winners) / n,
        "avg_winner": gross_wins / len(winners) if winners else 0.0,
        "avg_loser": sum(losers) / len(losers) if losers else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses > 0 else np.nan,
    }


# ── Tool 1: Rolling Performance Windows ──────────────────────────────────────


def rolling_performance(
    positions: list[Any],
    bars: list[Bar],
    *,
    window: str = "90D",
    step: str | None = None,
    starting_capital: float = 10_000,
) -> pd.DataFrame:
    """Compute per-window performance stats from backtest positions.

    Parameters
    ----------
    positions
        Position objects from a completed backtest. Use
        ``engine.cache.position_snapshots() + engine.cache.positions()``.
    bars
        The bar data fed to the backtest. Used to determine the full
        date range (windows span the data range, not just the positions).
    window
        Window size as a pandas offset string (e.g., "30D", "90D", "180D").
        Default 90 days.
    step
        Step size between windows. Default is half the window size
        (50% overlap), giving smoother output. Set equal to window
        for non-overlapping.
    starting_capital
        Used to compute PnL percentage per window.

    Returns
    -------
    pd.DataFrame
        One row per window with columns: window_start, window_end,
        pnl, pnl_pct, num_positions, win_rate, avg_winner, avg_loser,
        profit_factor, max_drawdown_pct.

    """
    pos_df = _positions_to_pnl_df(positions)

    data_start = pd.Timestamp(bars[0].ts_event, unit="ns", tz="UTC")
    data_end = pd.Timestamp(bars[-1].ts_event, unit="ns", tz="UTC")

    window_td = pd.Timedelta(window)
    step_td = pd.Timedelta(step) if step is not None else window_td / 2

    window_starts = pd.date_range(data_start, data_end, freq=step_td)

    results: list[dict[str, Any]] = []
    for ws in window_starts:
        we = ws + window_td

        if pos_df.empty:
            mask = pd.Series(dtype=bool)
        else:
            mask = (pos_df["ts_opened"] >= ws) & (pos_df["ts_opened"] < we)
        win_positions = pos_df.loc[mask] if not pos_df.empty else pos_df

        pnls = list(win_positions["pnl"]) if not win_positions.empty else []
        stats = _compute_window_stats(pnls, starting_capital)

        # Max drawdown within window: cumulative PnL sorted by close time
        mdd_pct = 0.0
        if not win_positions.empty:
            sorted_pnls = win_positions.sort_values("ts_closed")["pnl"]
            cum_pnl = float(0)
            peak = float(0)
            for pnl_val in sorted_pnls:
                cum_pnl += float(pnl_val)
                if cum_pnl > peak:
                    peak = cum_pnl
                drawdown = peak - cum_pnl
                dd_pct = drawdown / starting_capital * 100 if starting_capital > 0 else 0.0
                if dd_pct > mdd_pct:
                    mdd_pct = dd_pct

        row = {
            "window_start": ws,
            "window_end": we,
            **stats,
            "max_drawdown_pct": mdd_pct,
        }
        results.append(row)

    return pd.DataFrame(results)


# ── Tool 2: Regime Tagging ───────────────────────────────────────────────────


def _bars_to_ohlc_df(bars: list[Bar]) -> pd.DataFrame:
    """Convert NT Bar list to a DatetimeIndex DataFrame with OHLC columns."""
    rows = []
    for b in bars:
        rows.append({
            "time": pd.Timestamp(b.ts_event, unit="ns", tz="UTC"),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        })
    df = pd.DataFrame(rows)
    return df.set_index("time")


def tag_regimes(
    bars: list[Bar],
    *,
    method: str = "adx",
    adx_period: int = 14,
    adx_trending_threshold: float = 25.0,
    adx_ranging_threshold: float = 20.0,
    volatility_lookback: int = 20,
    volatility_percentile: float = 50.0,
) -> pd.DataFrame:
    """Tag each bar with a market regime label.

    Parameters
    ----------
    bars
        Bar data to analyze.
    method
        Detection method. One of:
        - "adx": ADX > trending_threshold = TRENDING,
          ADX < ranging_threshold = RANGING, between = TRANSITIONAL.
        - "volatility": ATR/close ratio above percentile = HIGH_VOL,
          below = LOW_VOL.
    adx_period
        ADX indicator period (used when method="adx").
    adx_trending_threshold
        ADX value above which the market is tagged TRENDING.
    adx_ranging_threshold
        ADX value below which the market is tagged RANGING.
        Values between ranging and trending are TRANSITIONAL.
    volatility_lookback
        ATR lookback period (used when method="volatility").
    volatility_percentile
        Percentile threshold for high/low volatility split.

    Returns
    -------
    pd.DataFrame
        Indexed by bar timestamp with columns: close, indicator_value
        (ADX or ATR ratio), regime (categorical label).

    """
    import pandas_ta  # noqa: PLC0415 — lazy import

    df = _bars_to_ohlc_df(bars)

    if method == "adx":
        adx_df = pandas_ta.adx(
            high=df["high"], low=df["low"], close=df["close"],
            length=adx_period,
        )
        adx_col = f"ADX_{adx_period}"
        indicator = adx_df[adx_col]

        regime = pd.Series(index=df.index, dtype="object")
        regime[indicator >= adx_trending_threshold] = "TRENDING"
        regime[indicator <= adx_ranging_threshold] = "RANGING"
        # Between thresholds → TRANSITIONAL (only where not already set)
        regime[(indicator > adx_ranging_threshold) & (indicator < adx_trending_threshold)] = "TRANSITIONAL"
        # NaN rows (warmup) stay as NaN

    elif method == "volatility":
        atr_series = pandas_ta.atr(
            high=df["high"], low=df["low"], close=df["close"],
            length=volatility_lookback,
        )
        indicator = atr_series / df["close"]

        threshold = indicator.quantile(volatility_percentile / 100)
        regime = pd.Series(index=df.index, dtype="object")
        regime[indicator >= threshold] = "HIGH_VOL"
        regime[indicator < threshold] = "LOW_VOL"
        # NaN rows (warmup) stay as NaN

    else:
        msg = f"Unknown method {method!r}. Must be 'adx' or 'volatility'."
        raise ValueError(msg)

    return pd.DataFrame({
        "close": df["close"],
        "indicator_value": indicator,
        "regime": regime,
    })


def performance_by_regime(
    positions: list[Any],
    regime_df: pd.DataFrame,
    *,
    starting_capital: float = 10_000,
) -> pd.DataFrame:
    """Split backtest results by market regime.

    Assigns each position to the regime active at its open timestamp,
    then computes aggregate stats per regime.

    Parameters
    ----------
    positions
        Position objects from a completed backtest.
    regime_df
        Output of tag_regimes(). Must have a datetime index and
        a 'regime' column.
    starting_capital
        Used to compute PnL percentage per regime.

    Returns
    -------
    pd.DataFrame
        One row per regime with columns: regime, pnl, pnl_pct,
        num_positions, win_rate, avg_winner, avg_loser,
        profit_factor, avg_duration.

    """
    pos_df = _positions_to_pnl_df(positions)

    if pos_df.empty:
        return pd.DataFrame(
            columns=[
                "regime", "pnl", "pnl_pct", "num_positions", "win_rate",
                "avg_winner", "avg_loser", "profit_factor", "avg_duration",
            ],
        )

    # Assign regime to each position by looking up the nearest bar at or before ts_opened
    regime_index = regime_df.index
    regimes: list[str | None] = []
    for ts in pos_df["ts_opened"]:
        loc = regime_index.asof(ts)
        if pd.isna(loc):
            regimes.append(None)
        else:
            regimes.append(regime_df.loc[loc, "regime"])
    pos_df["regime"] = regimes

    # Drop positions with no regime assignment (opened before first bar)
    pos_df = pos_df.dropna(subset=["regime"])

    results: list[dict[str, Any]] = []
    for regime_label, group in pos_df.groupby("regime", sort=False):
        pnls = list(group["pnl"])
        stats = _compute_window_stats(pnls, starting_capital)

        avg_dur_ns = group["duration_ns"].mean()
        avg_dur_hours = avg_dur_ns / 3_600_000_000_000

        results.append({
            "regime": regime_label,
            **stats,
            "avg_duration": round(avg_dur_hours, 1),
        })

    result_df = pd.DataFrame(results)
    return result_df.sort_values("num_positions", ascending=False).reset_index(drop=True)


# ── Tool 3: Fee Sensitivity Sweep ────────────────────────────────────────────


_DEFAULT_FEE_LEVELS_BPS: list[float] = [1, 2, 2.5, 3, 4, 5, 7.5, 10]


def run_fee_sweep(
    venue: Venue,
    instrument: CryptoPerpetual,
    bars: list[Bar],
    starting_capital: int | float,
    params: dict[str, Any],
    strategy_factory: Callable[[BacktestEngine, dict[str, Any]], None],
    *,
    fee_levels_bps: list[float] | None = None,
    log_level: str = "ERROR",
    verbose: bool = True,
) -> pd.DataFrame:
    """Re-run a strategy at different fee levels to measure sensitivity.

    The instrument already has realistic fees from with_venue_config().
    This sweep tests a range of fee levels centered around real-world
    rates to find the breakeven point and measure margin of safety.

    Creates a modified copy of the instrument (via with_venue_config)
    with different maker/taker fees for each level, runs a full
    backtest, and collects results.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The base instrument (CryptoPerpetual, already configured with
        realistic fees via with_venue_config).
    bars
        Bar data to feed.
    starting_capital
        Starting balance.
    params
        Strategy parameters (the "best" params from a sweep).
    strategy_factory
        Callback ``(engine, params) -> None`` that adds a strategy.
    fee_levels_bps
        List of fee levels in basis points to test (applied as both
        maker and taker). Default: [1, 2, 2.5, 3, 4, 5, 7.5, 10]
        which brackets real-world exchange rates:
        - Hyperliquid taker: ~3.5 bps
        - Binance Futures taker: ~5 bps
        - Higher tiers test resilience to fee increases
    log_level
        NT log level.
    verbose
        Print per-level results.

    Returns
    -------
    pd.DataFrame
        One row per fee level with columns: fee_bps, fee_rate,
        total_pnl, total_pnl_pct, num_positions, final_balance,
        pnl_per_trade, breakeven (bool -- True if total_pnl > 0).

    """
    from src.backtesting.engine import run_single_backtest
    from src.core.instruments import with_venue_config

    levels = fee_levels_bps if fee_levels_bps is not None else _DEFAULT_FEE_LEVELS_BPS
    total = len(levels)

    # Derive leverage from instrument's margin_init
    max_leverage = int(Decimal(1) / instrument.margin_init)

    results: list[dict[str, Any]] = []
    t0 = time.monotonic()

    for i, fee_bps in enumerate(levels, 1):
        fee_rate = Decimal(str(fee_bps)) / Decimal("10000")
        modified = with_venue_config(
            instrument, max_leverage,
            maker_fee=fee_rate, taker_fee=fee_rate,
        )

        row = run_single_backtest(
            venue=venue,
            instrument=modified,
            bars=bars,
            starting_capital=starting_capital,
            params=params,
            add_strategy=lambda eng, p=params: strategy_factory(eng, p),  # type: ignore[misc]
            log_level=log_level,
        )

        total_pnl = row.get("total_pnl", float("nan"))
        num_pos = row.get("num_positions", 0)

        result = {
            "fee_bps": fee_bps,
            "fee_rate": float(fee_rate),
            "total_pnl": total_pnl,
            "total_pnl_pct": row.get("total_pnl_pct", float("nan")),
            "num_positions": num_pos,
            "final_balance": row.get("final_balance", float("nan")),
            "pnl_per_trade": total_pnl / num_pos if num_pos > 0 else 0.0,
            "breakeven": bool(total_pnl > 0) if not np.isnan(total_pnl) else False,
        }
        results.append(result)

        if verbose:
            err = f"  !! {row['error']}" if row.get("error") else ""
            print(
                f"  [{i}/{total}] fee={fee_bps:>5.1f} bps  "
                f"PnL={total_pnl:>10.2f} PnL%={result['total_pnl_pct']:>7.2f}%"
                f"  positions={num_pos}{err}"
            )

    elapsed = time.monotonic() - t0

    df = pd.DataFrame(results)

    if verbose:
        breakeven_rows = df[df["breakeven"]]
        if not breakeven_rows.empty and not df[~df["breakeven"]].empty:
            last_profitable = breakeven_rows["fee_bps"].max()
            print(f"\n  Fee sweep complete — {total} levels in {elapsed:.1f}s")
            print(f"  Breakeven threshold: ~{last_profitable:.1f} bps")
        else:
            print(f"\n  Fee sweep complete — {total} levels in {elapsed:.1f}s")

    return df
