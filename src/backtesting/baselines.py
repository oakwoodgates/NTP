"""Comparison baselines for backtests.

Two industry-standard "does my strategy actually have alpha?" checks:

* :func:`buy_and_hold` — what would simply holding the underlying asset
  for the full sample period have produced, with no fees and no
  leverage?  A strategy with 900% PnL underperforming 1000% buy-and-hold
  is paying complexity for negative edge.
* :func:`random_entry_baseline` — Monte-Carlo a baseline that matches
  the strategy's trade count and average duration but enters at random
  bar indices (always long).  If your strategy can't beat the 95th
  percentile of this distribution, you don't have entry-timing alpha;
  you just have market-beta exposure.

Both are pure-Python — they take bars + a few numbers and return
plain summary dicts.  No NT engine spin-up here.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


def buy_and_hold(
    bars: list[Bar],
    *,
    starting_capital: float,
    fee_rate: float = 0.0,
    leverage: float = 1.0,
) -> dict[str, Any]:
    """Compute buy-and-hold PnL for the bar sample.

    Buys at the first bar's close, sells at the last bar's close.
    Models a single round-trip taker fee on entry and exit when
    ``fee_rate`` is non-zero.  Leverage scales the position notional
    (no margin enforcement — same simplification NT uses for
    backtests).

    Parameters
    ----------
    bars
        Bars covering the sample period.  Empty list yields a dict of
        nans.
    starting_capital
        Initial cash.  Used as the position-sizing budget at entry.
    fee_rate
        Round-trip fee modeled as ``fee_rate × notional`` on each side.
        Default 0 (frictionless baseline).  Pass your venue's taker
        fee to compare net-of-fees.
    leverage
        Multiplier on the position size.  Default 1.0 (no leverage).

    Returns
    -------
    dict[str, Any]
        Keys: ``pnl``, ``pnl_pct``, ``cagr``, ``entry_price``,
        ``exit_price``, ``return_pct`` (price-only, no leverage),
        ``years_in_sample``, ``max_drawdown_pct`` (peak-to-trough
        on bar-close equity curve), ``fees_paid``.

    """
    keys = [
        "pnl", "pnl_pct", "cagr", "entry_price", "exit_price",
        "return_pct", "years_in_sample", "max_drawdown_pct", "fees_paid",
    ]
    if not bars or len(bars) < 2:
        return dict.fromkeys(keys, float("nan"))

    entry_px = float(bars[0].close.as_double())
    exit_px = float(bars[-1].close.as_double())
    if entry_px <= 0:
        return dict.fromkeys(keys, float("nan"))

    return_pct = (exit_px - entry_px) / entry_px

    # Position size — leverage'd notional measured in units of the asset.
    qty = starting_capital * leverage / entry_px
    gross_pnl = qty * (exit_px - entry_px)

    # Round-trip fees on the leveraged notional.
    notional_in = qty * entry_px
    notional_out = qty * exit_px
    fees_paid = (notional_in + notional_out) * fee_rate

    pnl = gross_pnl - fees_paid
    pnl_pct = pnl / starting_capital * 100 if starting_capital > 0 else 0.0

    # CAGR
    span_secs = bars[-1].ts_event - bars[0].ts_event
    years = span_secs / (365.25 * 24 * 3600 * 1e9)
    final_cap = starting_capital + pnl
    if years > 0 and starting_capital > 0 and final_cap > 0:
        cagr = (final_cap / starting_capital) ** (1.0 / years) - 1.0
    else:
        cagr = float("nan")

    # Max drawdown on the bar-close equity curve
    closes = np.array([float(b.close.as_double()) for b in bars], dtype=float)
    equity = starting_capital + qty * (closes - entry_px) - fees_paid * (
        np.arange(len(closes)) >= len(closes) - 1
    )
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / np.where(peak > 0, peak, np.nan)
    max_dd = float(np.nanmax(drawdown)) if drawdown.size > 0 else 0.0

    return {
        "pnl": float(pnl),
        "pnl_pct": float(pnl_pct),
        "cagr": float(cagr) if math.isfinite(cagr) else float("nan"),
        "entry_price": entry_px,
        "exit_price": exit_px,
        "return_pct": float(return_pct),
        "years_in_sample": float(years) if years > 0 else float("nan"),
        "max_drawdown_pct": max_dd,
        "fees_paid": float(fees_paid),
    }


def random_entry_baseline(
    bars: list[Bar],
    *,
    n_trades: int,
    avg_duration_bars: float,
    starting_capital: float,
    notional_per_trade: float,
    fee_rate: float = 0.0,
    n_simulations: int = 1000,
    seed: int | None = 42,
) -> dict[str, Any]:
    """Monte-Carlo a random-entry baseline matched to your strategy.

    For ``n_simulations`` runs, opens long at random bar indices for the
    given count and average duration, computes per-trade PnL from the
    bar-close price change, sums to a total, and returns the distribution.

    All entries are long-only — the question is "does my entry timing
    add value over coin-flips?" not "does my strategy direction add
    value" (long/short attribution is already captured in the trade
    metrics).

    Compare your strategy's actual ``total_pnl`` against the percentile
    bands.  If your strategy is below the 95th percentile, you have no
    entry-timing edge.

    Parameters
    ----------
    bars
        Bars to sample entries from.
    n_trades
        Number of trades to simulate per run.  Match your strategy's
        ``num_positions``.
    avg_duration_bars
        Target average trade duration (bars).  Each random trade's
        duration is jittered ±50% around this.  Match your strategy's
        ``avg_trade_duration_bars``.
    starting_capital
        For ``pnl_pct`` calculation.
    notional_per_trade
        Position size per trade (constant).  Match your strategy's
        sizing — for fixed-notional, this is ``trade_notional``.
    fee_rate
        Round-trip taker fee per side.  Default 0.
    n_simulations
        Monte-Carlo sample count.  Default 1000.
    seed
        RNG seed for reproducibility.  Default 42.

    Returns
    -------
    dict[str, Any]
        Keys: ``mean_pnl``, ``median_pnl``, ``std_pnl``,
        ``pct_5``, ``pct_25``, ``pct_75``, ``pct_95``,
        ``min_pnl``, ``max_pnl``, ``win_rate_mean``,
        ``n_simulations``, ``trades_per_sim``.

    """
    keys = [
        "mean_pnl", "median_pnl", "std_pnl",
        "pct_5", "pct_25", "pct_75", "pct_95",
        "min_pnl", "max_pnl", "win_rate_mean",
        "n_simulations", "trades_per_sim",
    ]
    if not bars or n_trades <= 0 or avg_duration_bars <= 0:
        return dict.fromkeys(keys, float("nan"))

    closes = np.array([float(b.close.as_double()) for b in bars], dtype=float)
    n_bars = len(closes)
    if n_bars < 2:
        return dict.fromkeys(keys, float("nan"))

    rng = np.random.default_rng(seed)
    duration_low = max(1, int(avg_duration_bars * 0.5))
    duration_high = max(duration_low + 1, int(avg_duration_bars * 1.5))

    sim_pnls: list[float] = []
    sim_win_rates: list[float] = []

    for _ in range(n_simulations):
        # Pick n_trades random open indices that leave room for the
        # average duration before the end of bars.  If n_bars is too
        # small to fit, we clamp duration.
        max_open_idx = max(1, n_bars - duration_low)
        open_idx = rng.integers(0, max_open_idx, size=n_trades)
        durations = rng.integers(duration_low, duration_high + 1, size=n_trades)
        close_idx = np.minimum(open_idx + durations, n_bars - 1)

        # Per-trade PnL: notional × (exit_px / entry_px - 1) - fees
        entry_pxs = closes[open_idx]
        exit_pxs = closes[close_idx]
        pct_returns = (exit_pxs - entry_pxs) / np.where(entry_pxs > 0, entry_pxs, np.nan)
        per_trade_pnls = notional_per_trade * pct_returns

        if fee_rate > 0:
            per_trade_fees = (
                notional_per_trade * fee_rate * 2  # round trip
            )
            per_trade_pnls = per_trade_pnls - per_trade_fees

        # Filter NaN trades (shouldn't happen but defensive)
        per_trade_pnls = per_trade_pnls[~np.isnan(per_trade_pnls)]
        if len(per_trade_pnls) == 0:
            sim_pnls.append(0.0)
            sim_win_rates.append(0.0)
            continue

        sim_pnls.append(float(per_trade_pnls.sum()))
        sim_win_rates.append(float((per_trade_pnls > 0).mean()))

    sim_arr = np.array(sim_pnls)
    return {
        "mean_pnl": float(sim_arr.mean()),
        "median_pnl": float(np.median(sim_arr)),
        "std_pnl": float(sim_arr.std(ddof=1)) if len(sim_arr) > 1 else 0.0,
        "pct_5": float(np.percentile(sim_arr, 5)),
        "pct_25": float(np.percentile(sim_arr, 25)),
        "pct_75": float(np.percentile(sim_arr, 75)),
        "pct_95": float(np.percentile(sim_arr, 95)),
        "min_pnl": float(sim_arr.min()),
        "max_pnl": float(sim_arr.max()),
        "win_rate_mean": float(np.mean(sim_win_rates)),
        "n_simulations": int(n_simulations),
        "trades_per_sim": int(n_trades),
    }
