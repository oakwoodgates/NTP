"""Shared notebook utilities."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import nbformat
from nbconvert import HTMLExporter

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

def save_notebook(
    notebook_filename: str,
    result_filename: str,
    results_dir: str | Path | None = None,
) -> Path:
    """Copy a notebook (with outputs) to the results directory.

    Save the notebook (Ctrl+S) before calling this so outputs are on disk.

    Parameters
    ----------
    notebook_filename
        Source notebook filename (e.g., ``"backtest_sma_cross.ipynb"``).
    result_filename
        Descriptive name without extension or timestamp
        (e.g., ``"SMACross_BTCUSDT-PERP.BINANCE_4h_f15_s25"``).
        A timestamp is appended automatically.
    results_dir
        Target directory. Created if it doesn't exist. Default ``"results"``.

    Returns
    -------
    Path
        The destination file path.

    """
    if results_dir is None:
        results_dir = _PROJECT_ROOT / "reports" / "notebooks"
    results_path = Path(results_dir)
    results_path.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = results_path / f"{result_filename}_{timestamp}.ipynb"

    shutil.copy2(notebook_filename, dest)
    print(f"Saved -> {dest}")
    return dest


def save_notebook_html(
    notebook_filename: str,
    result_filename: str,
    results_dir: str | Path | None = None,
) -> Path:
    """Export a notebook to a self-contained HTML file in the results directory.

    Save the notebook (Ctrl+S) before calling this so outputs are on disk.

    Parameters
    ----------
    notebook_filename
        Source notebook filename (e.g., ``"backtest_sma_cross.ipynb"``).
    result_filename
        Descriptive name without extension or timestamp
        (e.g., ``"SMACross_BTCUSDT-PERP.BINANCE_4h_f15_s25"``).
        A timestamp is appended automatically.
    results_dir
        Target directory. Created if it doesn't exist. Default ``"results"``.

    Returns
    -------
    Path
        The destination file path.

    """
    if results_dir is None:
        results_dir = _PROJECT_ROOT / "reports" / "html"
    results_path = Path(results_dir)
    results_path.mkdir(exist_ok=True)

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
