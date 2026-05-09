"""Trustworthy backtest metrics — pure functions, no NT objects.

Sweep schema v2 metrics live here.  Everything in this module operates
on plain Python / pandas inputs (lists of floats, ints, timestamps,
``pd.Series`` of balances) so the math is fully testable without
spinning up a NT engine.

Why not the NT analyzer's Returns section?
    Because NT 1.226's ``_calculate_portfolio_returns`` zero-pads
    non-trading days via ``.ffill().pct_change()``, biasing every
    returns-derived stat for any strategy that doesn't trade daily.
    See ``docs/ANALYZER_RETURNS_CAVEAT.md``.

The metrics here all derive from PnL realized on closed positions and
event-time balance snapshots — both of which NT exposes correctly.

Public API
----------

* :class:`TradeRecord` — minimal closed-trade record, the input to all
  trade-level metrics.
* :func:`compute_trade_metrics` — PnL distribution, expectancy, payoff
  ratio, profit factor, win rate, largest win/loss, avg duration,
  max consecutive losses, long/short attribution.
* :func:`compute_balance_metrics` — drawdown anatomy + CAGR + MAR
  from an event-time balance series.
* :func:`compute_activity_metrics` — bars in market, fee-as-%-of-PnL.
* :func:`compute_all_metrics` — convenience wrapper that calls the
  three above and merges the result dicts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd


@dataclass(frozen=True)
class TradeRecord:
    """A single closed trade.  Currency-agnostic (PnL is a float)."""

    pnl: float
    """Realized PnL on the position, in the settlement currency."""

    ts_opened_ns: int
    """Position open timestamp (UTC nanos since epoch)."""

    ts_closed_ns: int
    """Position close timestamp (UTC nanos since epoch)."""

    side: str
    """``"LONG"`` or ``"SHORT"``."""


def _safe_div(numerator: float, denominator: float, default: float = float("nan")) -> float:
    """Division that returns *default* on zero/non-finite denominators."""
    if denominator == 0 or not math.isfinite(denominator):
        return default
    return numerator / denominator


# ── Trade-level metrics ──────────────────────────────────────────────────────


def compute_trade_metrics(
    trades: list[TradeRecord],
    *,
    bar_interval_ns: int | None = None,
) -> dict[str, float]:
    """Trade-distribution metrics from a list of closed trades.

    All keys are always present; missing-data cases use ``nan`` rather
    than raising, so the resulting dict slots cleanly into a sweep row.

    Parameters
    ----------
    trades
        Closed trades.  An empty list yields a dict of nans.
    bar_interval_ns
        Bar interval in nanoseconds.  When provided, durations are
        reported in bars; when ``None``, in seconds.

    Returns
    -------
    dict[str, float]
        Keys: ``avg_pnl_per_trade``, ``win_rate``, ``loss_rate``,
        ``num_winners``, ``num_losers``, ``num_breakeven``,
        ``gross_wins``, ``gross_losses``, ``avg_win``, ``avg_loss``,
        ``largest_win``, ``largest_loss``, ``pnl_profit_factor``,
        ``expectancy``, ``payoff_ratio``, ``avg_trade_duration_bars``
        (or ``..._secs``), ``max_consec_losers``, ``max_consec_winners``,
        ``num_long``, ``num_short``, ``long_pnl``, ``short_pnl``.

    """
    duration_unit = "bars" if bar_interval_ns is not None else "secs"
    keys = [
        "avg_pnl_per_trade", "win_rate", "loss_rate",
        "num_winners", "num_losers", "num_breakeven",
        "gross_wins", "gross_losses", "avg_win", "avg_loss",
        "largest_win", "largest_loss", "pnl_profit_factor",
        "expectancy", "payoff_ratio",
        f"avg_trade_duration_{duration_unit}",
        "max_consec_losers", "max_consec_winners",
        "num_long", "num_short", "long_pnl", "short_pnl",
    ]
    if not trades:
        return {k: float("nan") for k in keys}

    pnls = [t.pnl for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    breakevens = [p for p in pnls if p == 0]

    gross_wins = sum(winners)
    gross_losses = sum(losers)  # negative
    n_win = len(winners)
    n_loss = len(losers)

    avg_pnl = total_pnl / n
    win_rate = n_win / n
    loss_rate = n_loss / n
    avg_win = gross_wins / n_win if n_win else float("nan")
    avg_loss = gross_losses / n_loss if n_loss else float("nan")

    # Profit factor: gross_wins / abs(gross_losses).
    # When there are zero losses, conventional usage is "infinite" — we
    # report inf so a downstream sort puts it at the top, but a sweep
    # consumer should always treat n_losers == 0 specially.
    if gross_losses == 0:
        profit_factor = float("inf") if gross_wins > 0 else float("nan")
    else:
        profit_factor = gross_wins / abs(gross_losses)

    # Expectancy: average $ per trade.  Algebraically same as avg_pnl,
    # but the formula is the standard textbook decomposition.
    expectancy = (
        win_rate * (avg_win if n_win else 0.0)
        + loss_rate * (avg_loss if n_loss else 0.0)
    )

    payoff = (
        abs(avg_win / avg_loss)
        if (n_win and n_loss and avg_loss != 0)
        else float("nan")
    )

    largest_win = max(pnls) if pnls else float("nan")
    largest_loss = min(pnls) if pnls else float("nan")

    # Duration
    durations_ns = [t.ts_closed_ns - t.ts_opened_ns for t in trades]
    if bar_interval_ns:
        avg_duration = sum(durations_ns) / (n * bar_interval_ns)
    else:
        avg_duration = sum(durations_ns) / (n * 1_000_000_000)  # secs

    # Consecutive runs (sort trades chronologically by close time).
    sorted_trades = sorted(trades, key=lambda t: t.ts_closed_ns)
    max_consec_losers = _max_consec(sorted_trades, lambda t: t.pnl < 0)
    max_consec_winners = _max_consec(sorted_trades, lambda t: t.pnl > 0)

    # Long/short attribution
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]

    return {
        "avg_pnl_per_trade": avg_pnl,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "num_winners": float(n_win),
        "num_losers": float(n_loss),
        "num_breakeven": float(len(breakevens)),
        "gross_wins": gross_wins,
        "gross_losses": gross_losses,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "pnl_profit_factor": profit_factor,
        "expectancy": expectancy,
        "payoff_ratio": payoff,
        f"avg_trade_duration_{duration_unit}": avg_duration,
        "max_consec_losers": float(max_consec_losers),
        "max_consec_winners": float(max_consec_winners),
        "num_long": float(len(longs)),
        "num_short": float(len(shorts)),
        "long_pnl": sum(t.pnl for t in longs) if longs else 0.0,
        "short_pnl": sum(t.pnl for t in shorts) if shorts else 0.0,
    }


def _max_consec(
    trades: list[TradeRecord],
    predicate: Callable[[TradeRecord], bool],
) -> int:
    """Longest run of consecutive trades satisfying *predicate*."""
    best = 0
    cur = 0
    for t in trades:
        if predicate(t):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


# ── Balance / drawdown metrics ───────────────────────────────────────────────


def compute_balance_metrics(
    balance: pd.Series,
    starting_capital: float,
    *,
    bar_interval_ns: int | None = None,
) -> dict[str, float]:
    """Drawdown anatomy + CAGR + MAR from an event-time balance series.

    Parameters
    ----------
    balance
        Series of balance values indexed by ``pd.DatetimeIndex`` (UTC).
        Must have at least one row.  Multiple rows per timestamp are
        collapsed to the last balance per timestamp before computing.
    starting_capital
        Initial deposit, used as a fallback peak when the series is
        sparse or as the denominator for percentage drawdowns at t=0.
    bar_interval_ns
        Bar interval in nanoseconds.  When provided,
        ``time_underwater_bars`` is reported; when ``None``, the value
        is reported in seconds and the key is ``time_underwater_secs``.

    Returns
    -------
    dict[str, float]
        Keys: ``max_drawdown_abs``, ``max_drawdown_pct``,
        ``recovery_factor``, ``cagr``, ``mar_ratio``,
        ``time_underwater_bars`` (or ``..._secs``), ``years_in_sample``.
        All numeric; missing/undefined values are ``nan``.

    """
    duration_unit = "bars" if bar_interval_ns is not None else "secs"
    keys = [
        "max_drawdown_abs", "max_drawdown_pct", "recovery_factor",
        "cagr", "mar_ratio", f"time_underwater_{duration_unit}",
        "years_in_sample",
    ]
    if balance.empty:
        return {k: float("nan") for k in keys}

    # Collapse duplicate timestamps to the last value per timestamp.
    s = balance.astype(float).groupby(balance.index).last().sort_index()
    if s.empty:
        return {k: float("nan") for k in keys}

    peak = s.cummax()
    drawdown_abs = peak - s
    drawdown_pct = drawdown_abs / peak.where(peak > 0, other=float("nan"))

    max_dd_abs = float(drawdown_abs.max())
    max_dd_pct = float(drawdown_pct.max()) if drawdown_pct.notna().any() else float("nan")

    final_balance = float(s.iloc[-1])
    initial = float(starting_capital)
    total_pnl = final_balance - initial

    # CAGR — only meaningful if we have ≥1 day of data and positive end balance.
    span_secs = (s.index[-1] - s.index[0]).total_seconds() if len(s) >= 2 else 0.0
    years_in_sample = span_secs / (365.25 * 24 * 3600) if span_secs > 0 else float("nan")
    if (
        math.isfinite(years_in_sample)
        and years_in_sample > 0
        and initial > 0
        and final_balance > 0
    ):
        cagr = (final_balance / initial) ** (1.0 / years_in_sample) - 1.0
    else:
        cagr = float("nan")

    mar_ratio = (
        cagr / max_dd_pct
        if (math.isfinite(cagr) and math.isfinite(max_dd_pct) and max_dd_pct > 0)
        else float("nan")
    )

    recovery_factor = _safe_div(total_pnl, max_dd_abs)

    # Time underwater: cumulative duration where balance < running peak.
    underwater_mask = s < peak
    if underwater_mask.any():
        # Sum durations of consecutive underwater stretches.  Each stretch
        # has duration (next_index - this_index) for points within it,
        # plus the last point's contribution is implicit (we use diff).
        idx = s.index
        gaps = idx.to_series().diff().dt.total_seconds().fillna(0.0)
        underwater_secs = float(gaps[underwater_mask].sum())
    else:
        underwater_secs = 0.0

    if bar_interval_ns:
        time_underwater = underwater_secs * 1_000_000_000 / bar_interval_ns
    else:
        time_underwater = underwater_secs

    return {
        "max_drawdown_abs": max_dd_abs,
        "max_drawdown_pct": max_dd_pct,
        "recovery_factor": recovery_factor,
        "cagr": cagr,
        "mar_ratio": mar_ratio,
        f"time_underwater_{duration_unit}": time_underwater,
        "years_in_sample": years_in_sample,
    }


# ── Activity / cost metrics ──────────────────────────────────────────────────


def compute_activity_metrics(
    trades: list[TradeRecord],
    *,
    total_bars: int | None = None,
    bar_interval_ns: int | None = None,
    first_bar_ts_ns: int | None = None,
    last_bar_ts_ns: int | None = None,
    total_fees: float | None = None,
    total_pnl: float | None = None,
) -> dict[str, float]:
    """Bars-in-market and fee-as-%-of-PnL.

    Computes ``bars_in_market_pct`` either from ``total_bars`` directly
    (preferred) or from ``first_bar_ts_ns`` + ``last_bar_ts_ns`` +
    ``bar_interval_ns``.  Returns ``nan`` for that key when none of the
    required inputs are available.

    ``fee_pct_of_pnl`` is ``total_fees / abs(total_pnl)``; ``nan`` when
    ``total_fees`` or ``total_pnl`` is missing or PnL is zero.  Note: a
    value > 1.0 means fees exceed gross PnL — strategy is fee-killed.

    """
    out = {
        "bars_in_market_pct": float("nan"),
        "fee_pct_of_pnl": float("nan"),
    }

    # Bars in market — assumes NETTING (non-overlapping positions).
    if trades:
        if total_bars is None and (
            bar_interval_ns and first_bar_ts_ns is not None and last_bar_ts_ns is not None
        ):
            span_ns = last_bar_ts_ns - first_bar_ts_ns + bar_interval_ns
            total_bars = max(int(span_ns / bar_interval_ns), 1)
        if total_bars and bar_interval_ns:
            in_market_ns = sum(t.ts_closed_ns - t.ts_opened_ns for t in trades)
            in_market_bars = in_market_ns / bar_interval_ns
            out["bars_in_market_pct"] = min(in_market_bars / total_bars, 1.0)

    if total_fees is not None and total_pnl is not None and total_pnl != 0:
        out["fee_pct_of_pnl"] = total_fees / abs(total_pnl)

    return out


# ── Convenience wrapper ──────────────────────────────────────────────────────


def bootstrap_total_pnl(
    trade_pnls: list[float],
    *,
    n_iterations: int = 10_000,
    seed: int | None = 42,
) -> dict[str, float]:
    """Bootstrap a confidence interval on total PnL.

    Resamples the per-trade PnL list with replacement ``n_iterations``
    times.  Each iteration sums the resampled trades and records the
    total.  Returns the distribution as a dict of summary stats.

    Why this matters: a single point estimate of total PnL ($9,510)
    tells you nothing about whether the strategy is robust or just
    lucky on this specific historical path.  Bootstrap CI gives you
    the dispersion: "9,510 with 95% CI [4,200, 14,800]" tells a much
    more honest story.

    Caveat: trade-return bootstrap **assumes IID trades**.  Real
    strategies have autocorrelation (winning streaks cluster, drawdowns
    cluster).  Block-bootstrap is more honest for that — use this as a
    first-pass dispersion estimate, not a true confidence interval.

    Parameters
    ----------
    trade_pnls
        Per-trade realized PnL (one float per closed trade).
    n_iterations
        Number of bootstrap samples.  Default 10,000.
    seed
        RNG seed for reproducibility.  Default 42.

    Returns
    -------
    dict[str, float]
        Keys: ``mean``, ``std``, ``pct_5``, ``pct_25``, ``median``,
        ``pct_75``, ``pct_95``, ``min``, ``max``, ``n_iterations``,
        ``n_trades``, ``actual_total``.

    """
    keys = [
        "mean", "std", "pct_5", "pct_25", "median", "pct_75", "pct_95",
        "min", "max", "n_iterations", "n_trades", "actual_total",
    ]
    if not trade_pnls:
        return dict.fromkeys(keys, float("nan"))

    arr = np.asarray(trade_pnls, dtype=float)
    n = len(arr)
    rng = np.random.default_rng(seed)
    # Vectorised resample: (n_iterations, n) random indices, then sum.
    idx = rng.integers(0, n, size=(n_iterations, n))
    samples = arr[idx].sum(axis=1)

    return {
        "mean": float(samples.mean()),
        "std": float(samples.std(ddof=1)) if n_iterations > 1 else 0.0,
        "pct_5": float(np.percentile(samples, 5)),
        "pct_25": float(np.percentile(samples, 25)),
        "median": float(np.median(samples)),
        "pct_75": float(np.percentile(samples, 75)),
        "pct_95": float(np.percentile(samples, 95)),
        "min": float(samples.min()),
        "max": float(samples.max()),
        "n_iterations": int(n_iterations),
        "n_trades": int(n),
        "actual_total": float(arr.sum()),
    }


def bootstrap_max_drawdown(
    trade_pnls: list[float],
    *,
    n_iterations: int = 10_000,
    seed: int | None = 42,
) -> dict[str, float]:
    """Bootstrap a confidence interval on max drawdown.

    Companion to :func:`bootstrap_total_pnl` — same resampling
    procedure, but for each resample we build a synthetic equity curve
    by cumulative-summing the resampled trades and record the worst
    peak-to-trough drawdown.  Returns the distribution as summary
    stats (95th percentile is the "bad-luck" tail you want for
    position-sizing decisions).

    Why this matters: PnL CI tells you "what could the total return
    be?" but drawdown CI tells you "what's the worst hole I might
    have to climb out of?".  Pros size positions to survive their
    drawdown CI, not their PnL CI.

    Caveat: same IID assumption as ``bootstrap_total_pnl``.  Real
    strategies cluster losers (e.g. choppy regime → run of stops),
    which produces deeper drawdowns than IID resampling captures.
    Treat the 95th-percentile drawdown as a **lower bound** on
    realistic worst-case, not a true upper bound.

    All drawdown values are returned as **negative** dollar amounts
    (the worst peak-to-trough loss along the synthetic equity curve);
    a flat or always-positive curve returns 0.

    Parameters
    ----------
    trade_pnls
        Per-trade realized PnL (one float per closed trade).
    n_iterations
        Number of bootstrap samples.  Default 10,000.
    seed
        RNG seed for reproducibility.  Default 42.

    Returns
    -------
    dict[str, float]
        Keys: ``mean``, ``std``, ``pct_5``, ``pct_25``, ``median``,
        ``pct_75``, ``pct_95``, ``min``, ``max``, ``n_iterations``,
        ``n_trades``, ``actual_max_drawdown``.  All drawdowns are
        non-positive; the **worst-case tail** is ``pct_5`` (most-
        negative), the **least-bad tail** is ``pct_95``.

    """
    keys = [
        "mean", "std", "pct_5", "pct_25", "median", "pct_75", "pct_95",
        "min", "max", "n_iterations", "n_trades", "actual_max_drawdown",
    ]
    if not trade_pnls:
        return dict.fromkeys(keys, float("nan"))

    arr = np.asarray(trade_pnls, dtype=float)
    n = len(arr)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_iterations, n))
    resampled = arr[idx]                              # (n_iterations, n)
    equity = np.cumsum(resampled, axis=1)             # synthetic curves
    # Running peak per-row, then drawdown = equity - running_peak.
    # For peak-from-zero start, prepend zero so initial losses register
    # as drawdown from the starting balance.
    equity_with_zero = np.concatenate(
        [np.zeros((n_iterations, 1)), equity], axis=1,
    )
    running_peak = np.maximum.accumulate(equity_with_zero, axis=1)
    drawdowns = equity_with_zero - running_peak       # ≤ 0 everywhere
    max_dd = drawdowns.min(axis=1)                    # most-negative per resample

    # Actual MDD on the historical trade order (no resampling)
    actual_equity = np.concatenate([[0.0], np.cumsum(arr)])
    actual_peak = np.maximum.accumulate(actual_equity)
    actual_mdd = float((actual_equity - actual_peak).min())

    # All drawdowns ≤ 0, so pct_5 is the *worst* tail (most negative)
    # and pct_95 is the *least bad* tail (closest to zero).  Display
    # code labels pct_5 as "worst case" — same convention as
    # bootstrap_total_pnl where pct_5 is the bad tail of total PnL.
    return {
        "mean": float(max_dd.mean()),
        "std": float(max_dd.std(ddof=1)) if n_iterations > 1 else 0.0,
        "pct_5": float(np.percentile(max_dd, 5)),
        "pct_25": float(np.percentile(max_dd, 25)),
        "median": float(np.median(max_dd)),
        "pct_75": float(np.percentile(max_dd, 75)),
        "pct_95": float(np.percentile(max_dd, 95)),
        "min": float(max_dd.min()),
        "max": float(max_dd.max()),
        "n_iterations": int(n_iterations),
        "n_trades": int(n),
        "actual_max_drawdown": actual_mdd,
    }


def compute_drawdown_periods(
    balance: pd.Series,
) -> list[dict[str, Any]]:
    """Decompose a balance series into discrete underwater periods.

    Walks the equity curve and returns one dict per drawdown — defined
    as a contiguous stretch where ``balance < running_peak``, ending
    when balance recovers to a new peak (or at the end of the sample
    if recovery never happens).

    Each period dict carries:

    * ``start`` (pd.Timestamp) — first underwater bar
    * ``end`` (pd.Timestamp) — recovery bar (or last bar if open)
    * ``trough`` (pd.Timestamp) — bar where the deepest drawdown hit
    * ``depth_abs`` (float) — peak − trough in absolute units
    * ``depth_pct`` (float) — depth / peak  (always positive)
    * ``duration_seconds`` (float) — end − start
    * ``recovered`` (bool) — True if recovered before end of sample

    Parameters
    ----------
    balance
        Event-time balance series indexed by ``pd.DatetimeIndex``.
        Multiple rows per timestamp are collapsed (last per timestamp).

    Returns
    -------
    list[dict[str, Any]]
        One entry per underwater period, in chronological order.  Empty
        list if no drawdowns (monotonic-up balance).

    """
    if balance.empty:
        return []
    s = balance.astype(float).groupby(balance.index).last().sort_index()
    if s.empty:
        return []

    peak = s.cummax()
    underwater = s < peak  # bool series

    periods: list[dict[str, Any]] = []
    in_dd = False
    start_idx: pd.Timestamp | None = None
    trough_idx: pd.Timestamp | None = None
    trough_depth_pct = 0.0

    for ts, is_uw in underwater.items():
        if is_uw and not in_dd:
            in_dd = True
            start_idx = ts
            trough_idx = ts
            depth = float(peak.loc[ts] - s.loc[ts])
            trough_depth_pct = depth / float(peak.loc[ts]) if peak.loc[ts] > 0 else 0.0
        elif is_uw and in_dd:
            depth_pct = (
                float(peak.loc[ts] - s.loc[ts]) / float(peak.loc[ts])
                if peak.loc[ts] > 0 else 0.0
            )
            if depth_pct > trough_depth_pct:
                trough_depth_pct = depth_pct
                trough_idx = ts
        elif not is_uw and in_dd:
            in_dd = False
            assert start_idx is not None and trough_idx is not None
            depth_abs = float(peak.loc[trough_idx] - s.loc[trough_idx])
            periods.append({
                "start": start_idx,
                "end": ts,
                "trough": trough_idx,
                "depth_abs": depth_abs,
                "depth_pct": trough_depth_pct,
                "duration_seconds": (ts - start_idx).total_seconds(),
                "recovered": True,
            })
            start_idx = trough_idx = None
            trough_depth_pct = 0.0

    # Open drawdown at end of sample
    if in_dd and start_idx is not None and trough_idx is not None:
        end = s.index[-1]
        depth_abs = float(peak.loc[trough_idx] - s.loc[trough_idx])
        periods.append({
            "start": start_idx,
            "end": end,
            "trough": trough_idx,
            "depth_abs": depth_abs,
            "depth_pct": trough_depth_pct,
            "duration_seconds": (end - start_idx).total_seconds(),
            "recovered": False,
        })

    return periods


def compute_all_metrics(
    trades: list[TradeRecord],
    balance: pd.Series,
    *,
    starting_capital: float,
    total_bars: int | None = None,
    bar_interval_ns: int | None = None,
    first_bar_ts_ns: int | None = None,
    last_bar_ts_ns: int | None = None,
    total_fees: float | None = None,
    total_pnl: float | None = None,
) -> dict[str, float]:
    """Run all three metric groups and merge the results.

    Convenience wrapper for ``run_single_backtest``.  All keys from the
    three sub-functions are returned in one flat dict; no key collisions.
    """
    out: dict[str, float] = {}
    out.update(compute_trade_metrics(trades, bar_interval_ns=bar_interval_ns))
    out.update(
        compute_balance_metrics(
            balance, starting_capital, bar_interval_ns=bar_interval_ns,
        ),
    )
    out.update(
        compute_activity_metrics(
            trades,
            total_bars=total_bars,
            bar_interval_ns=bar_interval_ns,
            first_bar_ts_ns=first_bar_ts_ns,
            last_bar_ts_ns=last_bar_ts_ns,
            total_fees=total_fees,
            total_pnl=total_pnl,
        ),
    )
    return out
