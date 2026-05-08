"""Notebook-private helpers for ``compare_sweeps.ipynb``.

Same convention as ``_validate_helpers.py`` — extracted to keep cells
short, not part of the project's public API.  Unit tests live in
``tests/unit/test_compare_helpers.py``.
"""

from __future__ import annotations

import pandas as pd


def short_sweep_label(label: str) -> str:
    """Trim full sweep label to instrument + interval for chart titles.

    Sweep labels are formatted as
    ``"<strategy> · <instrument> · <interval>"``.  When every loaded
    sweep uses the same strategy (the common case in
    ``compare_sweeps.ipynb``), the strategy prefix is redundant in
    per-sweep heatmap titles — drop it so 60+-character labels become
    readable 30-character ones.

    Returns the input unchanged if the label doesn't follow the
    expected format.
    """
    parts = label.split(" · ")
    return " · ".join(parts[-2:]) if len(parts) >= 2 else label


def build_stability_df(
    sweeps_dict: dict[str, pd.DataFrame],
    param_cols: list[str],
) -> tuple[pd.DataFrame, int]:
    """Concat eligible sweeps and aggregate per-combo stability stats.

    For each ``param_cols`` combo, computes:

    * ``avg_pnl_pct`` / ``min_pnl_pct`` / ``max_pnl_pct`` / ``std_pnl_pct``
    * ``sweep_count`` — how many sweeps included this combo
    * ``all_profitable`` — bool, was this combo profitable in every sweep

    Sorted descending by ``avg_pnl_pct``.

    Sweeps that don't contain all of ``param_cols`` are silently
    skipped, as are sweeps with duplicate ``(param_cols)`` rows
    (sensitivity sweeps that vary an extra param).

    Parameters
    ----------
    sweeps_dict
        Mapping of sweep label to v2-schema sweep DataFrame.
    param_cols
        List of param-column names to group by (e.g. ``["fast", "slow"]``).

    Returns
    -------
    (stability_df, n_sweeps)
        ``stability_df`` is the per-combo aggregate; ``n_sweeps`` is
        the number of sweeps that contributed to it (after filtering).
        Returns ``(empty_df, 0)`` if nothing eligible.

    """
    tagged: list[pd.DataFrame] = []
    for label, df in sweeps_dict.items():
        if not all(pc in df.columns for pc in param_cols):
            continue
        if df.duplicated(subset=param_cols).any():
            continue
        chunk = df[[*param_cols, "total_pnl", "total_pnl_pct"]].copy()
        chunk["_label"] = label
        tagged.append(chunk)

    if not tagged:
        return pd.DataFrame(), 0

    combined = pd.concat(tagged, ignore_index=True)
    stab = (
        combined
        .groupby(param_cols, as_index=False)
        .agg(
            avg_pnl_pct=("total_pnl_pct", "mean"),
            min_pnl_pct=("total_pnl_pct", "min"),
            max_pnl_pct=("total_pnl_pct", "max"),
            std_pnl_pct=("total_pnl_pct", "std"),
            sweep_count=("total_pnl_pct", "count"),
            all_profitable=("total_pnl", lambda x: bool((x > 0).all())),
        )
        .sort_values("avg_pnl_pct", ascending=False)
    )
    return stab, len(tagged)
