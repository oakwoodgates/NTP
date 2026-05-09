"""Notebook-private helpers for ``validate_strategy.ipynb``.

This module is **not** part of the project's public API.  Functions here
are extracted from the notebook only to keep cells short — they are
re-executed in-kernel on every Run All, not imported by other notebooks
or production code.

The leading-underscore filename signals "private to ``notebooks/``";
unit tests live in ``tests/unit/test_validate_helpers.py`` to keep
coverage honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.strategies.bb_meanrev import BBMeanRev, BBMeanRevConfig
from src.strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutConfig
from src.strategies.ma_cross import MACross, MACrossConfig
from src.strategies.ma_cross_atr import MACrossATR, MACrossATRConfig
from src.strategies.ma_cross_bracket import MACrossBracket, MACrossBracketConfig
from src.strategies.ma_cross_long_only import MACrossLongOnly, MACrossLongOnlyConfig
from src.strategies.ma_cross_stop_entry import MACrossStopEntry, MACrossStopEntryConfig
from src.strategies.ma_cross_take_profit import MACrossTakeProfit, MACrossTakeProfitConfig
from src.strategies.ma_cross_trailing_stop import (
    MACrossTrailingStop,
    MACrossTrailingStopConfig,
)
from src.strategies.macd_rsi import MACDRSI, MACDRSIConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

# Try absolute import first (when notebooks/ is on sys.path), then
# fall back to inserting the parent dir on sys.path.  Lets the same
# module work whether imported from a notebook or from tests.
try:
    from utils import wilson_score_interval
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils import wilson_score_interval


# ─────────────────────────────────────────────────────────────────────────────
# Strategy registry — declarative table of (Class, Config, param_map,
# fixed_params).  param_map only carries keys that DIFFER from the
# config field names; identity-mapped keys (bb_period, atr_period, ...)
# pass through unchanged.
# ─────────────────────────────────────────────────────────────────────────────

StrategyEntry = tuple[type, type, dict[str, str], dict[str, Any]]

STRATEGIES: dict[str, StrategyEntry] = {
    "MACross-EMA":    (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "EMA"}),
    "MACross-SMA":    (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "SMA"}),
    "MACross-HMA":    (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "HMA"}),
    "MACross-DEMA":   (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "DEMA"}),
    "MACross-AMA":    (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "AMA"}),
    "MACross-VIDYA":  (MACross,    MACrossConfig,    {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "VIDYA"}),
    "MACrossLongOnly":     (MACrossLongOnly,     MACrossLongOnlyConfig,     {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "EMA"}),
    "MACrossTrailingStop": (MACrossTrailingStop, MACrossTrailingStopConfig, {"fast": "fast_period", "slow": "slow_period", "trailing_mult": "trailing_atr_multiple"}, {"ma_type": "EMA"}),
    "MACrossATR":          (MACrossATR,          MACrossATRConfig,          {"fast": "fast_period", "slow": "slow_period", "atr_sl": "atr_sl_multiplier", "atr_tp": "atr_tp_multiplier"}, {"ma_type": "EMA"}),
    "MACrossTakeProfit":           (MACrossTakeProfit,           MACrossTakeProfitConfig,           {"fast": "fast_period", "slow": "slow_period"}, {"ma_type": "EMA"}),
    "MACrossStopEntry":    (MACrossStopEntry,    MACrossStopEntryConfig,    {"fast": "fast_period", "slow": "slow_period", "trail_mult": "trailing_atr_multiple"}, {"ma_type": "EMA"}),
    "MACrossBracket":      (MACrossBracket,      MACrossBracketConfig,      {"fast": "fast_period", "slow": "slow_period", "bracket_dist": "bracket_distance_atr"}, {"ma_type": "EMA"}),
    "BBMeanRev":           (BBMeanRev,           BBMeanRevConfig,           {"rsi_buy": "rsi_buy_threshold", "rsi_sell": "rsi_sell_threshold"}, {}),
    "MACDRSI":             (MACDRSI,             MACDRSIConfig,             {"fast": "macd_fast_period", "slow": "macd_slow_period", "signal": "macd_signal_period", "rsi_ob": "rsi_overbought", "rsi_os": "rsi_oversold", "rsi_entry": "rsi_entry_threshold"}, {}),
    "DonchianBreakout":    (DonchianBreakout,    DonchianBreakoutConfig,    {}, {}),
}


def make_strategy_factory(
    strategy: str,
    instrument_id: str,
    bar_type_str: str,
    trade_notional: Decimal,
) -> Callable[[Any, dict[str, Any]], None]:
    """Build a closure that instantiates ``strategy`` with given params.

    The returned callable matches the signature ``run_sweep`` /
    ``run_walk_forward`` expects: ``factory(engine, params_dict)`` →
    side-effect (adds the strategy to the engine).  This is the v2
    convention from the reference notebook ``ma_cross.ipynb``.

    Parameters
    ----------
    strategy
        Registry key (e.g. ``"MACross-EMA"``).  Looked up in
        :data:`STRATEGIES`.
    instrument_id
        NT instrument-id string (e.g. ``"BTCUSDT-PERP.BINANCE"``).
    bar_type_str
        NT bar-type spec (e.g. ``"BTCUSDT-PERP.BINANCE-1-DAY-LAST-INTERNAL"``).
    trade_notional
        Fixed-notional trade size as a ``Decimal`` (e.g. ``Decimal(2000)``).

    Returns
    -------
    Callable[[BacktestEngine, dict], None]
        The factory; binds the four args above into a closure.

    Raises
    ------
    KeyError
        If ``strategy`` is not in :data:`STRATEGIES`.

    """
    cls, cfg_cls, param_map, fixed = STRATEGIES[strategy]

    def factory(eng: Any, params: dict[str, Any]) -> None:
        mapped = {param_map.get(k, k): v for k, v in params.items()}
        cfg = cfg_cls(
            instrument_id=InstrumentId.from_str(instrument_id),
            bar_type=BarType.from_str(bar_type_str),
            trade_notional=trade_notional,
            **fixed,
            **mapped,
        )
        eng.add_strategy(cls(cfg))

    return factory


# ─────────────────────────────────────────────────────────────────────────────
# Param grid per strategy — used by walk-forward + plateau heatmap.
# Returns ``(param_combos, row_param, col_param)``.
# ─────────────────────────────────────────────────────────────────────────────

ParamGrid = tuple[list[dict[str, Any]], str, str]


def get_param_grid(strategy: str) -> ParamGrid:
    """Resolve the (combos, row_param, col_param) tuple for a strategy.

    Centralises the per-strategy grid choices — fast/slow ranges for MA
    crossovers, period/std for BBMeanRev, etc.  When you bump a grid,
    update it once here rather than touching the notebook.

    Parameters
    ----------
    strategy
        Registry key from :data:`STRATEGIES`.

    Returns
    -------
    (combos, row_param, col_param)
        ``combos`` is a list of param dicts to sweep over.
        ``row_param`` / ``col_param`` are the column names used as the
        y- and x-axis of the plateau heatmap.

    """
    match strategy:
        case ("MACross-EMA" | "MACross-SMA" | "MACross-HMA"
              | "MACross-DEMA" | "MACross-AMA" | "MACross-VIDYA"
              | "MACrossLongOnly" | "MACrossATR" | "MACrossTakeProfit"):
            fast = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100]
            slow = [10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100, 200]
            combos = [{"fast": f, "slow": s} for f in fast for s in slow if f < s]
            return combos, "slow", "fast"
        case "MACrossTrailingStop":
            fast = [5, 10, 15, 20, 25]
            slow = [15, 20, 30, 40, 50]
            combos = [{"fast": f, "slow": s} for f in fast for s in slow if f < s]
            return combos, "slow", "fast"
        case "MACrossStopEntry" | "MACrossBracket":
            fast = [5, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45, 50]
            slow = [10, 15, 20, 25, 30, 35, 40, 45, 50, 75, 100, 200]
            combos = [{"fast": f, "slow": s} for f in fast for s in slow if f < s]
            return combos, "slow", "fast"
        case "BBMeanRev":
            periods = [15, 20, 25, 30]
            stds    = [1.5, 2.0, 2.5, 3.0]
            bb_combos: list[dict[str, Any]] = [
                {"bb_period": p, "bb_std": s}
                for p in periods for s in stds
            ]
            return bb_combos, "bb_std", "bb_period"
        case "MACDRSI":
            fast = [8, 12, 16]
            slow = [20, 26, 34]
            combos = [{"fast": f, "slow": s} for f in fast for s in slow if f < s]
            return combos, "slow", "fast"
        case "DonchianBreakout":
            periods = [10, 15, 20, 25, 30, 40, 50]
            combos = [{"dc_period": p} for p in periods]
            return combos, "dc_period", "dc_period"
        case _:
            msg = f"No param grid for {strategy!r}"
            raise ValueError(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Plateau analysis (consumed by section 2 of the validate notebook)
# ─────────────────────────────────────────────────────────────────────────────


def collapse_to_grid(
    df: pd.DataFrame, row_col: str, col_col: str,
) -> pd.DataFrame:
    """Reduce a sweep DataFrame to one row per (row_col, col_col) pair.

    Plateau analysis pivots on a 2-D grid so it needs unique
    ``(row_col, col_col)`` pairs.  When the sweep contains additional
    param dimensions (e.g. ``MACrossATR`` with atr_sl + atr_tp on top
    of fast + slow), this collapses to the best ``total_pnl`` per cell
    so the heatmap is well-defined.

    No-op when the sweep already has unique pairs.
    """
    if not df.duplicated(subset=[row_col, col_col]).any():
        return df
    return df.loc[
        df.groupby([row_col, col_col])["total_pnl"].idxmax()
    ].reset_index(drop=True)


def plateau_scores(
    df: pd.DataFrame, row_col: str, col_col: str,
    *, value_col: str = "total_pnl",
) -> pd.DataFrame:
    """Compute neighbour-profitability score for each grid cell.

    For each ``(row_col, col_col)`` cell, examines its 3×3 neighbour
    window (8 neighbours plus self) and scores the fraction that are
    profitable.  Used to detect plateaus (smooth profitable regions —
    high score) vs spikes (isolated peaks — low score).

    Parameters
    ----------
    df
        Sweep DataFrame.  Must already be collapsed to one row per
        (row_col, col_col) — see :func:`collapse_to_grid`.
    row_col, col_col
        Param-column names used as grid axes.
    value_col
        Column to score on.  Default ``"total_pnl"``.

    Returns
    -------
    pd.DataFrame
        Copy of ``df`` with three new columns: ``profitable``,
        ``neighbour_score`` (0–1 fraction), ``neighbour_avg`` (mean
        ``value_col`` over neighbours).

    """
    pivot = df.pivot(index=row_col, columns=col_col, values=value_col)
    row_vals = list(pivot.index)
    col_vals = list(pivot.columns)

    scores: dict[tuple[Any, Any], dict[str, float]] = {}
    for ri, rv in enumerate(row_vals):
        for ci, cv in enumerate(col_vals):
            val = pivot.iloc[ri, ci]
            if pd.isna(val):
                continue
            nbrs: list[float] = []
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = ri + dr, ci + dc
                    if 0 <= nr < len(row_vals) and 0 <= nc < len(col_vals):
                        nv = pivot.iloc[nr, nc]
                        if not pd.isna(nv):
                            nbrs.append(float(nv))
            n_pos = sum(1 for n in nbrs if n > 0)
            scores[(rv, cv)] = {
                "neighbour_score": n_pos / len(nbrs) if nbrs else 0.0,
                "neighbour_avg":   float(np.mean(nbrs)) if nbrs else 0.0,
            }

    out = df.copy()
    out["profitable"] = out[value_col] > 0
    out["neighbour_score"] = out.apply(
        lambda r: scores.get(
            (r[row_col], r[col_col]), {},
        ).get("neighbour_score", 0.0),
        axis=1,
    )
    out["neighbour_avg"] = out.apply(
        lambda r: scores.get(
            (r[row_col], r[col_col]), {},
        ).get("neighbour_avg", 0.0),
        axis=1,
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Filename helpers (consumed by RESULT_NAME composition in cell 1.1)
# ─────────────────────────────────────────────────────────────────────────────


def short_param_key(key: str) -> str:
    """Compact a param-dict key for use in filenames.

    Takes the first letter of each underscore-separated word, so:

    * ``"fast"`` → ``"f"``
    * ``"slow"`` → ``"s"``
    * ``"bb_period"`` → ``"bp"``
    * ``"bb_std"`` → ``"bs"``
    * ``"dc_period"`` → ``"dp"``
    * ``"atr_sl"`` → ``"as"``
    * ``"atr_tp"`` → ``"at"``
    * ``"trailing_mult"`` → ``"tm"``

    Used to generate the ``_OVERRIDE_TAG`` suffix in ``RESULT_NAME``
    so validate snapshots match the compact backtest filename style
    (``..._f10_s40_<ts>.html``) rather than verbose
    (``..._fast10_slow20_<ts>.html``).

    All compactions are unique within each strategy in
    :data:`STRATEGIES`, so the round-trip is unambiguous per-strategy.
    """
    return "".join(w[0] for w in key.split("_") if w)


def short_params_tag(params: dict[str, Any]) -> str:
    """Render a params dict as a compact filename tag.

    Joins ``short_param_key(k) + str(v)`` with underscores.  Returns
    empty string for an empty dict.  Examples::

        {"fast": 10, "slow": 20}              → "f10_s20"
        {"bb_period": 20, "bb_std": 2.0}      → "bp20_bs2.0"
        {"dc_period": 20}                     → "dp20"
        {}                                    → ""

    """
    return "_".join(f"{short_param_key(k)}{v}" for k, v in params.items())


# ─────────────────────────────────────────────────────────────────────────────
# Trade-PnL parsing (consumed by section 4.1 of the validate notebook)
# ─────────────────────────────────────────────────────────────────────────────


def parse_pnl(val: Any) -> float:
    """Parse a NT-formatted PnL string like ``'123.45 USDC'`` to a float.

    NT's ``positions_report`` returns realized_pnl as currency-suffixed
    strings; this drops the suffix.  Returns NaN for missing values.
    """
    if val is None or str(val) in ("None", "nan", "NaT"):
        return float("nan")
    try:
        return float(str(val).split()[0])
    except (ValueError, IndexError):
        return float("nan")


# ─────────────────────────────────────────────────────────────────────────────
# Regime enrichment (consumed by section 8 of the validate notebook)
# ─────────────────────────────────────────────────────────────────────────────


def enrich_regime_with_wilson(regime_perf: pd.DataFrame) -> pd.DataFrame:
    """Add 95% Wilson CI on win-rate to a regime-perf DataFrame.

    Returns a copy of ``regime_perf`` with one extra column:

    * ``wr_ci`` — string formatted ``"[lo, hi]"`` of the Wilson 95%
      interval, or ``"—"`` when ``num_positions == 0``.

    Why a separate column rather than 2 numeric ones: the regime
    DataFrame is meant to be displayed inline; a single string column
    reads better than two ``wr_lo`` / ``wr_hi`` floats.
    """
    out = regime_perf.copy()

    def _ci_str(row: pd.Series) -> str:
        n = int(row["num_positions"])
        if n <= 0:
            return "—"
        successes = int(round(row["win_rate"] * n))
        lo, hi = wilson_score_interval(successes, n)
        return f"[{lo:.2f}, {hi:.2f}]"

    out["wr_ci"] = out.apply(_ci_str, axis=1)
    return out
