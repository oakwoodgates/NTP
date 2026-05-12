"""Shared notebook utilities."""

from __future__ import annotations

import math
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nbformat
from nbconvert import HTMLExporter

if TYPE_CHECKING:
    import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def make_instrument_id(asset: str, data_source: str) -> str:
    """Build an instrument ID string for the given data source.

    Accepts both qualified names (``"BINANCE_PERP"``) and legacy
    unqualified names (``"BINANCE"``) for backward compatibility
    with un-migrated notebooks.

    Examples::

        HYPERLIQUID_PERP → BTC-USD-PERP.HYPERLIQUID
        BINANCE_PERP     → BTCUSDT-PERP.BINANCE
        BINANCE_SPOT     → BTCUSDT.BINANCE

    """
    if data_source in ("HYPERLIQUID", "HYPERLIQUID_PERP"):
        return f"{asset}-USD-PERP.HYPERLIQUID"
    if data_source in ("BINANCE", "BINANCE_PERP"):
        return f"{asset}USDT-PERP.BINANCE"
    if data_source == "BINANCE_SPOT":
        return f"{asset}USDT.BINANCE"
    raise ValueError(f"Unknown data source: {data_source!r}")

def save_tearsheet(html: str, result_name: str) -> Path:
    """Save a tearsheet HTML string to reports/tearsheets/."""
    results_dir = _PROJECT_ROOT / "reports" / "tearsheets"
    results_dir.mkdir(exist_ok=True, parents=True)
    dest = results_dir / f"{result_name}_tearsheet.html"
    dest.write_text(html, encoding="utf-8")
    print(f"Tearsheet saved → {dest}")
    return dest


def save_notebook(
    notebook_filename: str,
    result_filename: str,
    results_dir: str | Path | None = None,
    category: str = "backtest",
) -> Path:
    """Copy a notebook (with outputs) to the results directory.

    Save the notebook (Ctrl+S) before calling this so outputs are on disk.

    Parameters
    ----------
    notebook_filename
        Source notebook filename (e.g., ``"sma_cross.ipynb"``).
    result_filename
        Descriptive name without extension or timestamp
        (e.g., ``"SMACross_BTCUSDT-PERP.BINANCE_4h_f15_s25"``).
        A timestamp is appended automatically.
    results_dir
        Target directory. Created if it doesn't exist.
        Defaults to ``reports/notebooks/{category}``.
    category
        Subdirectory under ``reports/notebooks/`` (e.g., ``"backtest"``,
        ``"validate"``). Ignored when *results_dir* is provided.

    Returns
    -------
    Path
        The destination file path.

    """
    if results_dir is None:
        results_dir = _PROJECT_ROOT / "reports" / "notebooks" / category
    results_path = Path(results_dir)
    results_path.mkdir(exist_ok=True, parents=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = results_path / f"{result_filename}_{timestamp}.ipynb"

    shutil.copy2(notebook_filename, dest)
    print(f"Saved -> {dest}")
    return dest


def save_notebook_html(
    notebook_filename: str,
    result_filename: str,
    results_dir: str | Path | None = None,
    category: str = "backtest",
) -> Path:
    """Export a notebook to a self-contained HTML file in the results directory.

    Save the notebook (Ctrl+S) before calling this so outputs are on disk.

    Parameters
    ----------
    notebook_filename
        Source notebook filename (e.g., ``"sma_cross.ipynb"``).
    result_filename
        Descriptive name without extension or timestamp
        (e.g., ``"SMACross_BTCUSDT-PERP.BINANCE_4h_f15_s25"``).
        A timestamp is appended automatically.
    results_dir
        Target directory. Created if it doesn't exist.
        Defaults to ``reports/html/{category}``.
    category
        Subdirectory under ``reports/html/`` (e.g., ``"backtest"``,
        ``"validate"``). Ignored when *results_dir* is provided.

    Returns
    -------
    Path
        The destination file path.

    """
    if results_dir is None:
        results_dir = _PROJECT_ROOT / "reports" / "html" / category
    results_path = Path(results_dir)
    results_path.mkdir(exist_ok=True, parents=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = results_path / f"{result_filename}_{timestamp}.html"

    nb = nbformat.read(notebook_filename, as_version=4)

    # Convert Plotly JSON outputs to HTML so nbconvert can render them
    import plotly.io as pio

    for cell in nb.cells:
        for output in cell.get("outputs", []):
            data = output.get("data", {})
            if "application/vnd.plotly.v1+json" in data and "text/html" not in data:
                fig_dict = data["application/vnd.plotly.v1+json"]
                data["text/html"] = pio.to_html(
                    fig_dict, full_html=False, include_plotlyjs="cdn",
                )

    exporter = HTMLExporter()
    exporter.embed_images = True
    body, _ = exporter.from_notebook_node(nb)

    dest.write_text(body, encoding="utf-8")
    print(f"Saved -> {dest}")
    return dest


def save_notebook_snapshot(
    notebook_filename: str,
    result_name: str,
    *,
    save_on_run_all: bool = True,
    autosave_wait_secs: float = 3.0,
    freshness_threshold_secs: float = 30.0,
    category: str = "backtest",
) -> Path | None:
    """One-call wrapper around save_notebook + save_notebook_html with smart autosave handling.

    Designed to live in the last cell of a backtest notebook so a single
    "Run All" produces a complete snapshot without races.

    Three behaviors, picked automatically based on the notebook's
    on-disk freshness and the ``save_on_run_all`` flag:

    1. **Active wait for autosave** — polls the notebook file's mtime
       and breaks as soon as the editor's autosave fires (typically
       ~1s with VS Code's ``files.autoSave: afterDelay`` default).
       Without this, the cell immediately before the save cell would
       be missing from the snapshot — autosave hasn't flushed it yet.
    2. **Fresh file** (mtime within ``freshness_threshold_secs``) →
       save unconditionally (autosave on, or user just Ctrl+S'd).
    3. **Stale file** + ``save_on_run_all=True`` → save with a warning.
    4. **Stale file** + ``save_on_run_all=False`` → skip with a help
       message ("Ctrl+S, then Shift+Enter on this cell").

    Parameters
    ----------
    notebook_filename
        Source notebook filename (e.g. ``"ma_cross.ipynb"``).  Resolved
        relative to the kernel's current working directory.
    result_name
        Basename for the snapshot files (no extension, no timestamp).
        A timestamp is appended by the underlying helpers.
    save_on_run_all
        If True (default), save even when the on-disk file is stale —
        warn that the snapshot may be incomplete.  If False, skip the
        save with a manual-trigger reminder.  Manual re-run after
        Ctrl+S always works regardless of this flag (Ctrl+S refreshes
        the file).
    autosave_wait_secs
        Maximum seconds to wait for the editor's autosave to fire.
        Default 3.0 — ample for VS Code's 1000ms debounce default.
    freshness_threshold_secs
        On-disk mtime older than this is considered "stale".  Default 30.
    category
        Subdirectory under ``reports/notebooks/`` and ``reports/html/``.
        Default ``"backtest"``.

    Returns
    -------
    Path | None
        Path to the saved .ipynb (HTML is at the parallel path).
        ``None`` when the save was skipped.

    """
    import os
    import time

    # ── 0. Resolve the source notebook filename ─────────────────────
    # Try, in order:
    #   1. The path as-given (handles absolute paths and CWD-relative).
    #   2. notebooks/backtest/<filename>  (the most common case).
    #   3. notebooks/<filename>           (workflow notebooks at root).
    # Robust to whatever CWD the kernel was launched from.
    _candidates = [
        Path(notebook_filename),
        _PROJECT_ROOT / "notebooks" / "backtest" / notebook_filename,
        _PROJECT_ROOT / "notebooks" / notebook_filename,
    ]
    _resolved = next((p for p in _candidates if p.is_file()), None)
    if _resolved is None:
        print(f"⚠️ Notebook file not found: {notebook_filename}")
        print(f"  Tried (CWD={os.getcwd()}):")
        for _c in _candidates:
            print(f"    - {_c}")
        return None
    notebook_filename = str(_resolved)

    # ── 1. Active wait for editor autosave to flush prior cells ─────
    initial_mtime = os.path.getmtime(notebook_filename)
    deadline = time.time() + autosave_wait_secs
    while time.time() < deadline:
        if os.path.getmtime(notebook_filename) > initial_mtime:
            break  # autosave fired
        time.sleep(0.1)

    # ── 2. Decide based on freshness + flag ─────────────────────────
    file_age_secs = time.time() - os.path.getmtime(notebook_filename)
    fresh = file_age_secs <= freshness_threshold_secs

    if not fresh:
        if save_on_run_all:
            print(
                f"⚠️ Notebook on disk is {file_age_secs:.0f}s old — "
                "snapshot may be stale.",
            )
            print(
                "  Enable editor autosave (see notebooks/README.md) "
                "for a complete snapshot.",
            )
            # Fall through to save.
        else:
            print(
                f"⏭ Save skipped — save_on_run_all=False and on-disk "
                f"file is {file_age_secs:.0f}s old.",
            )
            print(
                "To snapshot:  1. Ctrl+S    2. Shift+Enter on this cell",
            )
            return None

    # ── 3. Save .ipynb + .html ───────────────────────────────────────
    nb_path = save_notebook(notebook_filename, result_name, category=category)
    save_notebook_html(notebook_filename, result_name, category=category)
    return nb_path


# ─────────────────────────────────────────────────────────────────────────────
# Backtest setup helpers
# ─────────────────────────────────────────────────────────────────────────────


def load_backtest_data(
    catalog_path: str | Path,
    instrument_id: str,
    bar_type_str: str,
    *,
    venue_config: Any = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> tuple[Any, list]:
    """Load instrument + bars from a NT ParquetDataCatalog.

    Standard "load data and override fees" boilerplate used at the top of
    every backtest notebook.

    Parameters
    ----------
    catalog_path
        Path to the catalog root.
    instrument_id
        Instrument string, e.g. ``"BTC-USD-PERP.HYPERLIQUID"``.
    bar_type_str
        Bar type string, e.g. ``"BTC-USD-PERP.HYPERLIQUID-1-DAY-LAST-EXTERNAL"``.
    venue_config
        Optional ``VenueConfig`` whose ``maker_fee`` and ``taker_fee`` will
        override the loaded instrument's fees.  Useful for cross-venue
        simulation (e.g. Binance data with Hyperliquid fees).  When
        ``None``, the instrument's stored fees are used as-is.
    date_start, date_end
        Optional ISO date strings to filter bars (inclusive).  Each may be
        ``None``.

    Returns
    -------
    tuple[Instrument, list[Bar]]
        The configured instrument and the (possibly filtered) bar list.

    """
    import pandas as pd
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    from src.core import with_venue_config

    catalog = ParquetDataCatalog(str(catalog_path))
    instrument = catalog.instruments(instrument_ids=[instrument_id])[0]
    bars = catalog.bars(bar_types=[bar_type_str])

    if date_start or date_end:
        start_ns = pd.Timestamp(date_start, tz="UTC").value if date_start else None
        end_ns = pd.Timestamp(date_end, tz="UTC").value if date_end else None
        bars = [
            b for b in bars
            if (start_ns is None or b.ts_event >= start_ns)
            and (end_ns is None or b.ts_event <= end_ns)
        ]

    if venue_config is not None:
        instrument = with_venue_config(
            instrument,
            maker_fee=venue_config.maker_fee,
            taker_fee=venue_config.taker_fee,
        )

    return instrument, bars


# ── Phase 2.5 signal-stream helpers ──────────────────────────────────────────
#
# Used by ``notebooks/review_live_run.ipynb`` to align paper-trade signals
# (from PG ``signal_events``) against a re-run-of-the-same-window backtest.
# Designed to feed Phase 2.6's broader live-vs-backtest harness too — same
# DataFrame schema, same join semantics.

_SIGNAL_STREAM_COLUMNS = (
    "ts", "signal", "fast_value", "slow_value", "acted", "bootstrap",
)


async def load_signal_events(
    dsn: str,
    run_id: str | Any,
) -> pd.DataFrame:
    """Load per-bar SignalEvent rows for one paper-trade run.

    Returns one row per bar after indicator warmup, with the gate state
    the strategy observed live. Pairs with :func:`run_backtest_signal_stream`
    to produce the dense paper-vs-backtest join.

    Parameters
    ----------
    dsn
        Postgres connection string (``settings.postgres_dsn``).
    run_id
        UUID identifying the ``strategy_runs`` row. Pass a string or
        ``uuid.UUID`` — coerced to UUID for the asyncpg query.

    Returns
    -------
    pd.DataFrame
        Columns ``ts`` (UTC timestamp), ``signal`` (int), ``fast_value``
        (Decimal), ``slow_value`` (Decimal), ``acted`` (bool),
        ``bootstrap`` (bool), sorted by ``ts`` ascending. Empty (with
        the right columns) when the run produced no signals.

    """
    import uuid as _uuid

    import asyncpg
    import pandas as pd

    if not isinstance(run_id, _uuid.UUID):
        run_id = _uuid.UUID(str(run_id))

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT ts, signal, fast_value, slow_value, acted, bootstrap
            FROM signal_events
            WHERE run_id = $1
            ORDER BY ts
            """,
            run_id,
        )
    finally:
        await conn.close()

    if not rows:
        return pd.DataFrame(columns=list(_SIGNAL_STREAM_COLUMNS))

    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df[list(_SIGNAL_STREAM_COLUMNS)]


def run_backtest_signal_stream(
    *,
    instrument: Any,
    bars: list,
    venue_config: Any,
    fast_period: int,
    slow_period: int,
    ma_type: str = "EMA",
    starting_capital: int = 1000,
    trade_notional: Decimal | None = None,
    leverage: int = 20,
) -> pd.DataFrame:
    """Re-run ``MACross`` over a fixed bar window and capture its signal stream.

    Subscribes to ``signals.ma_cross`` on the engine's msgbus BEFORE
    ``engine.run()`` (the same pre-subscribe pattern as
    ``_register_account_alive_monitor`` — late wildcard subscriptions
    miss events whose concrete-topic cache is already populated).
    Returns a DataFrame with the same schema as :func:`load_signal_events`
    so the two streams can be joined directly.

    Liquidation simulator is disabled in the backtest to match what
    the live runner does on testnet (the venue handles liquidation).

    Parameters
    ----------
    instrument
        Configured NT ``Instrument`` (typically from :func:`load_backtest_data`).
    bars
        Bar list, pre-filtered to the run window.
    venue_config
        Project ``VenueConfig`` for fees + leverage.
    fast_period, slow_period
        MA periods — must match what paper trading used.
    ma_type
        MA family (``"EMA"``, ``"SMA"``, ``"HMA"``, ``"DEMA"``,
        ``"AMA"``, ``"VIDYA"``). Must match the paper run.
    starting_capital, trade_notional, leverage
        Account sizing — defaults match the project's canonical config.
        ``trade_notional`` defaults to ``Decimal("2000")``.

    Returns
    -------
    pd.DataFrame
        Same columns as :func:`load_signal_events`.

    """
    import pandas as pd
    from nautilus_trader.model.data import BarType

    from src.backtesting import make_engine
    from src.backtesting.engine import resolve_strategy_liquidation_config
    from src.core import LiquidationConfig
    from src.core.signal_event import TOPIC_SIGNAL_MA_CROSS, SignalEvent
    from src.strategies.ma_cross import MACross, MACrossConfig

    if trade_notional is None:
        trade_notional = Decimal("2000")

    # NT's make_engine rejects empty bar lists. Return the empty-schema
    # frame so callers get the expected columns either way.
    if not bars:
        return pd.DataFrame(columns=list(_SIGNAL_STREAM_COLUMNS))

    # Disable the liquidation simulator — testnet/live venues handle this
    # themselves. We're comparing signal generation here, not liquidation.
    liq_cfg = LiquidationConfig(
        enabled=False,
        halt_on_account_liquidation=False,
        min_trade_notional=Decimal("10"),
    )
    liq_res = resolve_strategy_liquidation_config(liq_cfg, venue_config, instrument)

    engine = make_engine(
        starting_capital=starting_capital,
        instrument=instrument,
        bars=bars,
        venue=instrument.venue,
        venue_config=venue_config,
        liquidation=liq_cfg,
        leverage=leverage,
    )

    # bars[0].bar_type is the authoritative bar type for the loaded bars;
    # using anything else risks subscribe-mismatch warnings.
    bar_type: BarType = bars[0].bar_type if bars else BarType.from_str(
        f"{instrument.id}-1-DAY-LAST-EXTERNAL",
    )

    strategy = MACross(MACrossConfig(
        instrument_id=instrument.id,
        bar_type=bar_type,
        fast_period=fast_period,
        slow_period=slow_period,
        ma_type=ma_type,
        trade_notional=trade_notional,
        liquidation=liq_res,
    ))
    engine.add_strategy(strategy)

    captured: list[SignalEvent] = []
    engine.kernel.msgbus.subscribe(
        topic=TOPIC_SIGNAL_MA_CROSS,
        handler=lambda e: captured.append(e) if isinstance(e, SignalEvent) else None,
    )

    engine.run()

    if not captured:
        return pd.DataFrame(columns=list(_SIGNAL_STREAM_COLUMNS))

    rows = [
        {
            "ts": pd.Timestamp(ev.ts_event, unit="ns", tz="UTC"),
            "signal": ev.signal,
            "fast_value": ev.fast_value,
            "slow_value": ev.slow_value,
            "acted": ev.acted,
            "bootstrap": ev.bootstrap,
        }
        for ev in captured
    ]
    return pd.DataFrame(rows)[list(_SIGNAL_STREAM_COLUMNS)]


def join_signal_streams(
    paper: pd.DataFrame,
    backtest: pd.DataFrame,
) -> pd.DataFrame:
    """Dense inner-join paper-trade signal stream against backtest stream.

    Joined on bar timestamp ``ts``. Bars present in one stream but not
    the other are surfaced as separate counts on the result attrs
    (``.attrs["paper_only_bars"]`` / ``.attrs["bt_only_bars"]``) — those
    are the "signal-skip" cases the roadmap calls out.

    Parameters
    ----------
    paper
        DataFrame from :func:`load_signal_events` (the live/paper stream).
    backtest
        DataFrame from :func:`run_backtest_signal_stream`.

    Returns
    -------
    pd.DataFrame
        Columns: ``ts``, ``paper_signal``, ``bt_signal``, ``paper_fast``,
        ``bt_fast``, ``paper_slow``, ``bt_slow``, ``paper_acted``,
        ``bt_acted``, ``divergent`` (bool: ``paper_signal != bt_signal``).
        Sorted by ``ts``. Attrs hold the paper-only / bt-only bar counts.

    """
    paper_indexed = paper.set_index("ts")[
        ["signal", "fast_value", "slow_value", "acted"]
    ].add_prefix("paper_")
    bt_indexed = backtest.set_index("ts")[
        ["signal", "fast_value", "slow_value", "acted"]
    ].add_prefix("bt_")

    paper_only = paper_indexed.index.difference(bt_indexed.index)
    bt_only = bt_indexed.index.difference(paper_indexed.index)

    joined = paper_indexed.join(bt_indexed, how="inner").reset_index()
    joined = joined.rename(columns={
        "paper_fast_value": "paper_fast",
        "paper_slow_value": "paper_slow",
        "bt_fast_value": "bt_fast",
        "bt_slow_value": "bt_slow",
    })
    joined["divergent"] = joined["paper_signal"] != joined["bt_signal"]
    joined = joined.sort_values("ts").reset_index(drop=True)
    joined.attrs["paper_only_bars"] = len(paper_only)
    joined.attrs["bt_only_bars"] = len(bt_only)
    return joined


def print_setup_summary(
    instrument: Any,
    bars: list,
    *,
    data_source: str,
    exec_venue: str,
    leverage: int | float,
) -> None:
    """Print the standard "data + fees" summary block.

    Companion to :func:`load_backtest_data`.  Surfaces the data
    provenance (which catalog file, which venue's fees) and a
    cross-venue warning when applicable.
    """
    import pandas as pd

    print(f"Data source : {data_source}")
    print(f"Venue       : {instrument.venue}")
    print(f"Exec venue  : {exec_venue} (simulated)")
    print(f"Instrument  : {instrument.id}")
    print(f"Currency    : {instrument.settlement_currency}")
    print(f"Leverage    : {leverage}x")
    print(f"Maker fee   : {instrument.maker_fee}  (from {exec_venue})")
    print(f"Taker fee   : {instrument.taker_fee}  (from {exec_venue})")
    print(f"Bar count   : {len(bars):,}")
    if bars:
        print(f"First bar   : {pd.Timestamp(bars[0].ts_event, unit='ns', tz='UTC')}")
        print(f"Last bar    : {pd.Timestamp(bars[-1].ts_event, unit='ns', tz='UTC')}")
    if data_source != exec_venue:
        print(f"⚠️ Cross-venue simulation: {data_source} data → {exec_venue} fees")


def print_liquidation_resolution(
    liq_resolved: Any,
    leverage: int | float,
) -> None:
    """Print the resolved liquidation config.

    Companion to ``resolve_strategy_liquidation_config``.  Surfaces the
    final values the simulator will use plus the derived "alive
    threshold" (IM + fee buffer) so the user can sanity-check that
    threshold against their starting capital.
    """
    if liq_resolved is None or not liq_resolved.enabled:
        print()
        print("Liquidation        : disabled "
              "(set LIQUIDATION.enabled=True in Cell 1)")
        return

    floor_im = float(liq_resolved.min_trade_notional) / float(leverage)
    fee_buffer = (
        float(liq_resolved.min_trade_notional)
        * float(liq_resolved.fee_rate)
        * 2
        * liq_resolved.alive_trades_buffer
    )
    threshold = floor_im + fee_buffer
    print()
    print("Liquidation        : ENABLED")
    print(f"  mm_rate           : {liq_resolved.mm_rate}")
    print(f"  fee_rate          : {liq_resolved.fee_rate}")
    print(f"  min_trade_notional: {liq_resolved.min_trade_notional}")
    print(f"  alive_buffer      : {liq_resolved.alive_trades_buffer}")
    print(f"  halt on dead acct : {liq_resolved.halt_on_account_liquidation}")
    print(
        f"  alive threshold   : equity ≥ ${threshold:.4f}  "
        f"(IM=${floor_im:.4f} + fees=${fee_buffer:.4f})",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Run-time diagnostics
# ─────────────────────────────────────────────────────────────────────────────


def print_run_diagnostics(
    engine: Any,
    venue: Any,
    instrument: Any,
    *,
    trade_notional: Any | None = None,
    leverage: int | float | None = None,
    show_lowest_n: int = 5,
) -> None:
    """Print order-status and account-balance diagnostics for a finished run.

    Surfaces the things you'd typically eyeball after a backtest:

    * Order-status counts (FILLED / CANCELED / DENIED / REJECTED).
    * A warning if any orders were denied or rejected.
    * Account-balance summary (first / min / max / final) with a
      liquidated flag.
    * Lowest-N balance rows (for spotting the deepest drawdown).
    * Margin-field interpretation block (only when ``trade_notional`` and
      ``leverage`` are provided) — shows IM/MM under the two competing
      formulas in NT's docstrings vs source so you can see which the
      account actually uses.

    All output is printed.  No return value.
    """
    orders_report = engine.trader.generate_orders_report()
    account_report = engine.trader.generate_account_report(venue)

    # ── Orders ────────────────────────────────────────────────────────
    n_orders = len(orders_report)
    print(f"Total orders: {n_orders}")
    if n_orders > 0:
        status_counts = orders_report["status"].value_counts()
        print(status_counts)
        denied = orders_report[
            orders_report["status"].isin(["DENIED", "REJECTED"])
        ]
        n_denied = len(denied)
        if n_denied > 0:
            print(f"\n⚠️ {n_denied} orders were denied or rejected:")
            try:
                from IPython.display import display
                display(denied[["ts_init", "side", "quantity", "status"]])
            except ImportError:
                print(denied[["ts_init", "side", "quantity", "status"]].to_string())
        else:
            print(f"✓ All {n_orders} orders filled or are open")

    # ── Account balances ──────────────────────────────────────────────
    if account_report.empty:
        print("\n(No account report rows — engine may have failed to start.)")
        return

    totals = account_report["total"].astype(float)
    min_bal = float(totals.min())
    max_bal = float(totals.max())
    first_bal = float(totals.iloc[0])
    final_bal = float(totals.iloc[-1])

    print(f"\nAccount report rows: {len(account_report)}")
    print(f"First balance: {first_bal:,.4f}")
    print(f"Min balance:   {min_bal:,.4f}")
    print(f"Max balance:   {max_bal:,.4f}")
    print(f"Final balance: {final_bal:,.4f}")
    if min_bal <= 0:
        print(f"\n⚠️ LIQUIDATED — min balance was {min_bal:.2f}")
        print("PnL results after liquidation are meaningless.")
    else:
        print("Would flag liquidated: False")

    # Lowest-N rows
    if show_lowest_n > 0:
        tmp = account_report.copy()
        tmp["_total_float"] = totals
        cols = [c for c in ("total", "free", "locked") if c in account_report.columns]
        print(f"\n{show_lowest_n} lowest balance rows:")
        try:
            from IPython.display import display
            display(tmp.nsmallest(show_lowest_n, "_total_float")[cols])
        except ImportError:
            print(tmp.nsmallest(show_lowest_n, "_total_float")[cols].to_string())

    # ── Margin field interpretation ──────────────────────────────────
    if trade_notional is not None and leverage is not None:
        try:
            mi = float(instrument.margin_init)
            mm = float(instrument.margin_maint)
            n = float(trade_notional)
            lev = float(leverage)

            im_pct = mi * n
            mm_pct = mm * n
            im_formula = (n / lev) * mi
            mm_formula = (n / lev) * mm

            print(f"\nmargin_init field:  {instrument.margin_init}")
            print(f"margin_maint field: {instrument.margin_maint}")
            print(
                f"  If 'pct of order value':   IM=${im_pct:.2f}  MM=${mm_pct:.2f}",
            )
            print(
                f"  If 'notional/lev × field': "
                f"IM=${im_formula:.2f}  MM=${mm_formula:.2f}",
            )
        except (AttributeError, TypeError, ValueError):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Baselines orchestrator
# ─────────────────────────────────────────────────────────────────────────────


def baselines_for_strategy(
    positions: list,
    bars: list,
    *,
    starting_capital: float,
    notional_per_trade: float,
    fee_rate: float,
    leverage: float = 1.0,
    n_simulations: int = 1000,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Compute spot B&H, leveraged B&H, and a random-entry distribution.

    Wraps the position-introspection (extract trade count, average
    duration in bars) and the three calls to
    ``src.backtesting.baselines`` in one notebook helper.

    Returns
    -------
    dict[str, Any]
        ``{"buy_and_hold": dict, "buy_and_hold_leveraged": dict,
        "random_entry": dict | None, "n_trades": int,
        "avg_duration_bars": float}``.
        ``random_entry`` is ``None`` when there are no closed trades.

    """
    from src.backtesting.baselines import buy_and_hold, random_entry_baseline

    bh_spot = buy_and_hold(
        bars,
        starting_capital=starting_capital,
        fee_rate=fee_rate,
        leverage=1.0,
    )
    bh_lev = buy_and_hold(
        bars,
        starting_capital=starting_capital,
        fee_rate=fee_rate,
        leverage=leverage,
    )

    closed = [p for p in positions if getattr(p, "is_closed", False)]
    if not closed or len(bars) < 2:
        return {
            "buy_and_hold": bh_spot,
            "buy_and_hold_leveraged": bh_lev,
            "random_entry": None,
            "n_trades": 0,
            "avg_duration_bars": float("nan"),
        }

    n_trades = len(closed)
    bar_ns = int(bars[1].ts_event - bars[0].ts_event)
    avg_dur_ns = (
        sum(int(p.ts_closed) - int(p.ts_opened) for p in closed) / n_trades
    )
    avg_dur_bars = avg_dur_ns / bar_ns

    random_dist = random_entry_baseline(
        bars,
        n_trades=n_trades,
        avg_duration_bars=avg_dur_bars,
        starting_capital=starting_capital,
        notional_per_trade=notional_per_trade,
        fee_rate=fee_rate,
        n_simulations=n_simulations,
        seed=random_seed,
    )

    return {
        "buy_and_hold": bh_spot,
        "buy_and_hold_leveraged": bh_lev,
        "random_entry": random_dist,
        "n_trades": n_trades,
        "avg_duration_bars": avg_dur_bars,
    }


def print_baselines_verdict(
    baselines: dict[str, Any],
    strategy_pnl: float,
    *,
    leverage: int | float,
    currency: str = "USDC",
) -> None:
    """Print the spot-B&H verdict + random-entry summary + leveraged caveat.

    Companion to :func:`baselines_for_strategy`.  Pure print, no return.
    """
    bh_spot = baselines["buy_and_hold"]
    bh_lev = baselines["buy_and_hold_leveraged"]
    random_dist = baselines["random_entry"]
    n_trades = baselines["n_trades"]
    avg_dur_bars = baselines["avg_duration_bars"]

    print(
        f"=== Buy & Hold (SPOT — held "
        f"{bh_spot['years_in_sample']:.1f} years) ===",
    )
    print(f"  PnL    : {bh_spot['pnl']:>10,.2f}  ({bh_spot['pnl_pct']:>+7.2f}%)")
    print(f"  MaxDD% : {bh_spot['max_drawdown_pct']:>7.2%}")
    print(f"  CAGR   : {bh_spot['cagr']:>+7.2%}")

    print("=== Strategy ===")
    print(f"  PnL    : {strategy_pnl:>10,.2f}")

    print("=== Verdict ===")
    diff_abs = strategy_pnl - bh_spot["pnl"]
    diff_pct = (
        (strategy_pnl - bh_spot["pnl"]) / abs(bh_spot["pnl"]) * 100
        if bh_spot["pnl"] else float("nan")
    )
    verdict = "BEATS" if diff_abs > 0 else "LOSES TO"
    print(
        f"  Strategy {verdict} spot buy-and-hold by "
        f"{diff_abs:>+10,.2f} ({diff_pct:>+6.1f}%)",
    )
    print(
        f"  (Leveraged B&H counterfactual at {int(leverage)}x: "
        f"PnL={bh_lev['pnl']:,.2f}, "
        f"MaxDD%={bh_lev['max_drawdown_pct']:.0%} "
        f"— IGNORES LIQUIDATION; not a realistic benchmark.)",
    )

    if random_dist is not None:
        print(
            f"=== Random entry (1000 sims, n={n_trades}, "
            f"avg_dur={avg_dur_bars:.1f} bars) ===",
        )
        print(f"  median PnL : {random_dist['median_pnl']:>10,.2f}")
        print(
            f"  5/95 pct   : {random_dist['pct_5']:>10,.2f} "
            f"/ {random_dist['pct_95']:,.2f}",
        )
    else:
        print("No closed trades — skipping random-entry baseline.")


# ─────────────────────────────────────────────────────────────────────────────
# Close-cause classification (for chart + tearsheet annotation)
# ─────────────────────────────────────────────────────────────────────────────


def classify_position_exits(positions: list, engine: Any) -> "pd.DataFrame":
    """Classify each closed position by what closed it.

    For every position in *positions* that is closed, looks up the closing
    fill's order via ``engine.cache.order(client_order_id)`` and inspects
    the order's ``tags`` to determine the close cause:

    - ``"protective_stop"`` — closed by ``ProtectiveStopAware`` mixin's
      reduce-only stop (tagged ``protective_stop``).
    - ``"liquidation"`` — closed by ``LiquidationAware`` mixin's
      cross-margin reduce-only stop (tagged ``liquidation``).
    - ``"strategy_exit"`` — closed by an untagged order (the strategy's
      cross-back signal, ``close_all_positions``, or any other exit
      submitted by the strategy itself without a tag).

    Used by ``plot_ma_cross``, ``plot_equity_curve``,
    ``plot_trade_distributions``, ``generate_v2_tearsheet``, and
    ``generate_backtest_html`` to render close-cause-aware annotations.

    Parameters
    ----------
    positions
        Output of ``engine.cache.position_snapshots() +
        engine.cache.positions()`` — every position seen during the run.
    engine
        The ``BacktestEngine`` (or any object exposing ``.cache.order(id)``).

    Returns
    -------
    pd.DataFrame
        One row per closed position, columns:

        - ``position_id`` (str): the position's ID
        - ``ts_opened`` (int, ns since epoch): opening fill timestamp
        - ``ts_closed`` (int, ns since epoch): closing fill timestamp
        - ``opening_order_id`` (str): client order ID of the first fill
          that opened the position; ``""`` if not resolvable.  Used by
          chart helpers to map a fill back to its trade-number.
        - ``closing_order_id`` (str): client order ID of the last fill
          that closed the position; ``""`` if not resolvable.  Used by
          chart helpers to flag a fill as a forced exit.
        - ``close_cause`` (str): ``"strategy_exit"`` | ``"protective_stop"``
          | ``"liquidation"``
        - ``side`` (str): ``"LONG"`` | ``"SHORT"`` | ``"FLAT"`` (the
          position's side, useful for plotting)
        - ``fill_px`` (Decimal): the closing fill price (``Decimal(0)`` if
          the closing fill can't be located — rare but possible if the
          order was pruned from cache)
        - ``trigger_px`` (Decimal | None): for tagged stops, the order's
          ``trigger_price`` (so the chart can draw the planned stop level
          alongside the actual fill); ``None`` for untagged exits
        - ``realized_pnl`` (Decimal): the position's realized PnL
        - ``entry_px`` (Decimal): the position's average open price (for
          slippage calculations downstream)

    Notes
    -----
    Positions with multiple closing fills (NETTING reductions) are not
    handled specially — the helper looks at the LAST fill's order on the
    position.  In single-position-per-strategy v1 setups this matches
    intuition; in multi-leg strategies the helper would need extending.
    """
    import pandas as pd  # noqa: PLC0415 — keep heavy import lazy

    rows = []
    for pos in positions:
        if not pos.is_closed:
            continue

        # Find the closing fill: the LAST OrderFilled on the position.
        # NT's Position keeps a list of events; the closing one is the
        # one whose qty zeros the position.  Easiest path: pull the
        # last-known closing-fill order via the position's events.
        closing_order_id = _last_closing_order_id(pos)
        opening_order_id = _first_opening_order_id(pos)
        order = (
            engine.cache.order(closing_order_id) if closing_order_id else None
        )

        tags = list(getattr(order, "tags", None) or []) if order else []
        if "protective_stop" in tags:
            cause = "protective_stop"
        elif "liquidation" in tags:
            cause = "liquidation"
        else:
            cause = "strategy_exit"

        trigger_px = getattr(order, "trigger_price", None) if order else None

        rows.append({
            "position_id": str(pos.id),
            "ts_opened": int(pos.ts_opened) if getattr(pos, "ts_opened", None) else 0,
            "ts_closed": int(pos.ts_closed) if pos.ts_closed else 0,
            "opening_order_id": str(opening_order_id) if opening_order_id else "",
            "closing_order_id": str(closing_order_id) if closing_order_id else "",
            "close_cause": cause,
            "side": str(pos.side).split(".")[-1],  # "PositionSide.LONG" → "LONG"
            "fill_px": (
                Decimal(str(order.avg_px)) if order is not None
                and getattr(order, "avg_px", None) is not None
                else Decimal("0")
            ),
            "trigger_px": (
                Decimal(str(trigger_px)) if trigger_px is not None else None
            ),
            "realized_pnl": (
                pos.realized_pnl.as_decimal() if pos.realized_pnl is not None
                else Decimal("0")
            ),
            "entry_px": (
                Decimal(str(pos.avg_px_open)) if pos.avg_px_open is not None
                else Decimal("0")
            ),
        })

    return pd.DataFrame(rows)


def _last_closing_order_id(pos: Any) -> Any:
    """Return the ClientOrderId of the order that closed *pos*, or None.

    Walks the position's event list backwards to find the last
    ``OrderFilled`` event whose post-fill quantity is zero.  Returns the
    event's ``client_order_id``.

    Defensive: if events are unavailable or no closing fill is found
    (shouldn't happen on a closed position, but guard anyway), returns
    ``None`` and the caller treats it as ``strategy_exit``.
    """
    events = getattr(pos, "events", None)
    if not events:
        # Fallback: NT positions may expose ``last_event`` without the
        # full events list in some cache configurations.
        last = getattr(pos, "last_event", None)
        return getattr(last, "client_order_id", None) if last is not None else None
    # Walk backwards — closing fill is at the end.
    for ev in reversed(events):
        if getattr(ev, "client_order_id", None) is not None:
            return ev.client_order_id
    return None


def _first_opening_order_id(pos: Any) -> Any:
    """Return the ClientOrderId of the order that opened *pos*, or None.

    Mirror of ``_last_closing_order_id`` but walks events forward.  The
    opening fill is the first ``OrderFilled`` event on the position.

    Falls back to ``pos.opening_order_id`` if NT exposes it directly,
    then to the first event's ``client_order_id``, then to ``None``.
    """
    # Some NT versions expose this directly on Position.
    direct = getattr(pos, "opening_order_id", None)
    if direct is not None:
        return direct
    events = getattr(pos, "events", None)
    if not events:
        return None
    for ev in events:
        if getattr(ev, "client_order_id", None) is not None:
            return ev.client_order_id
    return None


def find_account_liq_culprit(
    account_liq_events: list,
    positions: list,
    account_report: "pd.DataFrame",
) -> dict[str, Any]:
    """Identify the position(s) that drained the account to liquidation.

    Given the ``AccountLiquidated`` event list captured by §2.2 of every
    v2 backtest notebook (via ``msgbus.subscribe(TOPIC_ACCOUNT_LIQUIDATED,
    ...)``), figures out which open position(s) caused the equity drain.
    A "culprit" is any position that was open within 1ms of the liquidation
    timestamp — typically a single position in single-strategy v1 setups,
    but the helper handles multiple simultaneously open positions.

    Used by ``plot_ma_cross`` (vertical line + annotation), ``plot_equity_curve``
    (vertical line on the equity timeline), and ``generate_v2_tearsheet``
    (the "Account liquidation" block) to highlight the catastrophic event.

    Parameters
    ----------
    account_liq_events
        The ``account_liquidations`` list from §2.2 — populated by the
        notebook's MessageBus subscriber.  Each entry is an
        ``AccountLiquidated`` event with at minimum ``ts_event`` and
        ``equity``.
    positions
        Output of ``engine.cache.position_snapshots() +
        engine.cache.positions()``.
    account_report
        Output of ``engine.trader.generate_account_report(VENUE)``.  Used
        to compute pre-liquidation equity (``equity_before``) by reading
        the last balance datapoint strictly before the liq timestamp.

    Returns
    -------
    dict
        Empty ``{}`` if no account-liquidation event fired.  Otherwise:

        - ``liq_ts`` (int, ns): timestamp of the AccountLiquidated event
        - ``liq_ts_iso`` (str): ISO-formatted timestamp for display
        - ``equity_at_liq`` (Decimal): equity at the moment of liquidation
          (from the event)
        - ``equity_before`` (Decimal): last equity datapoint strictly
          before ``liq_ts`` (from ``account_report``).  Zero if no prior
          datapoint.
        - ``drain_amount`` (Decimal): ``equity_before - equity_at_liq``
        - ``culprit_position_ids`` (list[str]): IDs of positions that
          were open within 1ms of ``liq_ts``.  Typically one element.
        - ``culprit_positions`` (list[dict]): per-position summary —
          ``{"position_id", "side", "entry_px", "fill_px", "realized_pnl"}``.

    Notes
    -----
    Only the FIRST account-liquidation event is processed (one halt per
    run is the design).  If multiple events somehow fire, the rest are
    ignored with a print warning.
    """
    if not account_liq_events:
        return {}

    if len(account_liq_events) > 1:
        print(
            f"⚠️ Multiple AccountLiquidated events ({len(account_liq_events)}); "
            f"using the first.",
        )

    ev = account_liq_events[0]
    liq_ts = int(ev.ts_event)
    equity_at_liq = (
        ev.equity.as_decimal() if hasattr(ev.equity, "as_decimal")
        else Decimal(str(ev.equity))
    )

    # equity_before: last account_report row with ts_event strictly < liq_ts.
    equity_before = Decimal("0")
    if account_report is not None and not account_report.empty:
        # account_report's index is ts_event (DatetimeIndex) in NT v1.225.
        # Convert liq_ts (ns) to a comparable timestamp.
        import pandas as pd  # noqa: PLC0415

        liq_dt = pd.Timestamp(liq_ts, unit="ns", tz="UTC")
        prior = account_report.loc[account_report.index < liq_dt]
        if not prior.empty:
            equity_before = Decimal(str(prior["total"].iloc[-1]))

    drain_amount = equity_before - equity_at_liq

    # Culprit positions: those open within 1ms of liq_ts.
    # "Open" means: ts_opened <= liq_ts AND (ts_closed is None OR ts_closed >= liq_ts - 1ms)
    tolerance_ns = 1_000_000  # 1ms
    culprit_ids: list[str] = []
    culprit_summaries: list[dict[str, Any]] = []
    for pos in positions:
        ts_opened = int(pos.ts_opened) if pos.ts_opened else 0
        ts_closed = int(pos.ts_closed) if pos.ts_closed else None
        was_open_at_liq = (
            ts_opened <= liq_ts
            and (ts_closed is None or ts_closed >= liq_ts - tolerance_ns)
        )
        if not was_open_at_liq:
            continue
        culprit_ids.append(str(pos.id))
        culprit_summaries.append({
            "position_id": str(pos.id),
            "side": str(pos.side).split(".")[-1],
            "entry_px": (
                Decimal(str(pos.avg_px_open)) if pos.avg_px_open is not None
                else Decimal("0")
            ),
            "fill_px": (
                Decimal(str(pos.avg_px_close))
                if getattr(pos, "avg_px_close", None) is not None
                else Decimal("0")
            ),
            "realized_pnl": (
                pos.realized_pnl.as_decimal() if pos.realized_pnl is not None
                else Decimal("0")
            ),
        })

    return {
        "liq_ts": liq_ts,
        "liq_ts_iso": datetime.fromtimestamp(liq_ts / 1e9, tz=UTC).isoformat(),
        "equity_at_liq": equity_at_liq,
        "equity_before": equity_before,
        "drain_amount": drain_amount,
        "culprit_position_ids": culprit_ids,
        "culprit_positions": culprit_summaries,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sweep diagnostics
# ─────────────────────────────────────────────────────────────────────────────


def print_sweep_liquidation_diagnostics(
    results_df: pd.DataFrame,
    *,
    liq_resolved: Any,
    trade_notional: Any | None = None,
    show_top_n: int = 10,
) -> None:
    """Trustworthiness checks for the sweep's liquidation simulator output.

    Surfaces:

    1. Schema completeness — every row has populated liq columns.
    2. ``min_balance`` / ``liquidated_account`` consistency — if equity
       went sub-zero but the actor didn't fire, the actor missed a breach.
    3. Halt enforcement — for combos with ``liquidated_account=True``,
       we expect ``denied_post_halt > 0`` (strategy keeps signaling
       but RiskEngine HALTED rejects the submits).
    4. Fee model cross-check — ``total_fees / num_positions`` should be
       roughly ``2 × notional × taker_fee`` (round-trip per position).
    5. Liquidation slippage — fill price vs trigger price.  Positive %
       = worse than trigger (gap-risk loss).
    """
    if liq_resolved is None or not liq_resolved.enabled:
        print(
            "Liquidation simulation off — new columns will be 0/False/None "
            "for all rows.",
        )
        return

    cols = [
        "fast", "slow",
        "liquidated_positions", "liquidated_account", "liquidated_at_ts",
        "denied_post_halt",
        "liq_slippage_avg_pct", "liq_slippage_max_pct",
        "min_balance", "final_balance", "total_pnl",
        "total_fees",
    ]
    available = [c for c in cols if c in results_df.columns]

    n_pos_liq = int((results_df["liquidated_positions"] > 0).sum())
    n_acct_liq = int(results_df["liquidated_account"].sum())
    n_negbal = int((results_df["min_balance"] < 0).sum())
    n_denied = int((results_df["denied_post_halt"] > 0).sum())

    print("=== Liquidation summary ===")
    print(f"Total combos          : {len(results_df)}")
    print(f"With position liq     : {n_pos_liq}")
    print(f"With account liq      : {n_acct_liq}")
    print(f"With denied post-halt : {n_denied}")
    print(f"Sub-zero min_balance  : {n_negbal}")

    try:
        from IPython.display import display
    except ImportError:
        display = print  # fallback for non-notebook environments

    if n_pos_liq > 0:
        print(f"\nTop {show_top_n} by liquidated_positions:")
        display(results_df.nlargest(show_top_n, "liquidated_positions")[available])

    # Sanity check 1: min_balance ≤ 0 ⇒ liquidated_account=True
    inconsistent = results_df[
        (results_df["min_balance"] <= 0)
        & (~results_df["liquidated_account"].astype(bool))
    ]
    if len(inconsistent) > 0:
        print(
            f"\n⚠️ {len(inconsistent)} rows with min_balance ≤ 0 but "
            f"liquidated_account=False — actor missed equity breach.",
        )
        display(inconsistent[available])
    else:
        print("\n✓ min_balance / liquidated_account consistent across all rows")

    # Sanity check 2: dead combos with no post-halt denials
    halt_no_denials = results_df[
        results_df["liquidated_account"].astype(bool)
        & (results_df["denied_post_halt"] == 0)
    ]
    if len(halt_no_denials) > 0:
        print(
            f"\nℹ {len(halt_no_denials)} dead combos with no post-halt "
            f"denials (strategy didn't re-signal — usually fine).",
        )
        display(halt_no_denials[available])
    else:
        print(
            "✓ Every dead combo had at least one post-halt denial — "
            "HALTED state is enforcing.",
        )

    # Sanity check 3: fee model
    if trade_notional is not None:
        survivors = results_df[~results_df["liquidated_account"].astype(bool)]
        if not survivors.empty:
            avg_fee_per_position = (
                (survivors["total_fees"] / survivors["num_positions"])
                .replace([float("inf"), -float("inf")], float("nan"))
                .mean()
            )
            expected = 2 * float(trade_notional) * float(liq_resolved.fee_rate)
            ratio = avg_fee_per_position / expected if expected > 0 else float("nan")
            print("\n=== Fee model cross-check (survivors) ===")
            print(f"Avg fees per position : ${avg_fee_per_position:.4f}")
            print(
                f"Expected round-trip   : ${expected:.4f}  "
                f"(2 × ${float(trade_notional):.0f} × "
                f"{float(liq_resolved.fee_rate):.5f})",
            )
            tag = "✓ within 10%" if 0.90 <= ratio <= 1.10 else "⚠️ outside 10%"
            print(f"Ratio actual/expected : {ratio:.3f}  ({tag})")
            if 1.02 <= ratio <= 1.10:
                print(
                    "  (Price-drift between qty calc and fill on a trending "
                    "asset typically pushes ratio 2-8% above 1.0.)",
                )

    # Sanity check 4: liquidation-stop slippage
    liq_rows = results_df[results_df["liquidated_positions"] > 0]
    if not liq_rows.empty:
        print("\n=== Liquidation slippage (trigger vs fill, % of entry) ===")
        print(f"Combos with liquidations    : {len(liq_rows)}")
        print(
            f"Avg slippage across combos  : "
            f"{liq_rows['liq_slippage_avg_pct'].mean():.4f}%",
        )
        print(
            f"Worst single-event slippage : "
            f"{liq_rows['liq_slippage_max_pct'].max():.4f}%",
        )
        clean_fills = (liq_rows["liq_slippage_max_pct"].abs() < 0.01).sum()
        print(
            f"Combos with clean fills (|slippage|<0.01%) : "
            f"{clean_fills}/{len(liq_rows)}",
        )
        if liq_rows["liq_slippage_max_pct"].max() > 1.0:
            worst_cols = [
                c for c in (
                    "fast", "slow",
                    "liq_slippage_avg_pct", "liq_slippage_max_pct",
                    "liquidated_at_ts", "total_pnl",
                ) if c in results_df.columns
            ]
            print("\nTop 5 worst slippage events (gap risk surfaced):")
            display(liq_rows.nlargest(5, "liq_slippage_max_pct")[worst_cols])


# ─────────────────────────────────────────────────────────────────────────────
# Verdict-JSON consumers (consumed by validate_all)
# ─────────────────────────────────────────────────────────────────────────────


def load_verdict_jsons(
    verdict_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load every ``*_verdict.json`` file from ``reports/validate/``.

    Each file is the dict returned by :func:`print_validation_verdict`
    when called with ``verdict_path=``.  Files are returned sorted by
    ``timestamp`` descending (newest first).

    Parameters
    ----------
    verdict_dir
        Directory containing verdict JSONs.  Default
        ``reports/validate/`` relative to the project root.

    Returns
    -------
    list[dict]
        One dict per file.  Empty list if the directory doesn't exist
        or has no matching files.

    """
    import json

    if verdict_dir is None:
        verdict_dir = _PROJECT_ROOT / "reports" / "validate"
    verdict_dir = Path(verdict_dir)
    if not verdict_dir.exists():
        return []

    out: list[dict[str, Any]] = []
    for p in sorted(verdict_dir.glob("*_verdict.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:  # noqa: PERF203
            print(f"⚠️ Skipping {p.name}: {e}")
            continue
        # Tag with source filename so the matrix can show provenance
        data["_source"] = p.name
        out.append(data)

    out.sort(key=lambda d: d.get("timestamp", ""), reverse=True)
    return out


def build_verdict_matrix(
    verdicts: list[dict[str, Any]],
) -> "pd.DataFrame":
    """Compile a list of verdict dicts into a comparison-matrix DataFrame.

    Each row is one validate run.  Columns:

    * ``instrument`` — instrument_id (short form)
    * ``interval`` — bar_interval
    * ``pick`` — ``"auto"`` or the override params (e.g. ``"fast=10, slow=20"``)
    * one column per check name, value = the icon (✅/⚠️/🚩)
    * ``verdict`` — final icon
    * ``timestamp`` — ISO timestamp

    Sort by (instrument, pick, timestamp).  Use directly with
    pandas ``display`` for an at-a-glance comparison.
    """
    import pandas as pd

    if not verdicts:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    # First pass: collect every check name across all verdicts so the
    # column order is stable even when some runs skipped checks.
    check_names: list[str] = []
    seen = set()
    for v in verdicts:
        for c in v.get("checks", []):
            name = c.get("name", "")
            if name and name not in seen:
                seen.add(name)
                check_names.append(name)

    for v in verdicts:
        params = v.get("params", {}) or {}
        # "auto" vs "override" determination — prefer the explicit
        # ``override_params`` field on the verdict dict (v1 schema
        # forward), then fall back to the legacy filename-suffix
        # heuristic for older JSONs that predate the field.
        override = v.get("override_params")
        if override:
            pick_str = ", ".join(f"{k}={vv}" for k, vv in override.items())
        elif override is None and "override_params" in v:
            # Explicit None — auto-pick run on the new schema
            pick_str = "auto"
        else:
            # Legacy fallback: sniff the source filename for known
            # override-tag prefixes.
            src = v.get("_source", "")
            is_override = (
                "_fast" in src or "_f10" in src or "_f5" in src
                or "_bb_period" in src or "_bp" in src
                or "_dc_period" in src or "_dp" in src
                or "_length" in src
            )
            pick_str = (
                ", ".join(f"{k}={vv}" for k, vv in params.items())
                if is_override else "auto"
            )

        check_icons = {c["name"]: c["icon"] for c in v.get("checks", [])}
        verdict_icon = v.get("verdict", {}).get("icon", "")
        timestamp = v.get("timestamp", "")[:19]  # drop microseconds

        row: dict[str, Any] = {
            "instrument": v.get("instrument_id", ""),
            "interval":   v.get("bar_interval", ""),
            "pick":       pick_str,
        }
        for name in check_names:
            row[name] = check_icons.get(name, "")
        row["verdict"]   = verdict_icon
        row["timestamp"] = timestamp
        rows.append(row)

    df = pd.DataFrame(rows)
    return df.sort_values(
        ["instrument", "pick", "timestamp"], ascending=[True, True, False],
    ).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics helpers (consumed by validate_strategy)
# ─────────────────────────────────────────────────────────────────────────────


def wilson_score_interval(
    successes: int, n: int, *, confidence: float = 0.95,
) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion.

    More honest than the naive ``successes / n ± z·√(p(1-p)/n)``
    interval at small ``n`` — Wilson is closer to nominal coverage and
    the bounds stay in [0, 1].  At ``n=4`` a naive 50% point estimate
    gives a CI of [0.0, 1.0]; Wilson gives ~[0.15, 0.85] which honestly
    reflects the tiny sample.

    Used to put confidence bounds on win-rate and similar binomial
    proportions in the validate notebook so a 4-trade win-rate doesn't
    get reported with the same gravitas as a 100-trade win-rate.

    Parameters
    ----------
    successes
        Count of successes (e.g. winning trades).
    n
        Sample size (e.g. total trades).  Returns ``(nan, nan)`` if 0.
    confidence
        Two-sided confidence level (default 0.95 → z = 1.96).

    Returns
    -------
    (lower, upper)
        Lower and upper bounds, both in [0, 1].

    """
    if n <= 0:
        return float("nan"), float("nan")

    # Two-sided z from inverse normal CDF.  scipy is available
    # transitively via pandas; falling back to the 95% constant 1.96
    # avoids an explicit dependency for the common case.
    if abs(confidence - 0.95) < 1e-9:
        z = 1.96
    else:
        from scipy.stats import norm
        z = float(norm.ppf(1 - (1 - confidence) / 2))

    p = successes / n
    z2 = z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / (1 + z2 / n)
    return max(0.0, center - half), min(1.0, center + half)


# ─────────────────────────────────────────────────────────────────────────────
# Sweep loading + filtering (consumed by compare_sweeps + validate_strategy)
# ─────────────────────────────────────────────────────────────────────────────


def load_sweeps_filtered(
    sweep_dir: str | Path | None = None,
    *,
    strategy: str | None = None,
    instrument_id: str | None = None,
    bar_interval: str | None = None,
    filter_liquidated: bool = True,
    filter_spotlight: bool = True,
) -> dict[str, Any]:
    """Load v2 sweep parquets with the standard filters applied.

    Wraps :func:`src.backtesting.engine.load_sweeps` (which itself does
    the schema-version warning) and applies two filters that almost
    every consumer wants:

    1. **Liquidated rows** — drop rows where the consolidated v2
       ``liquidated`` boolean is True.  Falls back to v1's
       ``error == "liquidated"`` check when the bool column is absent
       (so old sweeps still filter correctly).
    2. **Spotlight rows** — drop rows where ``_kind == "spotlight"``
       (off-grid combos that otherwise pollute heatmaps and
       ranking tables).

    Both filters can be turned off independently.

    Parameters
    ----------
    sweep_dir
        Directory containing sweep parquet files.  ``None`` uses the
        engine's default (``data/sweeps/``).
    strategy, instrument_id, bar_interval
        Forwarded to the underlying ``load_sweeps`` filters.
    filter_liquidated
        Drop liquidated rows.  Default True.
    filter_spotlight
        Drop ``_kind == "spotlight"`` rows.  Default True.

    Returns
    -------
    dict[str, pd.DataFrame]
        Same shape as ``load_sweeps`` returns (label → DataFrame).
        Filtered counts are printed per sweep.

    """
    from src.backtesting.engine import load_sweeps

    kwargs: dict[str, Any] = {}
    if strategy is not None:
        kwargs["strategy"] = strategy
    if instrument_id is not None:
        kwargs["instrument_id"] = instrument_id
    if bar_interval is not None:
        kwargs["bar_interval"] = bar_interval
    if sweep_dir is not None:
        sweeps = load_sweeps(sweep_dir, **kwargs)
    else:
        sweeps = load_sweeps(**kwargs)

    if not sweeps:
        return sweeps

    for label, df in list(sweeps.items()):
        n_total = len(df)
        n_liq = 0
        n_spot = 0

        if filter_liquidated:
            if "liquidated" in df.columns:
                # v2 path: bool column.  NaN treated as not-liquidated.
                mask = df["liquidated"].fillna(False).astype(bool)
                n_liq = int(mask.sum())
                df = df[~mask].copy()
            elif "error" in df.columns:
                # v1 fallback.
                mask = (df["error"].fillna("") == "liquidated")
                n_liq = int(mask.sum())
                df = df[~mask].copy()

        if filter_spotlight and "_kind" in df.columns:
            mask = (df["_kind"] == "spotlight")
            n_spot = int(mask.sum())
            df = df[~mask].copy()

        sweeps[label] = df

        notes = []
        if n_liq:
            notes.append(f"{n_liq} liquidated")
        if n_spot:
            notes.append(f"{n_spot} spotlight")
        if notes:
            print(f"  {label}: filtered {' + '.join(notes)} ({n_total} → {len(df)})")

    return sweeps


# ─────────────────────────────────────────────────────────────────────────────
# Validation verdict (consumed by validate_strategy)
# ─────────────────────────────────────────────────────────────────────────────


def print_validation_verdict(
    *,
    instrument_id: str,
    bar_interval: str,
    params: dict[str, Any],
    plateau_score: float | None = None,
    walkforward_results: Any | None = None,
    bootstrap_prob_positive: float | None = None,
    bootstrap_p5: float | None = None,
    bootstrap_p95: float | None = None,
    n_trades: int | None = None,
    rolling_results: Any | None = None,
    fee_results: Any | None = None,
    regime_results: Any | None = None,
    yearly_results: Any | None = None,
    starting_capital: float | None = None,
    verdict_path: str | Path | None = None,
    override_params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Print the consolidated go / no-go assessment for a strategy.

    Aggregates up to six PnL-based checks (plateau, walk-forward,
    bootstrap, rolling-window, fee sensitivity, regime split) into a
    single ✅ / ⚠️ / 🚩 verdict.  Skips checks whose inputs are
    ``None`` (e.g. if you don't run rolling-performance, pass
    ``rolling_results=None`` and that check is omitted).

    All thresholds are PnL-based — no Sharpe / Sortino /
    returns-derived metrics are used (see
    ``docs/ANALYZER_RETURNS_CAVEAT.md``).

    Parameters
    ----------
    instrument_id, bar_interval
        For the header.
    params
        Dict of best-params (e.g. ``{"fast": 10, "slow": 40}``).
    plateau_score
        Neighbour-profitability score in [0, 1] for the chosen combo.
        Thresholds: ≥0.8 ✅, ≥0.5 ⚠️, <0.5 🚩.  ``None`` to skip.
    walkforward_results
        DataFrame from :func:`src.backtesting.engine.run_walk_forward`.
        Expected columns: ``oos_pnl``.  Pass ``None`` to skip.
    bootstrap_prob_positive
        Percentage of bootstrap resamples with positive total PnL.
        Thresholds: ≥90% ✅, ≥70% ⚠️, <70% 🚩.
    bootstrap_p5, bootstrap_p95
        For display in the bootstrap line.  Both required if either
        is provided.
    n_trades
        Number of trades the bootstrap operated on.  Used to gate
        whether the bootstrap check is meaningful (≥5 required).
    rolling_results
        DataFrame from :func:`src.backtesting.analysis.rolling_performance`.
        Expected column: ``pnl``.  Pass ``None`` to skip.
    fee_results
        DataFrame from :func:`src.backtesting.analysis.run_fee_sweep`.
        Expected columns: ``fee_bps``, ``breakeven``.  Pass ``None`` to skip.
    regime_results
        DataFrame from :func:`src.backtesting.analysis.performance_by_regime`.
        Expected columns: ``regime``, ``pnl``.  Pass ``None`` to skip.

    """
    param_str = ", ".join(f"{k}={v}" for k, v in params.items())
    print("=" * 60)
    print("  VALIDATION SUMMARY")
    print(f"  {instrument_id}  {bar_interval}")
    print(f"  {param_str}")
    print("=" * 60)

    checks: list[tuple[str, str, str]] = []

    # 1. Plateau
    if plateau_score is not None:
        if plateau_score >= 0.8:
            checks.append(("✅", "Plateau", f"Score {plateau_score:.2f} — robust region"))
        elif plateau_score >= 0.5:
            checks.append(("⚠️", "Plateau", f"Score {plateau_score:.2f} — ridge, moderate risk"))
        else:
            checks.append(("🚩", "Plateau", f"Score {plateau_score:.2f} — isolated spike, high overfit risk"))

    # 2. Walk-forward — OOS profitability
    if walkforward_results is not None:
        if hasattr(walkforward_results, "empty") and walkforward_results.empty:
            checks.append(("⚠️", "Walk-forward", "No folds completed — need more data"))
        else:
            oos_profitable = int((walkforward_results["oos_pnl"] > 0).sum())
            oos_total = len(walkforward_results)
            oos_total_pnl = float(walkforward_results["oos_pnl"].sum())
            detail = (
                f"{oos_profitable}/{oos_total} folds profitable, "
                f"total OOS PnL {oos_total_pnl:,.2f}"
            )
            if oos_profitable == oos_total and oos_total_pnl > 0:
                checks.append(("✅", "Walk-forward", detail))
            elif oos_profitable >= oos_total * 0.5 and oos_total_pnl > 0:
                checks.append(("⚠️", "Walk-forward", detail))
            else:
                checks.append(("🚩", "Walk-forward", detail))

    # 2b. Walk-forward — parameter stability across folds.
    # If the optimiser picks different params on each fold the strategy
    # is fitting noise rather than a real signal — even if the OOS PnL
    # check above looks OK.  We grade by the fraction of folds that
    # share the most-common combo: ≥75% (3/4) → ✅, ≥50% (2/4) → ⚠️,
    # else 🚩.
    if walkforward_results is not None and not (
        hasattr(walkforward_results, "empty") and walkforward_results.empty
    ):
        param_cols = [
            c for c in walkforward_results.columns if c.startswith("best_")
        ]
        if param_cols and len(walkforward_results) >= 2:
            # Build a tuple per fold for the chosen params; count how
            # many folds share the most-common combo.
            combos = [
                tuple(row[c] for c in param_cols)
                for _, row in walkforward_results[param_cols].iterrows()
            ]
            from collections import Counter
            most_common_combo, most_common_count = Counter(combos).most_common(1)[0]
            n_folds = len(combos)
            stable_pct = most_common_count / n_folds
            combo_str = ", ".join(
                f"{c.removeprefix('best_')}={v}"
                for c, v in zip(param_cols, most_common_combo, strict=True)
            )
            if most_common_count == n_folds:
                checks.append((
                    "✅", "Param stability",
                    f"All {n_folds} folds picked {combo_str}",
                ))
            elif stable_pct >= 0.75:
                checks.append((
                    "⚠️", "Param stability",
                    f"{most_common_count}/{n_folds} folds picked {combo_str}",
                ))
            elif stable_pct >= 0.50:
                checks.append((
                    "⚠️", "Param stability",
                    f"Most-common combo only {most_common_count}/{n_folds} folds — drifting",
                ))
            else:
                checks.append((
                    "🚩", "Param stability",
                    f"Different combo nearly every fold ({n_folds} folds, "
                    f"top combo only {most_common_count}) — fitting noise",
                ))

    # 3. Bootstrap — high P(profit) by itself isn't enough.  Pros size
    # to survive their drawdown CI, so a strategy whose 5th-percentile
    # PnL is well below 10% of starting capital has a worst-case tail
    # at-or-below "essentially zero return," even at 95% P(profit).
    # The capital-relative tail check is only applied when
    # ``starting_capital`` is provided; absent it, we fall back to the
    # legacy P(profit)-only thresholds.
    if bootstrap_prob_positive is not None:
        if n_trades is not None and n_trades < 5:
            checks.append(("⚠️", "Bootstrap", f"Only {n_trades} trades — insufficient"))
        else:
            ci_str = ""
            if bootstrap_p5 is not None and bootstrap_p95 is not None:
                ci_str = f", 90% CI [{bootstrap_p5:,.0f}, {bootstrap_p95:,.0f}]"
            detail = f"P(profit)={bootstrap_prob_positive:.0f}%{ci_str}"

            # Capital-relative weak-tail check
            weak_tail = False
            if starting_capital is not None and bootstrap_p5 is not None:
                tail_threshold = starting_capital * 0.10
                if bootstrap_p5 < tail_threshold:
                    weak_tail = True
                    detail += (
                        f"  ⚠ pct_5 < {tail_threshold:,.0f} "
                        f"(10% of capital)"
                    )

            if bootstrap_prob_positive >= 90 and not weak_tail:
                checks.append(("✅", "Bootstrap", detail))
            elif bootstrap_prob_positive >= 70:
                # 70-89% prob OR ≥90% with weak tail → ⚠️
                checks.append(("⚠️", "Bootstrap", detail))
            else:
                checks.append(("🚩", "Bootstrap", detail))

    # 4. Rolling windows — use ACTIVE windows (where the strategy
    # actually traded) as the denominator.  Sparse strategies have
    # many no-trade windows that shouldn't count against them, and
    # this also keeps the verdict consistent with what the rolling
    # cell prints to the notebook.
    if rolling_results is not None:
        if hasattr(rolling_results, "empty") and rolling_results.empty:
            checks.append(("⚠️", "Rolling", "No rolling-window results"))
        else:
            active = rolling_results[rolling_results["pnl"] != 0.0]
            n_active = len(active)
            if n_active == 0:
                checks.append((
                    "⚠️", "Rolling",
                    "No active windows — strategy traded zero closed positions",
                ))
            else:
                pos_active = int((active["pnl"] > 0).sum())
                pct = pos_active / n_active * 100
                n_total = len(rolling_results)
                detail = (
                    f"{pos_active}/{n_active} active windows profitable "
                    f"({pct:.0f}%)"
                )
                if n_active < n_total:
                    detail += f"  [{n_total - n_active} no-trade windows excluded]"
                if pct >= 60:
                    checks.append(("✅", "Rolling", detail))
                elif pct >= 40:
                    checks.append(("⚠️", "Rolling", detail))
                else:
                    checks.append(("🚩", "Rolling", f"{detail} — concentrated"))

    # 5. Fee sensitivity
    if fee_results is not None:
        if hasattr(fee_results, "empty") and fee_results.empty:
            checks.append(("⚠️", "Fee sensitivity", "No fee-sweep results"))
        else:
            breakeven_rows = fee_results[fee_results["breakeven"]]
            if breakeven_rows.empty:
                checks.append(("🚩", "Fee sensitivity", "Not profitable at any fee level"))
            else:
                max_fee = float(breakeven_rows["fee_bps"].max())
                if max_fee >= 7.5:
                    checks.append(("✅", "Fee sensitivity", f"Profitable up to {max_fee:.1f} bps — strong margin"))
                elif max_fee >= 4:
                    checks.append(("⚠️", "Fee sensitivity", f"Profitable up to {max_fee:.1f} bps — moderate margin"))
                else:
                    checks.append(("🚩", "Fee sensitivity", f"Breakeven at {max_fee:.1f} bps — thin margin"))

    # 6. Regime
    if regime_results is not None:
        if hasattr(regime_results, "empty") and regime_results.empty:
            checks.append(("⚠️", "Regime", "No regime breakdown available"))
        else:
            ranging = regime_results[regime_results["regime"] == "RANGING"]
            trending = regime_results[regime_results["regime"] == "TRENDING"]
            if not ranging.empty and not trending.empty:
                ranging_pnl = float(ranging["pnl"].iloc[0])
                trending_pnl = float(trending["pnl"].iloc[0])
                if trending_pnl > 0 and trending_pnl > abs(ranging_pnl):
                    checks.append(("✅", "Regime", f"Trending +{trending_pnl:,.0f} > Ranging {ranging_pnl:,.0f}"))
                elif trending_pnl > 0:
                    checks.append(("⚠️", "Regime", f"Trending +{trending_pnl:,.0f}, Ranging {ranging_pnl:,.0f} — net depends on mix"))
                else:
                    checks.append(("🚩", "Regime", f"Trending {trending_pnl:,.0f}, Ranging {ranging_pnl:,.0f} — no clear edge"))
            else:
                checks.append(("⚠️", "Regime", "Trending or ranging regime missing from results"))

    # 7. Yearly concentration — if a single calendar year accounts for
    # the majority of total PnL, the strategy is a regime trade dressed
    # up.  This is the most diagnostic signal in any 5+ year backtest
    # and is invisible to the other checks (plateau, walk-forward,
    # bootstrap, regime — none of them look at the year-over-year
    # distribution of PnL).  Thresholds: ≥75% in one year → 🚩,
    # ≥50% → ⚠️, else ✅.
    if yearly_results is not None:
        if hasattr(yearly_results, "empty") and yearly_results.empty:
            checks.append(("⚠️", "Yearly concentration", "No per-year data"))
        elif "pnl" not in yearly_results.columns:
            checks.append(("⚠️", "Yearly concentration", "Missing 'pnl' column"))
        else:
            total_pnl_yearly = float(yearly_results["pnl"].sum())
            n_years = len(yearly_results)
            if abs(total_pnl_yearly) < 1e-6 or n_years < 2:
                checks.append((
                    "⚠️", "Yearly concentration",
                    f"Only {n_years} year(s) of data — insufficient",
                ))
            else:
                # Use absolute share for the dominant year — for a
                # mostly-profitable strategy this is the share of total
                # gains coming from one year; for a net-loser the
                # concentration question still applies symmetrically.
                # ``performance_by_year`` indexes by year (int), so
                # idxmax returns the year directly.
                year_shares = (yearly_results["pnl"] / total_pnl_yearly).abs()
                top_idx = year_shares.idxmax()
                top_share = float(year_shares.loc[top_idx])
                top_year = int(top_idx)
                detail = (
                    f"{top_year}: {top_share * 100:.0f}% of total PnL "
                    f"(over {n_years} years)"
                )
                if top_share >= 0.75:
                    checks.append((
                        "🚩", "Yearly concentration",
                        f"{detail} — one-trick pony risk",
                    ))
                elif top_share >= 0.50:
                    checks.append((
                        "⚠️", "Yearly concentration",
                        f"{detail} — heavy single-year skew",
                    ))
                else:
                    checks.append((
                        "✅", "Yearly concentration",
                        f"top year {top_year}: {top_share * 100:.0f}% "
                        f"(over {n_years} years)",
                    ))

    print()
    for icon, name, detail in checks:
        print(f"  {icon} {name:15s} {detail}")

    n_fail = sum(1 for icon, _, _ in checks if icon == "🚩")
    n_warn = sum(1 for icon, _, _ in checks if icon == "⚠️")
    n_pass = sum(1 for icon, _, _ in checks if icon == "✅")

    print()
    if n_fail > 0:
        verdict_icon = "🚩"
        verdict_outcome = "fail"
        verdict_summary = "DO NOT paper trade yet.  Address the red flags first."
    elif n_warn > 0:
        verdict_icon = "⚠️"
        verdict_outcome = "warn"
        verdict_summary = "PROCEED WITH CAUTION.  Monitor closely in paper trading."
    else:
        verdict_icon = "✅"
        verdict_outcome = "pass"
        verdict_summary = "READY for paper trading."

    print(f"  VERDICT: {verdict_icon} {verdict_summary}")
    print()
    print("  Remember: expect 30–40% haircut from backtest to live.")
    print("=" * 60)

    # Build the verdict dict — persisted to disk if verdict_path is
    # set, and returned to the caller so validate_all.ipynb can build
    # a comparison matrix without parsing stdout.  Schema versioned
    # so consumers can reject older formats cleanly.
    verdict_dict: dict[str, Any] = {
        "_schema_version": 1,
        "instrument_id": instrument_id,
        "bar_interval": bar_interval,
        "params": dict(params),
        # When ``override_params`` is set, the run validated a
        # specific user-supplied combo (typically the cross-sweep
        # robust pick) instead of the per-sweep best.  Persisting
        # it explicitly lets validate_all distinguish "auto" from
        # "override" rows without filename-suffix sniffing.
        "override_params": dict(override_params) if override_params else None,
        "starting_capital": starting_capital,
        "checks": [
            {
                "icon": icon,
                "name": name,
                "detail": detail,
                "outcome": (
                    "fail" if icon == "🚩"
                    else "warn" if icon == "⚠️"
                    else "pass"
                ),
            }
            for icon, name, detail in checks
        ],
        "counts": {"pass": n_pass, "warn": n_warn, "fail": n_fail},
        "verdict": {
            "icon": verdict_icon,
            "outcome": verdict_outcome,
            "summary": verdict_summary,
        },
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }

    if verdict_path is not None:
        import json
        path = Path(verdict_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(verdict_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Verdict JSON: {path}")

    return verdict_dict
