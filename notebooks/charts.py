"""Notebook chart helpers.

Plotting utilities for strategy visualisation in Jupyter notebooks.
Returns Plotly Figure objects — callers decide whether to render inline,
write to HTML, or embed in a tearsheet.

Usage
-----
    from charts import plot_ema_cross

    fig = plot_ema_cross(bars, fills_report, fast_period=10, slow_period=20)
    fig.show(config=dict(displaylogo=False))

Phase 2 note: if server-side chart generation is needed (tearsheets, API
endpoints), extract to src/visualisation/charts.py at that point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar

if TYPE_CHECKING:
    pass

# ── Plotly dark theme constants — matches TradingView defaults ─────────────────
_BG          = "#131722"
_GRID        = "#1e222d"
_BORDER      = "#2a2e39"
_TEXT        = "#d1d4dc"
_GREEN       = "#26a69a"
_RED         = "#ef5350"
_AMBER       = "#f5c518"
_BLUE        = "#2196f3"


# ── Public API ────────────────────────────────────────────────────────────────

def plot_ema_cross(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    *,
    instrument_label: str = "BTC-USD-PERP",
    bar_label: str = "1h",
    height: int = 600,
) -> go.Figure:
    """Candlestick chart with EMA overlays and trade entry markers.

    Parameters
    ----------
    bars:
        Ordered list of NT Bar objects (output of ``catalog.bars()``).
    fills_report:
        DataFrame from ``engine.trader.generate_order_fills_report()``.
        Handled defensively — missing or empty DataFrames produce a chart
        without trade markers rather than raising.
    fast_period:
        Fast EMA period (plotted in amber).
    slow_period:
        Slow EMA period (plotted in blue).
    instrument_label:
        Display string for the chart title.
    bar_label:
        Bar interval label used in the title (e.g. ``"1h"``, ``"4h"``).
    height:
        Figure height in pixels.

    Returns
    -------
    go.Figure
        Call ``.show()`` or ``.write_html()`` on the returned figure.
    """
    ohlcv = _bars_to_ohlcv(bars, fast_period, slow_period)
    buys, sells = _parse_fills(fills_report)

    fig = go.Figure()
    _add_candlesticks(fig, ohlcv)
    _add_ema_lines(fig, ohlcv, fast_period, slow_period)
    _add_trade_markers(fig, buys, sells, ohlcv)
    _apply_layout(fig, fast_period, slow_period, instrument_label, bar_label, height)

    return fig


# ── Private helpers ───────────────────────────────────────────────────────────

def _bars_to_ohlcv(
    bars: list[Bar],
    fast_period: int,
    slow_period: int,
) -> pd.DataFrame:
    """Convert NT Bar list to OHLCV DataFrame with EMA columns appended."""
    fast_ema = ExponentialMovingAverage(fast_period)
    slow_ema = ExponentialMovingAverage(slow_period)

    rows = []
    for bar in bars:
        fast_ema.handle_bar(bar)
        slow_ema.handle_bar(bar)
        rows.append({
            "ts":    pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
            "open":  float(bar.open),
            "high":  float(bar.high),
            "low":   float(bar.low),
            "close": float(bar.close),
            "vol":   float(bar.volume),
            f"EMA{fast_period}": fast_ema.value if fast_ema.initialized else np.nan,
            f"EMA{slow_period}": slow_ema.value if slow_ema.initialized else np.nan,
        })

    return pd.DataFrame(rows).set_index("ts")


def _parse_fills(fills_report: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Extract buy and sell rows from the fills report.

    Returns two DataFrames (buys, sells), each with normalised columns:
    ``_ts`` (datetime64, UTC), ``_px`` (float), ``_qty`` (str).
    Both may be empty if the report is empty or columns are missing.
    """
    empty = pd.DataFrame()

    if fills_report is None or fills_report.empty:
        return empty, empty

    fr = fills_report.copy()

    price_col = _first_col(fr, ["last_px", "avg_px", "price"])
    side_col  = _first_col(fr, ["side", "order_side"])
    ts_col    = _first_col(fr, ["ts_last", "ts_event", "ts_filled"])
    qty_col   = _first_col(fr, ["last_qty", "filled_qty", "quantity"])

    if not all([price_col, side_col, ts_col]):
        return empty, empty

    fr["_px"]  = fr[price_col].astype(float)
    fr["_ts"]  = pd.to_datetime(fr[ts_col].astype("int64"), unit="ns", utc=True)
    fr["_qty"] = fr[qty_col].astype(str) if qty_col else "—"
    fr["_side_str"] = fr[side_col].astype(str)

    buys  = fr[fr["_side_str"].str.contains("BUY",  case=False)].copy()
    sells = fr[fr["_side_str"].str.contains("SELL", case=False)].copy()

    return buys, sells


def _first_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first candidate column name present in *df*, or None."""
    return next((c for c in candidates if c in df.columns), None)


def _add_candlesticks(fig: go.Figure, ohlcv: pd.DataFrame) -> None:
    fig.add_trace(go.Candlestick(
        x=ohlcv.index,
        open=ohlcv["open"],
        high=ohlcv["high"],
        low=ohlcv["low"],
        close=ohlcv["close"],
        name="Price",
        increasing_line_color=_GREEN,
        decreasing_line_color=_RED,
        increasing_fillcolor=_GREEN,
        decreasing_fillcolor=_RED,
        line_width=1,
        whiskerwidth=0,
    ))


def _add_ema_lines(
    fig: go.Figure,
    ohlcv: pd.DataFrame,
    fast_period: int,
    slow_period: int,
) -> None:
    for col, color in [
        (f"EMA{fast_period}", _AMBER),
        (f"EMA{slow_period}", _BLUE),
    ]:
        fig.add_trace(go.Scatter(
            x=ohlcv.index,
            y=ohlcv[col],
            name=col,
            mode="lines",
            line=dict(color=color, width=1.5),
        ))


def _add_trade_markers(
    fig: go.Figure,
    buys: pd.DataFrame,
    sells: pd.DataFrame,
    ohlcv: pd.DataFrame,
) -> None:
    """Overlay buy/sell markers nudged just outside the candle bodies."""
    # Nudge factor — proportional to the median candle range so it scales
    # sensibly across different instruments and price levels.
    median_range = (ohlcv["high"] - ohlcv["low"]).median()
    nudge = median_range * 0.3

    _add_marker_trace(
        fig, buys,
        name="Buy",
        symbol="triangle-up",
        color=_GREEN,
        y_offset=-nudge,     # below the candle
        label="BUY",
    )
    _add_marker_trace(
        fig, sells,
        name="Sell",
        symbol="triangle-down",
        color=_RED,
        y_offset=+nudge,     # above the candle
        label="SELL",
    )


def _add_marker_trace(
    fig: go.Figure,
    df: pd.DataFrame,
    *,
    name: str,
    symbol: str,
    color: str,
    y_offset: float,
    label: str,
) -> None:
    if df.empty:
        return

    has_qty = "_qty" in df.columns
    customdata = (
        np.stack([df["_px"].round(2), df["_qty"]], axis=-1)
        if has_qty
        else df[["_px"]].values
    )
    hover = (
        f"<b>{label}</b><br>"
        "Price : $%{customdata[0]:,.2f}<br>"
        + ("Qty   : %{customdata[1]}<br>" if has_qty else "")
        + "Time  : %{x|%Y-%m-%d %H:%M}<extra></extra>"
    )

    fig.add_trace(go.Scatter(
        x=df["_ts"],
        y=df["_px"] + y_offset,
        name=name,
        mode="markers",
        marker=dict(
            symbol=symbol,
            size=10,
            color=color,
            line=dict(color="#ffffff", width=1),
        ),
        customdata=customdata,
        hovertemplate=hover,
    ))


def _apply_layout(
    fig: go.Figure,
    fast_period: int,
    slow_period: int,
    instrument_label: str,
    bar_label: str,
    height: int,
) -> None:
    fig.update_layout(
        title=dict(
            text=f"{instrument_label} · {bar_label} · EMACross({fast_period}/{slow_period})",
            font=dict(size=15),
        ),
        height=height,
        template="plotly_dark",
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="Inter, system-ui, sans-serif"),

        xaxis=dict(
            rangeslider=dict(visible=True, thickness=0.04),
            type="date",
            gridcolor=_GRID,
            linecolor=_BORDER,
            tickformat="%b %d\n%Y",
        ),
        yaxis=dict(
            side="right",
            gridcolor=_GRID,
            linecolor=_BORDER,
            tickprefix="$",
            tickformat=",.0f",
        ),

        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            bgcolor="rgba(0,0,0,0)",
        ),

        hovermode="x unified",
        margin=dict(l=0, r=60, t=60, b=0),
    )
