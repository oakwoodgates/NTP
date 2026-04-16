"""Shared notebook utilities."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import nbformat
from nbconvert import HTMLExporter

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
