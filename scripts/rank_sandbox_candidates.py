"""Headless ranker: pick the best MACross config for sandbox deployment.

Reads the sweep parquets from a batch run, applies sandbox-suitability
filters (trade frequency, profit factor, no liquidation, etc.), then
ranks (fast, slow, interval) combos by cross-instrument robustness:

1. Profitable in the most instruments
2. Highest mean PnL%/year across instruments
3. Lowest cross-instrument coefficient-of-variation (most consistent)

Prints a short-list and a per-instrument detail table for the top combo.
The output is meant to seed the docs/SANDBOX_CONFIG_DECISION.md write-up.

Usage::

    python scripts/rank_sandbox_candidates.py \\
        --sweep-dir data/sweeps/20260512_172234 \\
        --min-trades-per-year 30 \\
        --min-profit-factor 1.3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def load_sweeps(sweep_dir: Path) -> pd.DataFrame:
    """Load every .parquet in sweep_dir into one long DataFrame.

    Adds ``_asset`` (BTC/ETH/SOL) and ``_interval`` (1h/4h) columns
    parsed from the filename. Filenames follow the
    ``EMA_{ASSET}_HYPERLIQUID_PERP_{INTERVAL}_stop{N}.parquet`` pattern
    produced by ``scripts/batch_backtest.py``.
    """
    rows: list[pd.DataFrame] = []
    for pq in sorted(sweep_dir.glob("*.parquet")):
        df = pd.read_parquet(pq)
        # Parse filename: EMA_BTC_HYPERLIQUID_PERP_4h_stop5.parquet
        parts = pq.stem.split("_")
        # parts = ['EMA', 'BTC', 'HYPERLIQUID', 'PERP', '4h', 'stop5']
        df["_asset"] = parts[1]
        df["_interval"] = parts[4]
        df["_stop_pct"] = float(parts[5].removeprefix("stop")) / 100
        df["_source_file"] = pq.name
        rows.append(df)
    if not rows:
        raise SystemExit(f"No .parquet files in {sweep_dir}")
    return pd.concat(rows, ignore_index=True)


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived columns the ranker uses.

    - ``trades_per_year`` from num_positions / years_in_sample
    - ``pnl_per_year_pct`` from total_pnl_pct / years_in_sample
    - ``has_liquidation`` from {liquidated_positions, liquidated_account, denied_post_halt}
    """
    df = df.copy()
    df["trades_per_year"] = (
        df["num_positions"] / df["years_in_sample"]
    ).round(1)
    df["pnl_per_year_pct"] = (
        df["total_pnl_pct"] / df["years_in_sample"]
    ).round(2)
    df["has_liquidation"] = (
        (df["liquidated_positions"] > 0)
        | df["liquidated_account"]
        | (df["denied_post_halt"] > 0)
    )
    return df


def filter_sandbox_eligible(
    df: pd.DataFrame,
    *,
    min_trades_per_year: float,
    min_profit_factor: float,
) -> pd.DataFrame:
    """Apply the hard filters that disqualify a combo from sandbox deployment.

    Sandbox-suitability ≠ "highest PnL" — we want a combo that:
    1. Actually trades (≥N trades/year) so we exercise the pipeline
    2. Has a credible edge (PF ≥ threshold)
    3. Didn't blow up in any instrument (no liquidation events)
    4. Had a non-error backtest run
    """
    mask = (
        (df["error"].fillna("") == "")
        & (df["trades_per_year"] >= min_trades_per_year)
        & (df["pnl_profit_factor"] >= min_profit_factor)
        & (~df["has_liquidation"])
    )
    return df[mask].copy()


def rank_by_robustness(
    eligible: pd.DataFrame,
) -> pd.DataFrame:
    """Group eligible combos by (interval, fast, slow) and rank cross-instrument.

    Returns one row per combo with:
    - ``n_profitable``: how many instruments showed pnl > 0
    - ``mean_pnl_pct_per_year``: average across instruments
    - ``cv_pnl_pct``: coefficient of variation (std / |mean|) — lower = more consistent
    - ``min_pf``, ``max_drawdown_pct_worst``: worst-instrument metrics
    - ``trades_per_year_mean``: average trade frequency

    Sort order: n_profitable desc, mean_pnl desc, cv asc, max_drawdown asc.
    """
    grouped = eligible.groupby(["_interval", "fast", "slow"])
    rows = []
    for (interval, fast, slow), g in grouped:
        n_total = len(g)
        n_profitable = int((g["total_pnl_pct"] > 0).sum())
        mean_pnl = g["pnl_per_year_pct"].mean()
        std_pnl = g["pnl_per_year_pct"].std() if n_total > 1 else 0.0
        cv = std_pnl / abs(mean_pnl) if mean_pnl != 0 else float("inf")
        rows.append({
            "interval": interval,
            "fast": int(fast),
            "slow": int(slow),
            "n_instruments_eligible": n_total,
            "n_profitable": n_profitable,
            "mean_pnl_pct_per_year": round(mean_pnl, 2),
            "cv_pnl_pct": round(cv, 3),
            "min_pf": round(g["pnl_profit_factor"].min(), 3),
            "max_drawdown_pct_worst": round(g["max_drawdown_pct"].max(), 3),
            "trades_per_year_mean": round(g["trades_per_year"].mean(), 1),
        })
    summary = pd.DataFrame(rows)
    if summary.empty:
        return summary
    summary = summary.sort_values(
        ["n_profitable", "mean_pnl_pct_per_year", "cv_pnl_pct", "max_drawdown_pct_worst"],
        ascending=[False, False, True, True],
    ).reset_index(drop=True)
    return summary


def show_top_detail(
    eligible: pd.DataFrame,
    interval: str,
    fast: int,
    slow: int,
) -> pd.DataFrame:
    """Per-instrument detail rows for one specific combo."""
    mask = (
        (eligible["_interval"] == interval)
        & (eligible["fast"] == fast)
        & (eligible["slow"] == slow)
    )
    cols = [
        "_asset", "num_positions", "trades_per_year",
        "total_pnl_pct", "pnl_per_year_pct",
        "pnl_profit_factor", "Win Rate", "max_drawdown_pct",
        "mar_ratio", "avg_trade_duration_bars",
    ]
    return eligible[mask][cols].sort_values("_asset").reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sweep-dir", required=True,
        help="Directory of MACross sweep parquets from batch_backtest.py.",
    )
    parser.add_argument(
        "--min-trades-per-year", type=float, default=30,
        help="Hard filter — combos below this are disqualified.",
    )
    parser.add_argument(
        "--min-profit-factor", type=float, default=1.3,
        help="Hard filter — combos below this PF are disqualified.",
    )
    parser.add_argument(
        "--top-n", type=int, default=10,
        help="Print this many ranked combos before the detail dive.",
    )
    args = parser.parse_args()

    sweep_dir = Path(args.sweep_dir)
    if not sweep_dir.exists():
        print(f"Sweep dir not found: {sweep_dir}")
        return 1

    raw = load_sweeps(sweep_dir)
    annotated = annotate(raw)

    print(f"Loaded {len(annotated):>5} rows from {len(annotated['_source_file'].unique())} sweep files")
    print(f"Instruments: {sorted(annotated['_asset'].unique())}")
    print(f"Intervals  : {sorted(annotated['_interval'].unique())}")
    n_combos = annotated.groupby(['_asset', '_interval']).size().min()
    print(f"Grid combos per sweep (min): {n_combos}")

    eligible = filter_sandbox_eligible(
        annotated,
        min_trades_per_year=args.min_trades_per_year,
        min_profit_factor=args.min_profit_factor,
    )
    filter_pct = 100 * len(eligible) / len(annotated) if len(annotated) else 0
    print(
        f"\nEligible after filters ({args.min_trades_per_year}+ trades/yr, "
        f"{args.min_profit_factor}+ PF, no liq): "
        f"{len(eligible)} / {len(annotated)} "
        f"({filter_pct:.0f}%)",
    )

    if eligible.empty:
        print(
            "\nNo combos survived. Try relaxing --min-trades-per-year or "
            "--min-profit-factor, or investigate why every combo liquidated.",
        )
        return 2

    summary = rank_by_robustness(eligible)
    print(
        f"\n=== Top {args.top_n} cross-instrument robust combos ===",
    )
    print(summary.head(args.top_n).to_string(index=False))

    print("\n=== Per-instrument detail for top combo ===")
    top = summary.iloc[0]
    print(
        f"interval={top['interval']}  fast={int(top['fast'])}  slow={int(top['slow'])}  "
        f"mean PnL%/yr={top['mean_pnl_pct_per_year']}  cv={top['cv_pnl_pct']}",
    )
    detail = show_top_detail(eligible, top["interval"], int(top["fast"]), int(top["slow"]))
    print(detail.to_string(index=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
