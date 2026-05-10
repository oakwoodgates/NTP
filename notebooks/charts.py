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

import html
import json
import math
import textwrap
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports" / "charts"
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nautilus_trader.model.currencies import USDC
from nautilus_trader.indicators import (
    AdaptiveMovingAverage,
    BollingerBands,
    DonchianChannel,
    DoubleExponentialMovingAverage,
    ExponentialMovingAverage,
    HullMovingAverage,
    MovingAverageConvergenceDivergence,
    RelativeStrengthIndex,
    SimpleMovingAverage,
    VariableIndexDynamicAverage,
)
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

# ── Close-cause overlay constants ─────────────────────────────────────────
# Distinct colours / shapes for close-cause markers (protective_stop and
# liquidation) so they pop out against the regular green BUY / red SELL
# triangles produced by ``_add_trade_markers``.  Used by both the Plotly
# chart helpers and the TVLC HTML report.
_PSTOP_COLOR  = "#ff8a65"   # warm orange — distinct from sell red
_LIQ_COLOR    = "#ff1744"   # strong crimson — distinct from sell red
_LIQ_BAND     = "rgba(255, 23, 68, 0.20)"
_PSTOP_LABEL  = "STOP"
_LIQ_LABEL    = "LIQ"

# ── Flag constants ────────────────────────────────────────────────────────
_FLAG_BG   = "#eeeeee"
_FLAG_TEXT = "#777777"

# ── MA class dispatch ────────────────────────────────────────────────────────

_MA_CLASSES: dict[str, type] = {
    "EMA": ExponentialMovingAverage, "SMA": SimpleMovingAverage, "HMA": HullMovingAverage,
    "DEMA": DoubleExponentialMovingAverage, "VIDYA": VariableIndexDynamicAverage,
}


def _make_nt_ma(ma_type: str, period: int) -> Any:
    """Construct an NT moving-average indicator, with AMA special-casing."""
    if ma_type == "AMA":
        return AdaptiveMovingAverage(period, 2, 30)
    return _MA_CLASSES[ma_type](period)


# ── Public API ────────────────────────────────────────────────────────────────

def plot_ma_cross(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    *,
    ma_type: str = "EMA",
    instrument_label: str = "",
    bar_label: str = "1h",
    height: int = 600,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> go.Figure:
    """Candlestick chart with MA overlays and trade entry markers.

    Parameters
    ----------
    exit_classification
        Optional DataFrame from ``utils.classify_position_exits(...)``.
        When provided, protective-stop and liquidation closes get
        distinct overlay markers on top of the regular BUY/SELL
        triangles, so stop-driven exits stand out from strategy exits.
    account_liq_event
        Optional dict from ``utils.find_account_liq_culprit(...)``.
        When non-empty, a vertical red band is drawn at the
        ``liq_ts`` timestamp.
    """
    ohlcv = _bars_to_ma_ohlcv(bars, fast_period, slow_period, ma_type=ma_type)
    buys, sells = _parse_fills(fills_report)

    fig = go.Figure()
    _add_candlesticks(fig, ohlcv)
    _add_ma_lines(fig, ohlcv, fast_period, slow_period, ma_type=ma_type)
    _add_trade_markers(fig, buys, sells, ohlcv)
    _add_close_cause_markers(fig, exit_classification, ohlcv)
    _add_account_liq_marker(fig, account_liq_event)
    title = f"{instrument_label} · {bar_label} · {ma_type}Cross({fast_period}/{slow_period})"
    _apply_base_layout(fig, title, height)
    return fig


# Aliases — kept for notebooks that import the old names.
def plot_ema_cross(
    bars: list[Bar], fills_report: pd.DataFrame, *,
    fast_period: int = 20, slow_period: int = 50,
    instrument_label: str = "", bar_label: str = "1h", height: int = 600,
) -> go.Figure:
    """Alias for ``plot_ma_cross(..., ma_type="EMA")``."""
    return plot_ma_cross(bars, fills_report, fast_period, slow_period,
                         ma_type="EMA", instrument_label=instrument_label,
                         bar_label=bar_label, height=height)


def plot_sma_cross(
    bars: list[Bar], fills_report: pd.DataFrame,
    fast_period: int, slow_period: int, *,
    instrument_label: str = "", bar_label: str = "1h", height: int = 600,
) -> go.Figure:
    """Alias for ``plot_ma_cross(..., ma_type="SMA")``."""
    return plot_ma_cross(bars, fills_report, fast_period, slow_period,
                         ma_type="SMA", instrument_label=instrument_label,
                         bar_label=bar_label, height=height)


def plot_hma_cross(
    bars: list[Bar], fills_report: pd.DataFrame,
    fast_period: int, slow_period: int, *,
    instrument_label: str = "", bar_label: str = "1h", height: int = 600,
) -> go.Figure:
    """Alias for ``plot_ma_cross(..., ma_type="HMA")``."""
    return plot_ma_cross(bars, fills_report, fast_period, slow_period,
                         ma_type="HMA", instrument_label=instrument_label,
                         bar_label=bar_label, height=height)


# ── Private helpers ───────────────────────────────────────────────────────────

def _bars_to_ma_ohlcv(
    bars: list[Bar],
    fast_period: int,
    slow_period: int,
    ma_type: str = "EMA",
) -> pd.DataFrame:
    """Convert NT Bar list to OHLCV DataFrame with MA columns appended."""
    fast_ma = _make_nt_ma(ma_type, fast_period)
    slow_ma = _make_nt_ma(ma_type, slow_period)

    rows = []
    for bar in bars:
        fast_ma.handle_bar(bar)
        slow_ma.handle_bar(bar)
        rows.append({
            "ts":    pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
            "open":  float(bar.open),
            "high":  float(bar.high),
            "low":   float(bar.low),
            "close": float(bar.close),
            "vol":   float(bar.volume),
            f"{ma_type}{fast_period}": fast_ma.value if fast_ma.initialized else np.nan,
            f"{ma_type}{slow_period}": slow_ma.value if slow_ma.initialized else np.nan,
        })

    return pd.DataFrame(rows).set_index("ts")


def _parse_fills(fills_report: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Extract buy and sell rows from the fills report.

    Returns two DataFrames (buys, sells), each with normalized columns:
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


def _add_candlesticks(
    fig: go.Figure,
    ohlcv: pd.DataFrame,
    row: int | None = None,
) -> None:
    trace = go.Candlestick(
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
    )
    if row is not None:
        fig.add_trace(trace, row=row, col=1)
    else:
        fig.add_trace(trace)


def _add_trade_markers(
    fig: go.Figure,
    buys: pd.DataFrame,
    sells: pd.DataFrame,
    ohlcv: pd.DataFrame,
    row: int | None = None,
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
        row=row,
    )
    _add_marker_trace(
        fig, sells,
        name="Sell",
        symbol="triangle-down",
        color=_RED,
        y_offset=+nudge,     # above the candle
        label="SELL",
        row=row,
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
    row: int | None = None,
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

    trace = go.Scatter(
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
    )
    if row is not None:
        fig.add_trace(trace, row=row, col=1)
    else:
        fig.add_trace(trace)


def _add_close_cause_markers(
    fig: go.Figure,
    exit_classification: pd.DataFrame | None,
    ohlcv: pd.DataFrame,
    *,
    row: int | None = None,
) -> None:
    """Overlay distinct markers for protective-stop and liquidation closes.

    ``exit_classification`` is the DataFrame returned by
    ``notebooks.utils.classify_position_exits``.  Rows with
    ``close_cause == "strategy_exit"`` are skipped — those are already
    represented by the regular BUY/SELL markers from
    ``_add_trade_markers``.  This overlay sits on top of those, in
    distinct colours/shapes, so stop-driven exits visually pop out.
    """
    if exit_classification is None or exit_classification.empty:
        return
    if "close_cause" not in exit_classification.columns:
        return

    # Nudge above the candle by a fraction of the median range so the
    # stop / liq glyphs sit just clear of the body — matches the style
    # of the regular SELL markers but offset enough to read separately.
    median_range = (ohlcv["high"] - ohlcv["low"]).median()
    nudge = median_range * 0.7

    cfg = [
        ("protective_stop", _PSTOP_LABEL, _PSTOP_COLOR, "diamond"),
        ("liquidation",     _LIQ_LABEL,   _LIQ_COLOR,   "x"),
    ]
    for cause, label, color, symbol in cfg:
        subset = exit_classification[exit_classification["close_cause"] == cause]
        if subset.empty:
            continue

        ts = pd.to_datetime(subset["ts_closed"].astype("int64"), unit="ns", utc=True)
        px = subset["fill_px"].astype(float)
        pnl = subset["realized_pnl"].astype(float) if "realized_pnl" in subset.columns else None

        if pnl is not None:
            customdata = np.stack([px.round(2), pnl.round(2)], axis=-1)
            hover = (
                f"<b>{label}</b><br>"
                "Fill px : $%{customdata[0]:,.2f}<br>"
                "PnL     : %{customdata[1]:,.2f}<br>"
                "Time    : %{x|%Y-%m-%d %H:%M}<extra></extra>"
            )
        else:
            customdata = px.round(2).to_numpy().reshape(-1, 1)
            hover = (
                f"<b>{label}</b><br>"
                "Fill px : $%{customdata[0]:,.2f}<br>"
                "Time    : %{x|%Y-%m-%d %H:%M}<extra></extra>"
            )

        trace = go.Scatter(
            x=ts,
            y=px + nudge,
            name=label,
            mode="markers",
            marker=dict(
                symbol=symbol,
                size=13,
                color=color,
                line=dict(color="#ffffff", width=1.2),
            ),
            customdata=customdata,
            hovertemplate=hover,
            legendgroup="close_cause",
        )
        if row is not None:
            fig.add_trace(trace, row=row, col=1)
        else:
            fig.add_trace(trace)


def _add_account_liq_marker(
    fig: go.Figure,
    account_liq_event: dict[str, Any] | None,
) -> None:
    """Draw a vertical red band at the account-liquidation timestamp."""
    if not account_liq_event:
        return
    liq_ts = account_liq_event.get("liq_ts")
    if liq_ts is None:
        return
    x = pd.Timestamp(int(liq_ts), unit="ns", tz="UTC")
    fig.add_vline(
        x=x,
        line=dict(color=_LIQ_COLOR, width=2, dash="dash"),
        annotation_text="ACCOUNT LIQ",
        annotation_position="top",
        annotation_font=dict(color=_LIQ_COLOR, size=11),
    )


def _add_ma_lines(
    fig: go.Figure,
    ohlcv: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    ma_type: str = "EMA",
) -> None:
    for col, color in [
        (f"{ma_type}{fast_period}", _AMBER),
        (f"{ma_type}{slow_period}", _BLUE),
    ]:
        fig.add_trace(go.Scatter(
            x=ohlcv.index,
            y=ohlcv[col],
            name=col,
            mode="lines",
            line=dict(color=color, width=1.5),
        ))


def _apply_base_layout(
    fig: go.Figure,
    title: str,
    height: int,
    *,
    rangeslider: bool = True,
) -> None:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15)),
        height=height,
        template="plotly_dark",
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="Inter, system-ui, sans-serif"),

        xaxis=dict(
            rangeslider=dict(visible=rangeslider, thickness=0.04),
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


# ── MACD + RSI chart ────────────────────────────────────────────────────────

def plot_macd_rsi(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    rsi_period: int,
    rsi_overbought: float = 0.70,
    rsi_oversold: float = 0.30,
    *,
    instrument_label: str = "BTC-USD-PERP",
    bar_label: str = "1h",
    height: int = 900,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> go.Figure:
    """3-panel chart: candlesticks + trades, MACD + signal + histogram, RSI.

    Parameters
    ----------
    bars:
        Ordered list of NT Bar objects.
    fills_report:
        DataFrame from ``engine.trader.generate_order_fills_report()``.
    macd_fast / macd_slow / macd_signal:
        MACD fast EMA, slow EMA, and signal EMA periods.
    rsi_period:
        RSI period.
    rsi_overbought / rsi_oversold:
        RSI threshold levels (0.0-1.0 scale) drawn as horizontal lines.
    instrument_label / bar_label:
        Display strings for the chart title.
    height:
        Figure height in pixels.

    Returns
    -------
    go.Figure
    """
    df = _bars_to_macd_rsi_df(bars, macd_fast, macd_slow, macd_signal, rsi_period)
    buys, sells = _parse_fills(fills_report)

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.6, 0.2, 0.2],
        subplot_titles=("Price", "MACD", "RSI"),
    )

    # Row 1: Candlesticks + trade markers
    _add_candlesticks(fig, df, row=1)
    _add_trade_markers(fig, buys, sells, df, row=1)
    _add_close_cause_markers(fig, exit_classification, df, row=1)
    _add_account_liq_marker(fig, account_liq_event)

    # Row 2: MACD panel
    _add_macd_panel(fig, df, macd_fast, macd_slow, macd_signal, row=2)

    # Row 3: RSI panel
    _add_rsi_panel(fig, df, rsi_period, rsi_overbought, rsi_oversold, row=3)

    _apply_macd_rsi_layout(
        fig, macd_fast, macd_slow, macd_signal, rsi_period,
        instrument_label, bar_label, height,
    )

    return fig


def _bars_to_macd_rsi_df(
    bars: list[Bar],
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    rsi_period: int,
) -> pd.DataFrame:
    """Convert NT Bars to OHLCV DataFrame with MACD, signal, histogram, RSI."""
    macd = MovingAverageConvergenceDivergence(macd_fast, macd_slow)
    signal_ema = ExponentialMovingAverage(macd_signal)
    rsi = RelativeStrengthIndex(rsi_period)

    rows = []
    for bar in bars:
        macd.handle_bar(bar)
        rsi.handle_bar(bar)
        if macd.initialized:
            signal_ema.update_raw(macd.value)

        macd_val = macd.value if macd.initialized else np.nan
        sig_val = signal_ema.value if signal_ema.initialized else np.nan
        hist_val = (macd_val - sig_val) if (macd.initialized and signal_ema.initialized) else np.nan

        rows.append({
            "ts":    pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
            "open":  float(bar.open),
            "high":  float(bar.high),
            "low":   float(bar.low),
            "close": float(bar.close),
            "vol":   float(bar.volume),
            "macd":  macd_val,
            "signal": sig_val,
            "histogram": hist_val,
            "rsi":   rsi.value if rsi.initialized else np.nan,
        })

    return pd.DataFrame(rows).set_index("ts")


def _add_macd_panel(
    fig: go.Figure,
    df: pd.DataFrame,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    row: int,
) -> None:
    """Add MACD line, signal line, and histogram bars."""
    # Histogram as bar chart (green positive, red negative)
    colors = [_GREEN if v >= 0 else _RED for v in df["histogram"].fillna(0)]
    fig.add_trace(go.Bar(
        x=df.index,
        y=df["histogram"],
        name="Histogram",
        marker_color=colors,
        opacity=0.5,
        showlegend=False,
    ), row=row, col=1)

    # MACD line
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["macd"],
        name=f"MACD({macd_fast},{macd_slow})",
        mode="lines",
        line=dict(color=_AMBER, width=1.5),
    ), row=row, col=1)

    # Signal line
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["signal"],
        name=f"Signal({macd_signal})",
        mode="lines",
        line=dict(color=_BLUE, width=1.5),
    ), row=row, col=1)


def _add_rsi_panel(
    fig: go.Figure,
    df: pd.DataFrame,
    rsi_period: int,
    rsi_overbought: float,
    rsi_oversold: float,
    row: int,
) -> None:
    """Add RSI line with overbought/oversold horizontal markers."""
    # RSI line
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["rsi"],
        name=f"RSI({rsi_period})",
        mode="lines",
        line=dict(color=_TEXT, width=1.5),
    ), row=row, col=1)

    # Overbought / oversold horizontal lines
    for level, color, label in [
        (rsi_overbought, _RED, "OB"),
        (rsi_oversold, _GREEN, "OS"),
        (0.50, _GRID, "50"),
    ]:
        fig.add_hline(  # type: ignore[arg-type]  # plotly stubs type row/col as str
            y=level, row=row, col=1,
            line_dash="dash", line_color=color, line_width=1,
            annotation_text=label,
            annotation_position="right",
            annotation_font_color=color,
        )


def _apply_macd_rsi_layout(
    fig: go.Figure,
    macd_fast: int,
    macd_slow: int,
    macd_signal: int,
    rsi_period: int,
    instrument_label: str,
    bar_label: str,
    height: int,
) -> None:
    title = (
        f"{instrument_label} · {bar_label} · "
        f"MACD({macd_fast}/{macd_slow}/{macd_signal}) + RSI({rsi_period})"
    )
    _apply_base_layout(fig, title, height, rangeslider=False)

    # Disable rangeslider on all subplot x-axes
    fig.update_xaxes(rangeslider_visible=False)

    # Style all subplot axes
    for i in range(1, 4):
        fig.update_xaxes(gridcolor=_GRID, linecolor=_BORDER, row=i, col=1)
        fig.update_yaxes(gridcolor=_GRID, linecolor=_BORDER, side="right", row=i, col=1)

    # Price axis formatting
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", row=1, col=1)

    # RSI axis range
    fig.update_yaxes(range=[0, 1], row=3, col=1)

    # Bottom x-axis date formatting
    fig.update_xaxes(tickformat="%b %d\n%Y", row=3, col=1)


# ── BB Mean Reversion chart ─────────────────────────────────────────────────

_BB_FILL = "rgba(33, 150, 243, 0.08)"  # very light blue fill between bands
_BB_LINE = "rgba(33, 150, 243, 0.5)"   # semi-transparent blue band lines


def plot_bb_meanrev(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    bb_period: int,
    bb_std: float,
    rsi_period: int,
    rsi_buy_threshold: float = 0.30,
    rsi_sell_threshold: float = 0.70,
    *,
    instrument_label: str = "BTC-USD-PERP",
    bar_label: str = "1h",
    height: int = 800,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> go.Figure:
    """2-panel chart: candlesticks + BB bands + trades, RSI with thresholds.

    Parameters
    ----------
    bars:
        Ordered list of NT Bar objects.
    fills_report:
        DataFrame from ``engine.trader.generate_order_fills_report()``.
    bb_period / bb_std:
        Bollinger Bands period and standard deviation multiplier.
    rsi_period:
        RSI period.
    rsi_buy_threshold / rsi_sell_threshold:
        RSI threshold levels (0.0-1.0 scale) drawn as horizontal lines.
    instrument_label / bar_label:
        Display strings for the chart title.
    height:
        Figure height in pixels.

    Returns
    -------
    go.Figure
    """
    df = _bars_to_bb_rsi_df(bars, bb_period, bb_std, rsi_period)
    buys, sells = _parse_fills(fills_report)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
        subplot_titles=("Price", "RSI"),
    )

    # Row 1: Candlesticks + BB bands + trade markers
    _add_candlesticks(fig, df, row=1)
    _add_bb_bands(fig, df, bb_period, bb_std, row=1)
    _add_trade_markers(fig, buys, sells, df, row=1)
    _add_close_cause_markers(fig, exit_classification, df, row=1)
    _add_account_liq_marker(fig, account_liq_event)

    # Row 2: RSI panel (reuse existing helper)
    _add_rsi_panel(fig, df, rsi_period, rsi_sell_threshold, rsi_buy_threshold, row=2)

    _apply_bb_meanrev_layout(
        fig, bb_period, bb_std, rsi_period,
        instrument_label, bar_label, height,
    )

    return fig


def _bars_to_bb_rsi_df(
    bars: list[Bar],
    bb_period: int,
    bb_std: float,
    rsi_period: int,
) -> pd.DataFrame:
    """Convert NT Bars to OHLCV DataFrame with BB bands and RSI."""
    bb = BollingerBands(bb_period, bb_std)
    rsi = RelativeStrengthIndex(rsi_period)

    rows = []
    for bar in bars:
        bb.handle_bar(bar)
        rsi.handle_bar(bar)
        rows.append({
            "ts":        pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
            "open":      float(bar.open),
            "high":      float(bar.high),
            "low":       float(bar.low),
            "close":     float(bar.close),
            "vol":       float(bar.volume),
            "bb_upper":  bb.upper if bb.initialized else np.nan,
            "bb_middle": bb.middle if bb.initialized else np.nan,
            "bb_lower":  bb.lower if bb.initialized else np.nan,
            "rsi":       rsi.value if rsi.initialized else np.nan,
        })

    return pd.DataFrame(rows).set_index("ts")


def _add_bb_bands(
    fig: go.Figure,
    df: pd.DataFrame,
    bb_period: int,
    bb_std: float,
    row: int,
) -> None:
    """Add Bollinger Bands overlay with semi-transparent fill between bands."""
    # Lower band first (fill='tonexty' on upper band fills between them)
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["bb_lower"],
        name=f"BB Lower",
        mode="lines",
        line=dict(color=_BB_LINE, width=1),
        showlegend=False,
    ), row=row, col=1)

    # Upper band with fill down to lower band
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["bb_upper"],
        name=f"BB({bb_period}, {bb_std})",
        mode="lines",
        line=dict(color=_BB_LINE, width=1),
        fill="tonexty",
        fillcolor=_BB_FILL,
    ), row=row, col=1)

    # Middle band (SMA baseline)
    fig.add_trace(go.Scatter(
        x=df.index,
        y=df["bb_middle"],
        name=f"SMA({bb_period})",
        mode="lines",
        line=dict(color=_AMBER, width=1, dash="dash"),
    ), row=row, col=1)


def _apply_bb_meanrev_layout(
    fig: go.Figure,
    bb_period: int,
    bb_std: float,
    rsi_period: int,
    instrument_label: str,
    bar_label: str,
    height: int,
) -> None:
    title = (
        f"{instrument_label} · {bar_label} · "
        f"BB({bb_period}, {bb_std}) + RSI({rsi_period})"
    )
    _apply_base_layout(fig, title, height, rangeslider=False)

    # Disable rangeslider on all subplot x-axes
    fig.update_xaxes(rangeslider_visible=False)

    # Style all subplot axes
    for i in range(1, 3):
        fig.update_xaxes(gridcolor=_GRID, linecolor=_BORDER, row=i, col=1)
        fig.update_yaxes(gridcolor=_GRID, linecolor=_BORDER, side="right", row=i, col=1)

    # Price axis formatting
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", row=1, col=1)

    # RSI axis range
    fig.update_yaxes(range=[0, 1], row=2, col=1)

    # Bottom x-axis date formatting
    fig.update_xaxes(tickformat="%b %d\n%Y", row=2, col=1)


# ── Donchian Channel Breakout chart ──────────────────────────────────────────

_DC_FILL = "rgba(255, 152, 0, 0.08)"   # very light orange fill between entry bands
_DC_LINE = "rgba(255, 152, 0, 0.5)"    # semi-transparent orange entry band lines


def plot_donchian_breakout(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    entry_period: int,
    exit_period: int,
    *,
    instrument_label: str = "BTC-USD-PERP",
    bar_label: str = "1h",
    height: int = 600,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> go.Figure:
    """Candlestick chart with dual Donchian Channel bands and trade markers.

    Parameters
    ----------
    bars:
        Ordered list of NT Bar objects.
    fills_report:
        DataFrame from ``engine.trader.generate_order_fills_report()``.
    entry_period:
        Entry channel period (longer).
    exit_period:
        Exit channel period (shorter).
    instrument_label / bar_label:
        Display strings for the chart title.
    height:
        Figure height in pixels.

    Returns
    -------
    go.Figure
    """
    df = _bars_to_donchian_df(bars, entry_period, exit_period)
    buys, sells = _parse_fills(fills_report)

    fig = go.Figure()
    _add_candlesticks(fig, df)
    _add_dc_bands(fig, df, entry_period, exit_period)
    _add_trade_markers(fig, buys, sells, df)
    _add_close_cause_markers(fig, exit_classification, df)
    _add_account_liq_marker(fig, account_liq_event)
    title = (
        f"{instrument_label} · {bar_label} · "
        f"DonchianBreakout(entry={entry_period}, exit={exit_period})"
    )
    _apply_base_layout(fig, title, height)
    return fig


def _bars_to_donchian_df(
    bars: list[Bar],
    entry_period: int,
    exit_period: int,
) -> pd.DataFrame:
    """Convert NT Bars to OHLCV DataFrame with Donchian Channel bands."""
    dc_entry = DonchianChannel(entry_period)
    dc_exit = DonchianChannel(exit_period)

    rows = []
    for bar in bars:
        dc_entry.handle_bar(bar)
        dc_exit.handle_bar(bar)
        rows.append({
            "ts":              pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
            "open":            float(bar.open),
            "high":            float(bar.high),
            "low":             float(bar.low),
            "close":           float(bar.close),
            "vol":             float(bar.volume),
            "dc_entry_upper":  dc_entry.upper if dc_entry.initialized else np.nan,
            "dc_entry_lower":  dc_entry.lower if dc_entry.initialized else np.nan,
            "dc_entry_middle": dc_entry.middle if dc_entry.initialized else np.nan,
            "dc_exit_upper":   dc_exit.upper if dc_exit.initialized else np.nan,
            "dc_exit_lower":   dc_exit.lower if dc_exit.initialized else np.nan,
        })

    return pd.DataFrame(rows).set_index("ts")


def _add_dc_bands(
    fig: go.Figure,
    df: pd.DataFrame,
    entry_period: int,
    exit_period: int,
) -> None:
    """Add dual Donchian Channel overlay: entry band fill + exit channel dashes."""
    # Entry channel lower (fill='tonexty' on upper fills between them)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["dc_entry_lower"],
        name="Entry Lower",
        mode="lines",
        line=dict(color=_DC_LINE, width=1),
        showlegend=False,
    ))

    # Entry channel upper with fill to lower
    fig.add_trace(go.Scatter(
        x=df.index, y=df["dc_entry_upper"],
        name=f"Entry DC({entry_period})",
        mode="lines",
        line=dict(color=_DC_LINE, width=1),
        fill="tonexty",
        fillcolor=_DC_FILL,
    ))

    # Entry channel middle (midpoint)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["dc_entry_middle"],
        name=f"Mid({entry_period})",
        mode="lines",
        line=dict(color=_AMBER, width=1, dash="dash"),
    ))

    # Exit channel upper (dotted red — triggers short exit)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["dc_exit_upper"],
        name=f"Exit Upper({exit_period})",
        mode="lines",
        line=dict(color=_RED, width=1, dash="dot"),
    ))

    # Exit channel lower (dotted green — triggers long exit)
    fig.add_trace(go.Scatter(
        x=df.index, y=df["dc_exit_lower"],
        name=f"Exit Lower({exit_period})",
        mode="lines",
        line=dict(color=_GREEN, width=1, dash="dot"),
    ))


# ── Matplotlib display helpers ───────────────────────────────────────────────


def plot_equity_curve(
    *args,
    currency: str = "USDC",
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> None:
    """Plot the event-time account balance curve with running peak + drawdown.

    Pulls ``total`` from NT's account report and draws three series on a
    shared time axis:

    * **Equity (event-time)** — total balance at every NT account-state
      event (fills, position changes).  This is *not* a daily mark-to-market
      curve.  Between events the line is a step (last known balance);
      intra-event price drift on open positions is invisible.
    * **Running peak** — equity high-water mark to that point.
    * **Drawdown ($)** — ``running_peak - equity`` on a secondary axis,
      so the depth and duration of underwater periods are visible at a
      glance.

    The previous implementation used ``analyzer.returns()`` which is the
    upstream-broken zero-padded daily series.  We deliberately do not
    plot any returns-derived series until upstream fixes the methodology.
    See ``docs/ANALYZER_RETURNS_CAVEAT.md``.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    account_report : pd.DataFrame | None
        DataFrame from ``engine.trader.generate_account_report(venue)``,
        with ``total`` column.  May be ``None`` or empty.
    title : str
        Chart title string (e.g. ``"EMACross(20/50)  BTC 1h"``).
    currency : str, keyword-only
        Settlement currency label for the y-axis.  Default ``"USDC"``.

    Notes
    -----
    Two call signatures are accepted:

    * **Current**: ``plot_equity_curve(account_report, title, currency=...)``.
    * **Legacy**: ``plot_equity_curve(analyzer, account_report, title)``.
      The ``analyzer`` argument is silently ignored — kept solely so that
      pre-overhaul backtest notebooks don't error.

    """
    # Signature back-compat: legacy notebooks call with (analyzer, df, title);
    # current call is (df, title).  Detect by argument count and types.
    if len(args) == 3:
        # Legacy: (analyzer, account_report, title) — ignore the analyzer.
        _, account_report, title = args
    elif len(args) == 2:
        account_report, title = args
    else:
        msg = (
            f"plot_equity_curve expected 2 or 3 positional args "
            f"(account_report, title) — got {len(args)}"
        )
        raise TypeError(msg)

    if account_report is None or account_report.empty:
        print("No account report data available for equity curve.")
        return

    equity = account_report["total"].astype(float).copy()
    # account_report can have multiple rows per timestamp (locked vs free).
    # Collapse to one row per timestamp by taking the last balance.
    equity = equity.groupby(equity.index).last().sort_index()
    peak = equity.cummax()
    drawdown_abs = peak - equity

    fig, ax = plt.subplots(figsize=(14, 5))
    equity.plot(ax=ax, color="#1f77b4", label="Equity (event-time)", linewidth=1.5)
    peak.plot(ax=ax, color="#888", label="Running peak", linestyle="--", linewidth=1.0)
    ax.set_ylabel(f"Balance ({currency})")
    ax.grid(True, alpha=0.2)

    ax2 = ax.twinx()
    ax2.fill_between(
        drawdown_abs.index, 0, drawdown_abs.values,
        color="#d62728", alpha=0.25, label="Drawdown ($)",
    )
    ax2.set_ylabel(f"Drawdown ({currency})", color="#d62728")
    ax2.tick_params(axis="y", labelcolor="#d62728")
    ax2.invert_yaxis()  # drawdown grows downward visually

    # ── Close-cause overlay (protective_stop + liquidation) ──────────────
    # Subtle vertical lines at every stop/liq fill so equity collapses
    # tied to forced exits become visually attributable. Drawn on the
    # equity axis so they don't fight the drawdown axis fill.
    n_stops = n_liqs = 0
    if exit_classification is not None and not exit_classification.empty \
            and "close_cause" in exit_classification.columns:
        ts_index = pd.to_datetime(
            exit_classification["ts_closed"].astype("int64"), unit="ns", utc=True,
        )
        cls = exit_classification["close_cause"]
        stop_ts = ts_index[cls == "protective_stop"]
        liq_ts  = ts_index[cls == "liquidation"]
        n_stops = len(stop_ts)
        n_liqs  = len(liq_ts)
        for t in stop_ts:
            ax.axvline(t, color=_PSTOP_COLOR, alpha=0.35, linewidth=0.8)
        for t in liq_ts:
            ax.axvline(t, color=_LIQ_COLOR, alpha=0.55, linewidth=1.0)
        # Single legend entry per cause via empty-data proxy lines
        if n_stops:
            ax.plot([], [], color=_PSTOP_COLOR, alpha=0.6, linewidth=1.5,
                    label=f"Protective stop ({n_stops})")
        if n_liqs:
            ax.plot([], [], color=_LIQ_COLOR, alpha=0.8, linewidth=1.5,
                    label=f"Position liquidation ({n_liqs})")

    # ── Account-liq vertical band ────────────────────────────────────────
    if account_liq_event:
        liq_ts_ns = account_liq_event.get("liq_ts")
        if liq_ts_ns is not None:
            x_liq = pd.Timestamp(int(liq_ts_ns), unit="ns", tz="UTC")
            ax.axvline(x_liq, color=_LIQ_COLOR, linewidth=2.0,
                       linestyle="--", label="ACCOUNT LIQ")

    # Combine legends from both axes
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left")

    ax.set_title(
        f"Equity & drawdown — {title}\n"
        "(event-time, NOT daily MTM — intra-event drift on open positions invisible)",
        fontsize=11,
    )
    ax.set_xlabel("Time")
    fig.tight_layout()
    plt.show()


def plot_drawdown_distribution(
    account_report: pd.DataFrame | None,
    *,
    title: str = "Drawdown distribution",
    currency: str = "USDC",
    bar_interval_ns: int | None = None,
) -> None:
    """Two-panel chart: drawdown depth distribution + duration distribution.

    Complements the equity & drawdown chart (which shows individual
    drawdowns over time) by aggregating across all underwater periods
    in the run.  Answers the questions the time-axis chart can't:

    * "How deep are typical drawdowns?" (depth histogram)
    * "How long was I underwater on average?" (duration histogram)
    * "What's the worst-case duration I have to be ready for?"
      (annotated max duration)

    Long drawdowns kill psychology much more than deep ones — a 30%
    drawdown that recovers in 2 weeks is easier to stomach than a 10%
    drawdown that takes 18 months to unwind.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    account_report
        DataFrame from ``engine.trader.generate_account_report(venue)``.
    title
        Suptitle for the figure.
    currency
        Currency label (used in depth-axis label).
    bar_interval_ns
        Bar interval in nanoseconds.  When provided, durations are
        reported in bars; otherwise in days.

    """
    from src.backtesting.metrics import compute_drawdown_periods

    if account_report is None or account_report.empty:
        print("No account report data — skipping drawdown distribution.")
        return

    balance = account_report["total"].astype(float).copy()
    periods = compute_drawdown_periods(balance)
    if not periods:
        print("No drawdowns in this run (monotonically increasing equity).")
        return

    depths_pct = np.array([p["depth_pct"] * 100 for p in periods])
    durations_secs = np.array([p["duration_seconds"] for p in periods])
    if bar_interval_ns:
        durations = durations_secs * 1e9 / bar_interval_ns
        dur_unit = "bars"
    else:
        durations = durations_secs / 86400
        dur_unit = "days"
    n_recovered = sum(1 for p in periods if p["recovered"])
    n_open = len(periods) - n_recovered

    fig, axes = plt.subplots(1, 2, figsize=(14, 4.5))
    fig.suptitle(
        f"{title}  ·  {len(periods)} drawdowns  "
        f"({n_recovered} recovered, {n_open} open at end)",
        fontsize=13,
    )

    # Panel 1: Depth distribution
    ax = axes[0]
    bins = max(15, min(50, len(periods) // 2))
    ax.hist(depths_pct, bins=bins, color="#d62728", alpha=0.7,
            edgecolor="#891a1b")
    ax.axvline(depths_pct.mean(), color="black", linestyle="-", linewidth=1.0,
               label=f"Mean = {depths_pct.mean():.1f}%")
    ax.axvline(np.median(depths_pct), color="black", linestyle="--",
               linewidth=0.8,
               label=f"Median = {np.median(depths_pct):.1f}%")
    ax.axvline(depths_pct.max(), color="#891a1b", linestyle=":",
               linewidth=0.8,
               label=f"Max = {depths_pct.max():.1f}%")
    ax.set_xlabel("Drawdown depth (%)")
    ax.set_ylabel("# drawdowns")
    ax.set_title("Drawdown depth")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.2)

    # Panel 2: Duration distribution
    ax = axes[1]
    bins = max(15, min(50, len(periods) // 2))
    ax.hist(durations, bins=bins, color="#ff7f0e", alpha=0.7,
            edgecolor="#b35900")
    ax.axvline(durations.mean(), color="black", linestyle="-", linewidth=1.0,
               label=f"Mean = {durations.mean():.1f} {dur_unit}")
    ax.axvline(np.median(durations), color="black", linestyle="--",
               linewidth=0.8,
               label=f"Median = {np.median(durations):.1f} {dur_unit}")
    ax.axvline(durations.max(), color="#b35900", linestyle=":", linewidth=0.8,
               label=f"Max = {durations.max():.0f} {dur_unit}")
    ax.set_xlabel(f"Drawdown duration ({dur_unit})")
    ax.set_ylabel("# drawdowns")
    ax.set_title("Drawdown duration")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.2)

    # Highlight long drawdowns warning if max duration is > 90 days equivalent
    long_dd_threshold_secs = 90 * 86400  # 90 days
    if durations_secs.max() > long_dd_threshold_secs:
        ax.text(
            0.5, 0.95,
            f"⚠️ longest drawdown: "
            f"{durations_secs.max() / 86400:.0f} days",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=9, color="#b35900",
            bbox={"facecolor": "#fff8e1", "alpha": 0.9, "edgecolor": "#b35900"},
        )

    plt.tight_layout()
    plt.show()


def plot_bootstrap_pnl(
    bootstrap_dist: dict[str, float],
    *,
    title: str = "Bootstrap PnL distribution",
    currency: str = "USDC",
) -> None:
    """Visualise a bootstrap-PnL confidence interval as a synthetic histogram.

    The metrics module's ``bootstrap_total_pnl`` returns summary stats
    only (mean / std / 5/25/50/75/95 percentiles) — not the full sample.
    This chart reconstructs an approximate histogram from those summary
    stats by drawing a Gaussian centered at ``mean`` with width ``std``
    and overlaying the empirical percentile lines.  It's a visual aid
    for the dispersion, not a true reproduction of the bootstrap
    distribution.

    The actual-total line shows where the strategy's realized PnL
    sits within the resampled distribution — your eye should see
    immediately whether it's at the mean, in the upper tail, or in
    the lower tail.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    bootstrap_dist
        Output of ``src.backtesting.metrics.bootstrap_total_pnl(...)``.
    title
        Suptitle for the figure.
    currency
        Currency label for the x-axis.

    """
    if not bootstrap_dist or math.isnan(bootstrap_dist.get("mean", float("nan"))):
        print("No bootstrap distribution to plot.")
        return

    mean = bootstrap_dist["mean"]
    std = bootstrap_dist["std"]
    actual = bootstrap_dist["actual_total"]
    pct5 = bootstrap_dist["pct_5"]
    pct25 = bootstrap_dist["pct_25"]
    median = bootstrap_dist["median"]
    pct75 = bootstrap_dist["pct_75"]
    pct95 = bootstrap_dist["pct_95"]
    n_iter = bootstrap_dist.get("n_iterations", 0)
    n_trades = bootstrap_dist.get("n_trades", 0)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.suptitle(
        f"{title}  ·  {n_iter:,} resamples of {n_trades} trades",
        fontsize=13,
    )

    # Synthetic Gaussian histogram visual (purely for shape — actual
    # bootstrap is reflected via the percentile lines).
    rng = np.random.default_rng(seed=0)
    synth = rng.normal(mean, std, 5000) if std > 0 else np.full(5000, mean)
    ax.hist(synth, bins=60, color="#1f77b4", alpha=0.35, edgecolor="#0f4c81",
            label="approx. distribution shape")

    # Percentile bands
    ax.axvspan(pct5, pct95, alpha=0.10, color="#1f77b4",
               label="5–95 pct band")
    ax.axvspan(pct25, pct75, alpha=0.20, color="#1f77b4",
               label="25–75 pct band")
    ax.axvline(median, color="#0f4c81", linestyle="-", linewidth=1.5,
               label=f"Median = {median:,.0f}")
    ax.axvline(actual, color="#d62728", linestyle="-", linewidth=2.0,
               label=f"Actual = {actual:,.0f}")

    # Verdict text
    if actual >= pct95:
        verdict = "Actual is in the TOP 5% of resamples — lucky path?"
        verdict_color = "#b35900"
    elif actual >= pct75:
        verdict = "Actual is in the 75–95th percentile — above-average path."
        verdict_color = "#26a69a"
    elif actual >= pct25:
        verdict = "Actual is within the central 50% — typical path."
        verdict_color = "#444"
    elif actual >= pct5:
        verdict = "Actual is in the 5–25th percentile — below-average path."
        verdict_color = "#666"
    else:
        verdict = "Actual is in the BOTTOM 5% of resamples — unlucky path?"
        verdict_color = "#b35900"
    ax.text(
        0.02, 0.95, verdict,
        transform=ax.transAxes, ha="left", va="top",
        fontsize=10, color=verdict_color,
        bbox={"facecolor": "#f0f0f0", "alpha": 0.85, "edgecolor": verdict_color},
    )

    ax.set_xlabel(f"Total PnL ({currency})")
    ax.set_ylabel("Density (synthetic shape)")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    plt.show()


def plot_bootstrap_drawdown(
    bootstrap_dist: dict[str, float],
    *,
    title: str = "Bootstrap max-drawdown distribution",
    currency: str = "USDC",
) -> None:
    """Visualise a bootstrap max-drawdown CI as a synthetic histogram.

    Companion to :func:`plot_bootstrap_pnl`.  Drawdowns are non-positive
    so the x-axis spans negative values; the **worst-case tail** is
    ``pct_5`` (left edge), the **least-bad tail** is ``pct_95`` (right
    edge).  The actual-historical drawdown is overlaid as a red line —
    if it sits in the right tail (less bad than median), the historical
    path was relatively gentle; if it's in the left tail, the historical
    path was unusually rough and the realistic forward worst-case is
    deeper.

    See :func:`plot_bootstrap_pnl` for caveats — the IID resampling
    assumption underestimates real drawdown clustering.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    bootstrap_dist
        Output of ``src.backtesting.metrics.bootstrap_max_drawdown(...)``.
    title
        Suptitle for the figure.
    currency
        Currency label for the x-axis.

    """
    if not bootstrap_dist or math.isnan(bootstrap_dist.get("mean", float("nan"))):
        print("No bootstrap-drawdown distribution to plot.")
        return

    mean = bootstrap_dist["mean"]
    std = bootstrap_dist["std"]
    actual = bootstrap_dist["actual_max_drawdown"]
    pct5 = bootstrap_dist["pct_5"]      # WORST tail (most negative)
    pct25 = bootstrap_dist["pct_25"]
    median = bootstrap_dist["median"]
    pct75 = bootstrap_dist["pct_75"]
    pct95 = bootstrap_dist["pct_95"]    # LEAST-BAD tail (closest to zero)
    n_iter = bootstrap_dist.get("n_iterations", 0)
    n_trades = bootstrap_dist.get("n_trades", 0)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.suptitle(
        f"{title}  ·  {n_iter:,} resamples of {n_trades} trades",
        fontsize=13,
    )

    # Synthetic Gaussian shape — same trick as plot_bootstrap_pnl
    rng = np.random.default_rng(seed=0)
    synth = rng.normal(mean, std, 5000) if std > 0 else np.full(5000, mean)
    ax.hist(synth, bins=60, color="#d62728", alpha=0.35, edgecolor="#7f1f1f",
            label="approx. distribution shape")

    # Percentile bands — note pct5 < pct95 numerically (both negative)
    ax.axvspan(pct5, pct95, alpha=0.10, color="#d62728",
               label="5–95 pct band")
    ax.axvspan(pct25, pct75, alpha=0.20, color="#d62728",
               label="25–75 pct band")
    ax.axvline(median, color="#7f1f1f", linestyle="-", linewidth=1.5,
               label=f"Median = {median:,.0f}")
    ax.axvline(pct5, color="#7f1f1f", linestyle="--", linewidth=1.2,
               label=f"Worst 5% = {pct5:,.0f}")
    ax.axvline(actual, color="#0f4c81", linestyle="-", linewidth=2.0,
               label=f"Actual = {actual:,.0f}")

    # Verdict — flipped vs PnL because more-negative is worse
    if actual <= pct5:
        verdict = "Actual MDD is in the WORST 5% of resamples — bad path?"
        verdict_color = "#b35900"
    elif actual <= pct25:
        verdict = "Actual MDD is in the 5–25th pct — worse than median path."
        verdict_color = "#444"
    elif actual <= pct75:
        verdict = "Actual MDD is within the central 50% — typical path."
        verdict_color = "#444"
    elif actual <= pct95:
        verdict = "Actual MDD is in the 75–95th pct — better than median path."
        verdict_color = "#26a69a"
    else:
        verdict = "Actual MDD is in the BEST 5% — unusually shallow drawdown."
        verdict_color = "#26a69a"
    ax.text(
        0.02, 0.95, verdict,
        transform=ax.transAxes, ha="left", va="top",
        fontsize=10, color=verdict_color,
        bbox={"facecolor": "#f0f0f0", "alpha": 0.85, "edgecolor": verdict_color},
    )

    ax.set_xlabel(f"Max drawdown ({currency})")
    ax.set_ylabel("Density (synthetic shape)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    plt.show()


def plot_baselines_comparison(
    *,
    strategy_pnl: float,
    buy_and_hold_pnl: float | None = None,
    random_entry_dist: dict[str, Any] | None = None,
    title: str = "Strategy vs baselines",
    currency: str = "USDC",
) -> None:
    """Side-by-side comparison: strategy vs buy-and-hold vs random-entry distribution.

    Renders three visual elements in one figure:

    1. **Bars**: strategy PnL and buy-and-hold PnL.  Sign-coded.
    2. **Random-entry distribution** as a horizontal whisker plot
       showing 5/25/50/75/95 percentiles, with the strategy's PnL
       overlaid as a vertical line.  Tells you "where in the random
       distribution does my strategy land?"
    3. **Verdict text**: percentile rank of the strategy within the
       random-entry distribution, plus a note on B&H comparison.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    strategy_pnl
        Your strategy's total PnL.
    buy_and_hold_pnl
        Output of ``baselines.buy_and_hold(bars, ...)``'s ``"pnl"`` key.
        ``None`` skips the B&H bar.
    random_entry_dist
        Full output dict of ``baselines.random_entry_baseline(...)``.
        ``None`` skips the random-entry whisker plot.
    title
        Suptitle.
    currency
        Currency label for axes.

    """
    has_bh = buy_and_hold_pnl is not None
    has_random = (
        random_entry_dist is not None
        and not math.isnan(random_entry_dist.get("mean_pnl", float("nan")))
    )

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.suptitle(title, fontsize=13)

    # Bars: strategy + B&H
    labels = ["Strategy"]
    pnls = [strategy_pnl]
    colors = ["#2ca02c" if strategy_pnl > 0 else "#d62728"]
    if has_bh:
        bh = float(buy_and_hold_pnl)  # type: ignore[arg-type]
        labels.append("Buy & Hold")
        pnls.append(bh)
        colors.append("#2ca02c" if bh > 0 else "#d62728")

    bars = ax.barh(labels, pnls, color=colors, alpha=0.75, edgecolor="black")
    for b, v in zip(bars, pnls, strict=False):
        x = b.get_width()
        ax.text(
            x + (max(abs(p) for p in pnls) * 0.02 * (1 if x >= 0 else -1)),
            b.get_y() + b.get_height() / 2,
            f"{v:,.0f}",
            ha="left" if x >= 0 else "right",
            va="center",
            fontsize=10,
        )
    ax.axvline(0, color="black", linewidth=0.5)
    ax.set_xlabel(f"PnL ({currency})")
    ax.grid(True, alpha=0.2, axis="x")

    # Random-entry whisker overlay
    if has_random:
        d = random_entry_dist  # type: ignore[assignment]
        # Draw whiskers above the bars
        y = -0.6  # below the bars
        ax.plot(
            [d["pct_5"], d["pct_95"]], [y, y],
            color="#888", linewidth=2, label="Random entry: 5–95 pct",
        )
        ax.plot(
            [d["pct_25"], d["pct_75"]], [y, y],
            color="#444", linewidth=6, alpha=0.6,
            label="Random entry: 25–75 pct",
        )
        ax.plot(
            d["median_pnl"], y, "D", color="#fff",
            markersize=8, markeredgecolor="#444",
            label=f"Random median ({d['median_pnl']:,.0f})",
        )
        # Vertical line at strategy PnL (extends through the random whisker row)
        ax.axvline(
            strategy_pnl, color="#2962ff", linestyle="--",
            linewidth=1.5, alpha=0.8,
            label=f"Strategy ({strategy_pnl:,.0f})",
        )
        ax.set_ylim(-1.0, 1.5)

        # Verdict
        n = d["n_simulations"]
        # Approximate percentile of strategy in the simulation distribution
        # (we don't keep the full sim, so use percentile bands)
        pct_estimate = "below 5th"
        if strategy_pnl >= d["pct_95"]:
            pct_estimate = "above 95th"
        elif strategy_pnl >= d["pct_75"]:
            pct_estimate = "75th–95th"
        elif strategy_pnl >= d["median_pnl"]:
            pct_estimate = "50th–75th"
        elif strategy_pnl >= d["pct_25"]:
            pct_estimate = "25th–50th"
        elif strategy_pnl >= d["pct_5"]:
            pct_estimate = "5th–25th"
        ax.text(
            0.02, 0.95,
            f"Random-entry sims: {n}  ·  Strategy ranks: {pct_estimate} pct",
            transform=ax.transAxes,
            fontsize=10,
            verticalalignment="top",
            bbox={"facecolor": "#f0f0f0", "alpha": 0.8, "edgecolor": "#888"},
        )
        ax.legend(loc="lower right", fontsize=8)
    else:
        ax.text(
            0.5, 0.95, "(no random-entry distribution provided)",
            transform=ax.transAxes, ha="center", va="top",
            color="#888", fontsize=10,
        )

    plt.tight_layout()
    plt.show()


def plot_yearly_breakdown(
    yearly_df: pd.DataFrame,
    *,
    title: str = "Performance by year",
    currency: str = "USDC",
) -> None:
    """Plot per-year PnL bars + per-year win-rate / profit-factor lines.

    Two-panel side-by-side:

    1. **PnL bars** — one bar per year, green/red sign-coded.  Shows
       year-over-year consistency at a glance.  A strategy that's
       +500% one year and -50% the next is regime-dependent.
    2. **Trade-quality lines** — win rate and profit factor on twin
       axes per year.  Diverging direction (rising win rate but
       falling PF, or vice versa) flags a behavioral shift in the
       strategy across regimes.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    yearly_df
        Output of ``src.backtesting.analysis.performance_by_year``.
        Must be indexed by year and contain ``pnl``, ``win_rate``,
        ``profit_factor``, ``num_positions`` columns.
    title
        Suptitle.
    currency
        Currency label for the PnL axis.

    """
    if yearly_df.empty:
        print("No yearly data to plot.")
        return

    fig, axes = plt.subplots(1, 2, figsize=(14, 4.5))
    fig.suptitle(title, fontsize=13)

    years = yearly_df.index.tolist()
    pnls = yearly_df["pnl"].tolist()
    n_pos = yearly_df["num_positions"].tolist()

    # ── Panel 1: yearly PnL bars ─────────────────────────────────────────
    ax = axes[0]
    colors = ["#2ca02c" if v > 0 else "#d62728" for v in pnls]
    bars = ax.bar(
        [str(y) for y in years], pnls,
        color=colors, alpha=0.7, edgecolor="black",
    )
    for b, v, n in zip(bars, pnls, n_pos, strict=False):
        label_y = b.get_height()
        ax.text(
            b.get_x() + b.get_width() / 2,
            label_y + (50 if v >= 0 else -50),
            f"{v:,.0f}\n({n})",
            ha="center", va="bottom" if v >= 0 else "top",
            fontsize=9,
        )
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_ylabel(f"PnL ({currency})")
    ax.set_xlabel("Year")
    ax.set_title("PnL by year (trade count in parens)")
    ax.grid(True, alpha=0.2, axis="y")

    # ── Panel 2: win rate + profit factor lines ──────────────────────────
    ax = axes[1]
    win_rates = [float(v) * 100 for v in yearly_df["win_rate"].tolist()]
    ax.plot(
        [str(y) for y in years], win_rates,
        marker="o", color="#1f77b4", label="Win rate",
    )
    ax.set_ylabel("Win rate (%)", color="#1f77b4")
    ax.tick_params(axis="y", labelcolor="#1f77b4")
    ax.set_ylim(0, 100)

    ax2 = ax.twinx()
    pfs = [
        v if (v is not None and not (isinstance(v, float) and math.isnan(v))) else 0.0
        for v in yearly_df["profit_factor"].tolist()
    ]
    ax2.plot(
        [str(y) for y in years], pfs,
        marker="s", color="#ff7f0e", label="Profit factor",
    )
    ax2.set_ylabel("Profit factor", color="#ff7f0e")
    ax2.tick_params(axis="y", labelcolor="#ff7f0e")
    ax2.axhline(1.0, color="#ff7f0e", linestyle=":", linewidth=0.8)

    ax.set_xlabel("Year")
    ax.set_title("Trade quality by year")
    ax.grid(True, alpha=0.2)

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)

    plt.tight_layout()
    plt.show()


def print_yearly_breakdown(
    yearly_df: pd.DataFrame,
    *,
    currency: str = "USDC",
) -> None:
    """Pretty-print a per-year performance table.

    Companion to ``plot_yearly_breakdown`` for users who want the
    numbers without the chart, or alongside it.

    Parameters
    ----------
    yearly_df
        Output of ``src.backtesting.analysis.performance_by_year``.
    currency
        Label for the PnL columns.

    """
    if yearly_df.empty:
        print("No yearly data available.")
        return

    print(f"=== Performance by year ({currency}) ===")
    cols = [
        ("PnL", "pnl", "{:>12,.2f}"),
        ("PnL %", "pnl_pct", "{:>8,.2f}%"),
        ("Trades", "num_positions", "{:>6}"),
        ("Win Rate", "win_rate", "{:>8.2%}"),
        ("PF", "profit_factor", "{:>6.2f}"),
        ("Avg Win", "avg_winner", "{:>10,.2f}"),
        ("Avg Loss", "avg_loser", "{:>10,.2f}"),
        ("Largest Win", "largest_win", "{:>12,.2f}"),
        ("Largest Loss", "largest_loss", "{:>12,.2f}"),
    ]
    # Header
    header_parts = [f"{'Year':>6}"]
    for label, _key, fmt in cols:
        # Use the format string's width to align the header
        sample = fmt.format(0).replace("0.00", "x").replace("0", "x")
        header_parts.append(f"{label:>{len(sample)}}")
    print("  ".join(header_parts))
    print("-" * (len("  ".join(header_parts))))

    for year in yearly_df.index:
        row = yearly_df.loc[year]
        parts = [f"{int(year):>6}"]
        for _label, key, fmt in cols:
            val = row.get(key, float("nan"))
            try:
                parts.append(fmt.format(val))
            except (TypeError, ValueError):
                parts.append(f"{val!s:>10}")
        print("  ".join(parts))


def plot_trade_distributions(
    positions: list,
    *,
    title: str = "",
    bar_interval_ns: int | None = None,
    currency: str = "USDC",
    exit_classification: pd.DataFrame | None = None,
) -> None:
    """Three-panel: PnL distribution, duration distribution, top-trade share.

    Trustworthy trade-quality view that doesn't depend on the broken
    NT returns methodology.

    Panels:

    1. **PnL histogram** — distribution of per-trade realized PnL.
       Wins green, losers red.  Mean and median lines marked.  Reveals
       whether profits are spread across many trades or concentrated in
       a few outliers.
    2. **Trade duration histogram** — bars (or seconds) per trade.
       Bimodal often means "two strategies in one" (a short scalp tail
       + a long-hold tail).
    3. **Top-trade-share bar** — shows what fraction of total PnL comes
       from the top 1, top 3, top 5 winners and from the worst 1, 3, 5
       losers.  Concentration → fragility.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    positions
        List of NT Position objects (closed positions only are used —
        open positions don't have realized PnL).
    title
        Suptitle.  Empty string skips it.
    bar_interval_ns
        Bar interval in nanoseconds.  When provided, the duration panel
        x-axis is in bars; otherwise in days.
    currency
        Settlement currency label for axis.

    """
    closed = [
        p for p in positions
        if getattr(p, "is_closed", False)
        and getattr(p, "realized_pnl", None) is not None
    ]
    if not closed:
        print("No closed trades to plot.")
        return

    pnls = np.array(
        [float(p.realized_pnl.as_decimal()) for p in closed], dtype=float,
    )
    durations_ns = np.array(
        [int(p.ts_closed) - int(p.ts_opened) for p in closed], dtype=float,
    )
    if bar_interval_ns:
        durations = durations_ns / bar_interval_ns
        dur_unit = "bars"
    else:
        durations = durations_ns / 1e9 / 86400  # days
        dur_unit = "days"

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    if title:
        fig.suptitle(title, fontsize=13)

    # ── Panel 1: PnL distribution ────────────────────────────────────────
    ax = axes[0]
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    bins = max(20, min(60, len(pnls) // 3))
    if len(wins) > 0:
        ax.hist(
            wins, bins=bins, color="#2ca02c", alpha=0.6,
            edgecolor="#1a6e1a", label=f"Wins ({len(wins)})",
        )
    if len(losses) > 0:
        ax.hist(
            losses, bins=bins, color="#d62728", alpha=0.6,
            edgecolor="#891a1b", label=f"Losses ({len(losses)})",
        )
    ax.axvline(pnls.mean(), color="black", linestyle="-", linewidth=1.2,
               label=f"Mean = {pnls.mean():,.2f}")
    ax.axvline(np.median(pnls), color="black", linestyle="--", linewidth=1.0,
               label=f"Median = {np.median(pnls):,.2f}")
    ax.axvline(0, color="#888", linestyle=":", linewidth=0.8)

    # ── Close-cause overlay: stops + liqs as rug ticks above the bars ───
    # Stops/liqs are nearly always losers (forced exits at adverse prices),
    # so plotting them along the PnL axis quickly tells you "of my losing
    # trades, how many came from stop-outs vs strategy-driven exits".
    # Rug sits in the top 10% of the panel so the histogram bars stay clean.
    pstop_count = liq_count = 0
    if exit_classification is not None and not exit_classification.empty \
            and {"close_cause", "realized_pnl"}.issubset(exit_classification.columns):
        cls = exit_classification["close_cause"]
        pstop_pnls = exit_classification.loc[cls == "protective_stop", "realized_pnl"].astype(float).to_numpy()
        liq_pnls   = exit_classification.loc[cls == "liquidation",     "realized_pnl"].astype(float).to_numpy()
        pstop_count, liq_count = len(pstop_pnls), len(liq_pnls)
        y_top = ax.get_ylim()[1]
        if pstop_count:
            ax.scatter(pstop_pnls, np.full_like(pstop_pnls, y_top * 0.95),
                       marker="|", s=70, color=_PSTOP_COLOR,
                       label=f"Stops (n={pstop_count})", clip_on=False)
        if liq_count:
            ax.scatter(liq_pnls, np.full_like(liq_pnls, y_top * 0.88),
                       marker="x", s=50, color=_LIQ_COLOR,
                       label=f"Liquidations (n={liq_count})", clip_on=False)

    ax.set_xlabel(f"Trade PnL ({currency})")
    ax.set_ylabel("# trades")
    title_extra = ""
    if pstop_count or liq_count:
        title_extra = f"  ·  forced exits: {pstop_count} stop / {liq_count} liq"
    ax.set_title(f"PnL distribution per trade{title_extra}")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.2)

    # ── Panel 2: Duration distribution ───────────────────────────────────
    ax = axes[1]
    bins = max(20, min(60, len(durations) // 3))
    ax.hist(durations, bins=bins, color="#1f77b4", alpha=0.7, edgecolor="#0f4c81")
    ax.axvline(durations.mean(), color="black", linestyle="-", linewidth=1.2,
               label=f"Mean = {durations.mean():.1f} {dur_unit}")
    ax.axvline(np.median(durations), color="black", linestyle="--", linewidth=1.0,
               label=f"Median = {np.median(durations):.1f} {dur_unit}")
    ax.set_xlabel(f"Trade duration ({dur_unit})")
    ax.set_ylabel("# trades")
    ax.set_title("Trade duration distribution")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.2)

    # ── Panel 3: Concentration / top-trade share ─────────────────────────
    ax = axes[2]
    sorted_desc = np.sort(pnls)[::-1]  # winners first
    sorted_asc = np.sort(pnls)  # losers first
    total_abs = max(np.sum(np.abs(pnls)), 1e-9)
    sum_abs_total = float(np.sum(np.abs(pnls)))

    metrics_x = ["Top 1", "Top 3", "Top 5", "Bot 1", "Bot 3", "Bot 5"]
    metrics_y = []
    for n in (1, 3, 5):
        share = float(np.sum(sorted_desc[: min(n, len(sorted_desc))]))
        metrics_y.append(share / sum_abs_total * 100)
    for n in (1, 3, 5):
        share = float(np.sum(sorted_asc[: min(n, len(sorted_asc))]))
        metrics_y.append(share / sum_abs_total * 100)

    colors = ["#2ca02c", "#2ca02c", "#2ca02c", "#d62728", "#d62728", "#d62728"]
    bars = ax.bar(metrics_x, metrics_y, color=colors, alpha=0.7, edgecolor="black")
    for b, v in zip(bars, metrics_y, strict=False):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + (1 if v >= 0 else -1),
            f"{v:.1f}%",
            ha="center", va="bottom" if v >= 0 else "top",
            fontsize=9,
        )
    ax.set_ylabel("% of total |PnL|")
    ax.set_title("Trade-PnL concentration")
    ax.axhline(0, color="black", linewidth=0.5)
    ax.grid(True, alpha=0.2, axis="y")
    # Annotate concentration risk
    if metrics_y[1] > 50:  # top 3 wins > 50% of total |PnL|
        ax.text(
            0.5, 0.95, "⚠️ top-3 wins > 50% of total |PnL|",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=9, color="#b35900",
            bbox={"facecolor": "#fff8e1", "alpha": 0.9, "edgecolor": "#b35900"},
        )

    plt.tight_layout()
    plt.show()


def print_summary_stats(
    analyzer,
    num_positions: int | None = None,
    currency=USDC,
) -> None:
    """Print general and PnL stats from the analyzer.

    The Returns section (Sharpe, Sortino, Volatility, Returns Profit Factor,
    Average Return, Risk Return Ratio) is **deliberately omitted** because
    NT's ``_calculate_portfolio_returns`` zero-pads non-trading days via
    ``.ffill().pct_change()``, biasing all returns-derived stats for any
    strategy that doesn't trade every day.  See
    ``docs/ANALYZER_RETURNS_CAVEAT.md`` for the analysis and which stats
    will be reinstated when upstream lands the fix.

    Parameters
    ----------
    analyzer
        NT portfolio analyzer (after ``calculate_statistics`` has been called).
    num_positions
        If provided, printed at the end.
    currency
        Currency for PnL stats. Default ``USDC``.

    """
    general_stats = analyzer.get_performance_stats_general()
    pnl_stats = analyzer.get_performance_stats_pnls(currency)

    print("=== General ===")
    for k, v in general_stats.items():
        print(f"  {k}: {v}")

    print(f"\n=== PnL ({currency}) ===")
    for k, v in pnl_stats.items():
        print(f"  {k}: {v}")

    print(f"\nTotal PnL      : {analyzer.total_pnl(currency)}")
    print(f"Total PnL %    : {analyzer.total_pnl_percentage(currency)}")
    if num_positions is not None:
        print(f"Positions      : {num_positions}")
    print(
        "\nReturns-section stats (Sharpe, Sortino, Vol, Returns PF, "
        "Avg Return, Risk Return Ratio) are suppressed — see "
        "docs/ANALYZER_RETURNS_CAVEAT.md.",
    )


def plot_pnl_heatmap(
    results_df: pd.DataFrame,
    row_col: str,
    col_col: str,
    value_col: str = "total_pnl",
    *,
    row_label: str | None = None,
    col_label: str | None = None,
    title: str = "Total PnL (USDC)",
    fmt: str = ",.0f",
    flag_col: str | None = "liquidated",
    flag_value: str = "liquidated",
    flag_label: str = "hit zero equity",
    exclude_kinds: tuple[str, ...] | None = ("spotlight",),
    figsize: tuple[float, float] = (10, 7),
    cell_fontsize: int = 8,
    save_to: str | Path | None = None,
    show: bool = True,
) -> Any:
    """Diverging RdYlGn heatmap from sweep results DataFrame.

    Designed for inline notebook use — by default calls ``plt.show()``.
    Pass ``save_to=<path>`` to also persist the figure as a PNG (useful
    for batch runners and embedding into the sweep HTML).  Pass
    ``show=False`` for headless / scripted callers that don't want the
    inline display.

    Returns the ``matplotlib.figure.Figure`` for downstream callers
    (e.g. base64-encoding for embed); returns ``None`` only on the
    no-data early-return path.

    Parameters
    ----------
    results_df
        DataFrame with at least *row_col*, *col_col*, and *value_col*.
    row_col
        Column name for the y-axis (pivot index).
    col_col
        Column name for the x-axis (pivot columns).
    value_col
        Column to plot. Default ``"total_pnl"``.
    row_label
        Y-axis label. Defaults to *row_col*.
    col_label
        X-axis label. Defaults to *col_col*.
    title
        Chart title.
    fmt
        Format string for cell annotations.
    flag_col
        Column whose truthy/matching rows render as greyed-out + underlined
        cells (visually distinguishes wipeouts from small losses on the
        diverging colormap).  When the column is bool-dtype, True values
        flag.  When string-dtype, rows matching *flag_value* flag.  Set to
        ``None`` to disable flagging.  Default ``"liquidated"`` (the v2
        sweep schema's account-liquidation bool).  Use ``"error"`` for v1
        sweep parquets.
    flag_value
        The string value in *flag_col* that triggers the flag — only used
        when *flag_col* is a string-dtype column (bool columns ignore this).
        Default ``"liquidated"``.
    flag_label
        Footnote text for the flag legend. Default ``"hit zero equity"``.
    exclude_kinds
        Tuple of ``_kind`` values to drop before pivoting.  Default
        ``("spotlight",)`` — keeps the heatmap a clean regular grid even
        when off-grid spotlight combos are mixed into the sweep.  Pass
        an empty tuple to disable filtering and include everything.
    figsize
        Matplotlib figure size in inches.  Default ``(10, 7)`` — sized to
        keep cell annotations readable for grids up to 12×12 with
        comma-formatted values up to 6 digits.
    cell_fontsize
        Font size for the per-cell annotation text.  Default ``8``.
        Reduce further (e.g. 7) for grids with 7+ digit values.

    """
    df = results_df
    if exclude_kinds and "_kind" in df.columns:
        # Filter out off-grid combos so the pivot stays a regular grid.
        # NaN _kind values are kept (regular grid rows).
        df = df[~df["_kind"].isin(list(exclude_kinds))]

    pivot = df.pivot(index=row_col, columns=col_col, values=value_col)

    # Build a matching boolean pivot for flagged cells (using the
    # filtered df so flag_pivot lines up with the value pivot).
    # Handles both v2 bool columns (e.g. ``liquidated``) and v1
    # string columns (e.g. ``error == "liquidated"``) — for v2 the
    # column is itself the flag and ``flag_value`` is ignored.
    flag_pivot = None
    if flag_col and flag_col in df.columns:
        col_data = df[flag_col]
        if pd.api.types.is_bool_dtype(col_data):
            flagged = col_data.fillna(False).astype(float)
        else:
            flagged = (col_data.fillna("") == flag_value).astype(float)
        flag_pivot = df.assign(_flag=flagged).pivot(
            index=row_col, columns=col_col, values="_flag",
        )

    fig, ax = plt.subplots(figsize=figsize)

    vmax = max(abs(np.nanmin(pivot.values)), abs(np.nanmax(pivot.values)))
    im = ax.imshow(
        pivot.values,
        cmap="RdYlGn",
        aspect="auto",
        vmin=-vmax,
        vmax=vmax,
    )

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([str(c) for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([str(i) for i in pivot.index])
    ax.set_xlabel(col_label or col_col)
    ax.set_ylabel(row_label or row_col)
    ax.set_title(title)

    underline_drawn = False
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if np.isnan(val):
                continue

            is_flagged = (
                flag_pivot is not None
                and not np.isnan(flag_pivot.values[i, j])
                and flag_pivot.values[i, j] > 0.5
            )

            if is_flagged:
                # Grey out the cell so it's visually distinct from the heatmap
                ax.add_patch(Rectangle(
                    (j - 0.5, i - 0.5), 1, 1,
                    facecolor=_FLAG_BG, edgecolor="none", zorder=2,
                ))
                text_color = _FLAG_TEXT
            else:
                text_color = "white" if abs(val) > vmax * 0.6 else "black"

            ax.text(j, i, f"{val:{fmt}}", ha="center", va="center",
                    fontsize=cell_fontsize, color=text_color, zorder=3)

            if is_flagged:
                ax.plot(
                    [j - 0.22, j + 0.22], 
                    [i + 0.18, i + 0.18],
                    color=_FLAG_TEXT, # prev: color=color
                    linewidth=1, 
                    solid_capstyle="round",
                    zorder=3,
                )
                underline_drawn = True

    fig.colorbar(im, ax=ax, label=f"{value_col} (USDC)")

    plt.tight_layout()

    # Add footnote below everything (after tight_layout so we know final bounds)
    if underline_drawn:
        fig.text(
            0.5, 0.01, f"grey or underlined cells = {flag_label}",
            ha="center", fontsize=9, color="#666666", style="italic",
        )
        fig.subplots_adjust(bottom=fig.subplotpars.bottom + 0.04)

    if save_to is not None:
        out_path = Path(save_to)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=140, bbox_inches="tight")

    if show:
        plt.show()
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers for generate_backtest_html
# ─────────────────────────────────────────────────────────────────────────────

def _ts_to_unix_s(ts: Any) -> int | None:
    """Convert NT timestamp representations to Unix seconds integer."""
    if ts is None:
        return None
    if isinstance(ts, pd.Timestamp):
        return int(ts.timestamp())
    if isinstance(ts, (int, float)):
        if math.isnan(float(ts)):
            return None
        v = int(ts)
        # NT internal timestamps are nanoseconds (> 1e15 for post-2001 dates)
        if v > 10**15:
            return v // 1_000_000_000
        if v > 10**12:
            return v // 1_000_000
        return v
    try:
        return int(pd.Timestamp(ts).timestamp())
    except Exception:
        return None


def _bars_to_df(bars: list) -> pd.DataFrame:
    """Convert NT Bar list to DataFrame with unix-second time and OHLCV."""
    rows = [
        {
            "time":   _ts_to_unix_s(b.ts_event),
            "open":   float(b.open),
            "high":   float(b.high),
            "low":    float(b.low),
            "close":  float(b.close),
            "volume": float(b.volume),
        }
        for b in bars
    ]
    df = pd.DataFrame(rows).dropna(subset=["time"])
    df["time"] = df["time"].astype(int)
    return df.sort_values("time").reset_index(drop=True)


def _ma_series(close: pd.Series, period: int, ma_type: str = "EMA") -> pd.Series:
    """MA using pandas — EMA, SMA, HMA, DEMA, AMA (KAMA), or VIDYA."""
    if ma_type == "SMA":
        return close.rolling(window=period).mean()
    if ma_type == "HMA":
        half = max(period // 2, 1)
        sqrt_p = max(int(math.sqrt(period)), 1)

        def _wma(s: pd.Series, w: int) -> pd.Series:
            weights = list(range(1, w + 1))
            return s.rolling(window=w).apply(
                lambda x: np.average(x, weights=weights), raw=True,
            )

        return _wma(2 * _wma(close, half) - _wma(close, period), sqrt_p)
    if ma_type == "DEMA":
        import pandas_ta
        return pandas_ta.dema(close, length=period)
    if ma_type == "AMA":
        import pandas_ta
        return pandas_ta.kama(close, length=period, fast=2, slow=30)
    if ma_type == "VIDYA":
        import pandas_ta
        return pandas_ta.vidya(close, length=period)
    return close.ewm(span=period, adjust=False).mean()


def _parse_money_str(s: Any) -> float | None:
    """Parse NT money string '123.45 USDC' → 123.45. Returns None on failure."""
    if s is None or str(s) in ("None", "nan", "NaT", "<NA>"):
        return None
    try:
        return float(str(s).split()[0])
    except (ValueError, IndexError):
        return None


def _fills_to_markers(
    fills_df: pd.DataFrame,
    oid_to_trade_num: dict[str, int] | None = None,
    oid_to_close_cause: dict[str, str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """
    Convert fills_report → (tvlc_markers, marker_detail_by_key).

    tvlc_markers   passed to candleSeries.setMarkers() — TVLC accepts
                   multiple markers at the same time, so every fill
                   gets its own marker.
    marker_detail  keyed by ``"<unix_s>:<oid>"`` so simultaneous fills
                   (NETTING reversals where the close+open of two
                   trades land on the same bar timestamp) keep their
                   own tooltip data instead of one overwriting the
                   other.  The JS tooltip walks the keys for the
                   hovered timestamp and renders all matching fills.

    Parameters
    ----------
    fills_df
        DataFrame from ``engine.trader.generate_order_fills_report()``.
        Must include ``client_order_id`` so each fill can be linked
        back to its owning order (for trade-number + close-cause).
    oid_to_trade_num
        Per-fill mapping from client-order-id → 1-based trade number.
        Built upstream by traversing ``classify_position_exits``
        rows and assigning each row's ``opening_order_id`` /
        ``closing_order_id`` to its trade index.
    oid_to_close_cause
        Per-fill mapping from client-order-id → close cause
        (``"protective_stop"`` | ``"liquidation"``).  Built upstream
        from ``classify_position_exits``'s ``closing_order_id`` column,
        skipping ``"strategy_exit"`` (the default).

    Why per-order-id, not per-timestamp
    -----------------------------------
    With NETTING reversals on bar-aligned data (e.g. daily bars), the
    close-fill of trade N and the open-fill of trade N+1 share the
    same ``ts_event``.  A timestamp-keyed lookup collides: we'd lose
    one trade's metadata to the other, then the dedup step drops one
    of the two markers entirely.  Each fill has its own
    ``client_order_id`` though — that's a stable, collision-free key.
    """
    if fills_df is None or fills_df.empty:
        return [], {}

    tvlc_markers: list[dict] = []
    detail: dict[str, dict] = {}

    # Marker visual config per close cause (keys must match
    # classify_position_exits output values).  Forced exits override the
    # regular BUY/SELL arrow regardless of fill side — a SHORT-position
    # protective stop fires a BUY order, and we still want the STOP
    # visual on it.
    cause_marker = {
        "protective_stop": ("#ff8a65", "circle"),
        "liquidation":     ("#ff1744", "square"),
    }

    # NT's generate_order_fills_report() puts client_order_id on the
    # DataFrame *index*, not in a column.  Allow both shapes: prefer
    # the index name, fall back to a column for callers passing a
    # custom shape.
    oid_is_index = fills_df.index.name == "client_order_id"

    for idx, row in fills_df.iterrows():
        ts_s = _ts_to_unix_s(row.get("ts_last") or row.get("ts_init"))
        if ts_s is None:
            continue

        if oid_is_index:
            oid = idx
        else:
            oid = row.get("client_order_id")
        oid_key = str(oid) if oid is not None else ""

        side_raw = str(row.get("side", "")).upper()
        is_buy = "BUY" in side_raw

        qty_raw = row.get("filled_qty", row.get("quantity", "?"))
        px_raw  = row.get("avg_px", "?")

        try:
            px_fmt = f"{float(px_raw):,.2f}"
        except (ValueError, TypeError):
            px_fmt = str(px_raw)

        qty_str = str(qty_raw).rstrip("0").rstrip(".")  # "0.01000000" → "0.01"

        trade_num: int | None = None
        if oid_to_trade_num is not None and oid_key:
            trade_num = oid_to_trade_num.get(oid_key)

        cause = None
        if oid_to_close_cause is not None and oid_key:
            cause = oid_to_close_cause.get(oid_key)

        # Default visuals (regular BUY/SELL).
        marker_color = "#26a69a" if is_buy else "#ef5350"
        marker_shape = "arrowUp" if is_buy else "arrowDown"
        # Drive the visual from cause, not fill side: a SHORT-position
        # stop is a BUY closing fill but should still show the STOP shape.
        if cause and cause in cause_marker:
            marker_color, marker_shape = cause_marker[cause]

        # Marker label is just the trade number; cause is conveyed by the
        # marker shape + color + the legend, not redundant inline text.
        # Falls back to "B qty" / "S qty" only when there's no trade
        # number to show.
        if trade_num is not None:
            marker_text = f"#{trade_num}"
        else:
            marker_text = f"{'B' if is_buy else 'S'} {qty_str}"

        # Position the cause-marker on the *opposite* side of the bar
        # from the regular BUY/SELL arrow:
        #   - LONG-position stop (SELL fill at adverse low) → belowBar,
        #     so it sits where the price actually went, not floating
        #     above the candle alongside a non-existent SELL signal.
        #   - SHORT-position stop (BUY fill at adverse high) → aboveBar.
        # Without this, every stop is visually indistinguishable from a
        # signal entry on the same side.
        if cause:
            position = "aboveBar" if is_buy else "belowBar"
        else:
            position = "belowBar" if is_buy else "aboveBar"

        tvlc_markers.append({
            "time":     ts_s,
            "position": position,
            "color":    marker_color,
            "shape":    marker_shape,
            "text":     marker_text,
            "size":     1.5,
        })

        # Detail key includes the fill's order id so simultaneous fills
        # don't clobber each other.  The JS tooltip groups keys by
        # leading timestamp prefix.
        detail_key = f"{ts_s}:{oid_key}" if oid_key else f"{ts_s}:{len(detail)}"
        detail[detail_key] = {
            "ts":          ts_s,
            "is_buy":      is_buy,
            "side":        "BUY" if is_buy else "SELL",
            "qty":         qty_str,
            "px":          px_fmt,
            "trade_num":   trade_num,
            "close_cause": cause,
        }

    # TVLC requires markers sorted by time.  We do NOT dedup — every
    # fill is its own real event and deserves its own marker; with
    # NETTING reversals you legitimately have two fills at the same
    # bar timestamp (close of trade N + open of trade N+1).
    tvlc_markers.sort(key=lambda m: m["time"])

    return tvlc_markers, detail


def _fmt_px(v: Any) -> str:
    """Format a price value for display, returning '—' for missing/NaN."""
    if v is None:
        return "—"
    try:
        fv = float(v)
        return f"{fv:,.2f}" if not math.isnan(fv) else "—"
    except (ValueError, TypeError):
        return "—"


def _positions_to_rows(
    positions_df: pd.DataFrame,
    pos_id_to_close_cause: dict[str, str] | None = None,
) -> list[dict]:
    """Convert positions_report → list of plain dicts for the HTML trade table.

    ``pos_id_to_close_cause`` (when supplied) is keyed by ``position_id``
    so each row picks up the correct cause regardless of whether two
    positions share a closing-bar timestamp.  Falls back to
    ``"strategy_exit"`` for any row not in the map.
    """
    if positions_df is None or positions_df.empty:
        return []

    rows: list[dict] = []

    # NT's generate_positions_report() puts position_id on the
    # DataFrame *index*, not in a column.  Allow both shapes: prefer
    # the index when it's named "position_id"; fall back to a column
    # for callers passing a custom-shaped DataFrame.
    pos_id_is_index = positions_df.index.name == "position_id"

    for idx, row in positions_df.iterrows():
        ts_opened = row.get("ts_opened")
        ts_closed = row.get("ts_closed")

        opened_s   = _ts_to_unix_s(ts_opened)
        closed_s   = _ts_to_unix_s(ts_closed)

        opened_str = str(ts_opened)[:19].replace("T", " ") if opened_s else "—"
        closed_str = str(ts_closed)[:19].replace("T", " ") if closed_s else "Open"

        # entry (BUY/SELL) is the directional signal; side is FLAT once closed
        entry = str(row.get("entry", "")).upper()
        side_label = "Long" if entry == "BUY" else "Short" if entry == "SELL" else "?"

        # Quantities — peak_qty is the filled size; quantity is 0 after close
        qty_raw = row.get("peak_qty", row.get("quantity", "?"))
        qty_str = str(qty_raw).rstrip("0").rstrip(".")

        # Prices
        entry_px = _fmt_px(row.get("avg_px_open"))
        exit_px  = _fmt_px(row.get("avg_px_close"))

        # PnL — stored as string "value CURRENCY" in positions_report
        pnl = _parse_money_str(row.get("realized_pnl"))

        # Return — stored as fraction (0.05 = 5 %)
        try:
            ret_frac = float(row.get("realized_return", 0) or 0)
        except (ValueError, TypeError):
            ret_frac = 0.0

        # Look up close_cause by position_id, not timestamp — two
        # positions can share a closing-bar ts (NETTING reversal) and
        # the timestamp lookup would silently grab the wrong row's cause.
        pos_id = idx if pos_id_is_index else row.get("position_id")
        pos_key = str(pos_id) if pos_id is not None else None
        close_cause = (
            pos_id_to_close_cause.get(pos_key)
            if (pos_id_to_close_cause is not None and pos_key)
            else None
        ) or "strategy_exit"

        rows.append({
            "opened":     opened_str,
            "closed":     closed_str,
            "opened_ts_s": opened_s or 0,
            "side":       side_label,
            "qty":        qty_str,
            "entry_px":   entry_px,
            "exit_px":    exit_px,
            "pnl":        pnl,
            "realized_return": ret_frac,
            "close_cause": close_cause,
        })

    return rows


def _compute_stats(position_rows: list[dict], starting_capital: float) -> dict:
    """Derive summary statistics from position rows."""
    pnls = [r["pnl"] for r in position_rows if r["pnl"] is not None]
    if not pnls:
        return {}

    total_pnl   = sum(pnls)
    winners     = [p for p in pnls if p > 0]
    losers      = [p for p in pnls if p < 0]
    gross_win   = sum(winners)
    gross_loss  = abs(sum(losers))

    return {
        "total_pnl":      round(total_pnl, 4),
        "total_pnl_pct":  round(total_pnl / starting_capital * 100, 4) if starting_capital else 0,
        "num_trades":     len(pnls),
        "win_rate":       round(len(winners) / len(pnls) * 100, 2),
        "avg_win":        round(gross_win  / len(winners), 4) if winners else 0,
        "avg_loss":       round(-gross_loss / len(losers), 4) if losers else 0,
        "profit_factor":  round(gross_win / gross_loss, 4) if gross_loss else None,
        "total_wins":     len(winners),
        "total_losses":   len(losers),
    }


def _compute_close_cause_counts(position_rows: list[dict]) -> dict[str, dict]:
    """Aggregate per-cause trade counts and PnL for the close-cause panel.

    Returns a dict keyed by cause (``strategy_exit`` / ``protective_stop`` /
    ``liquidation``) with ``count`` and ``pnl`` per bucket — empty when no
    rows carry a ``close_cause`` field.
    """
    out: dict[str, dict] = {}
    for r in position_rows:
        cause = r.get("close_cause")
        if not cause:
            continue
        bucket = out.setdefault(cause, {"count": 0, "pnl": 0.0})
        bucket["count"] += 1
        if r.get("pnl") is not None:
            bucket["pnl"] += float(r["pnl"])
    # Round PnL after summation to keep JSON small and stable.
    for v in out.values():
        v["pnl"] = round(v["pnl"], 4)
    return out


def _summarise_account_liq(account_liq_event: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce ``find_account_liq_culprit`` output to JSON-safe primitives.

    Only the bits needed by the HTML banner are kept — the full
    ``culprit_positions`` list (NT Position objects) is not JSON-encodable.
    """
    if not account_liq_event:
        return {}
    return {
        "liq_ts":         account_liq_event.get("liq_ts"),
        "liq_ts_iso":     account_liq_event.get("liq_ts_iso"),
        "equity_at_liq":  _decimal_to_float(account_liq_event.get("equity_at_liq")),
        "equity_before":  _decimal_to_float(account_liq_event.get("equity_before")),
        "drain_amount":   _decimal_to_float(account_liq_event.get("drain_amount")),
        "culprit_count":  len(account_liq_event.get("culprit_position_ids", []) or []),
    }


def _decimal_to_float(v: Any) -> float | None:
    """Best-effort coerce Decimal/None/numeric → float for JSON serialisation."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Analysis tool charts ──────────────────────────────────────────────────────


def plot_walkforward_oos_equity(
    wf_results: pd.DataFrame,
    *,
    title: str = "Walk-forward stitched OOS equity",
    currency: str = "USDC",
) -> None:
    """Stitched cumulative OOS PnL line across walk-forward folds.

    The canonical "what if I'd actually traded the WF picks live"
    chart.  Plots a single piecewise-linear line over calendar time:
    each fold contributes a segment from ``(test_start, prior_cum)``
    to ``(test_end, prior_cum + oos_pnl)``, so the slope of each
    segment shows that fold's per-day OOS rate of return.

    Steeper slope = better fold; flat = breakeven; downward = lossy
    fold.  Fold boundaries are marked with vertical guides annotated
    with the fold's chosen params, so it's instantly visible whether
    drift in the picks coincided with degraded OOS performance.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    wf_results
        DataFrame from :func:`src.backtesting.engine.run_walk_forward`.
        Required columns: ``fold``, ``test_start``, ``test_end``,
        ``oos_pnl``.  Optional: any ``best_*`` columns are used to
        annotate fold boundaries.
    title
        Chart title.
    currency
        Currency label for the y-axis.

    """
    if wf_results.empty:
        print("No walk-forward results to plot.")
        return

    required = {"fold", "test_start", "test_end", "oos_pnl"}
    missing = required - set(wf_results.columns)
    if missing:
        print(f"Missing required columns: {missing}")
        return

    import matplotlib.dates as mdates

    # Sort folds by test_start so the cumulative chain is monotonic
    # in calendar time even if the input rows aren't ordered.
    folds = wf_results.sort_values("test_start").reset_index(drop=True)
    test_starts = pd.to_datetime(folds["test_start"], utc=True)
    test_ends   = pd.to_datetime(folds["test_end"],   utc=True)
    oos_pnls = folds["oos_pnl"].astype(float).to_numpy()
    cum_after = np.cumsum(oos_pnls)            # cumulative AFTER each fold
    cum_before = np.concatenate([[0.0], cum_after[:-1]])  # cumulative BEFORE

    # Best-* columns for annotations
    param_cols = [c for c in folds.columns if c.startswith("best_")]

    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    # Stitched line: alternate (test_start, prior_cum) → (test_end, new_cum)
    xs: list[Any] = []
    ys: list[float] = []
    for ts, te, c0, c1 in zip(test_starts, test_ends, cum_before, cum_after, strict=True):
        xs.extend([ts, te])
        ys.extend([c0, c1])
    ax.plot(
        xs, ys,
        color="#26a69a", linewidth=2.0,
        label="Cumulative OOS PnL",
    )
    # Markers at fold boundaries
    ax.scatter(
        test_ends, cum_after,
        color="#26a69a", s=40, zorder=3, edgecolor=_BORDER, linewidth=0.5,
    )

    # Zero-PnL guide
    ax.axhline(0, color="white", linewidth=0.5, alpha=0.3)

    # Vertical fold-boundary guides at test_end of every fold except the last
    # (so the rightmost edge isn't crowded).  Annotated with chosen params.
    y_top = max(cum_after.max(), 0.0)
    y_bot = min(cum_after.min(), 0.0)
    y_range = max(y_top - y_bot, 1e-9)
    for i, (te, fold_id, c1) in enumerate(zip(
        test_ends, folds["fold"], cum_after, strict=True,
    )):
        ax.axvline(te, color=_GRID, linewidth=0.6, alpha=0.4, linestyle="--")
        # Annotate with picked params + fold OOS PnL
        if param_cols:
            param_label = ", ".join(
                f"{c.removeprefix('best_')}={folds.iloc[i][c]}"
                for c in param_cols
            )
            anno = f"#{int(fold_id)}: {param_label}\n+{oos_pnls[i]:,.0f}" \
                if oos_pnls[i] >= 0 \
                else f"#{int(fold_id)}: {param_label}\n{oos_pnls[i]:,.0f}"
        else:
            anno = f"#{int(fold_id)}\n{oos_pnls[i]:+,.0f}"
        ax.annotate(
            anno,
            xy=(te, c1),
            xytext=(0, 8 if c1 >= 0 else -22),
            textcoords="offset points",
            ha="right", va="bottom" if c1 >= 0 else "top",
            fontsize=8, color=_TEXT, alpha=0.85,
        )

    # Headline number — final cumulative OOS PnL
    final_pnl = cum_after[-1]
    headline_color = "#26a69a" if final_pnl >= 0 else "#ef5350"
    ax.text(
        0.02, 0.95,
        f"Total OOS: {final_pnl:+,.0f} {currency}",
        transform=ax.transAxes, ha="left", va="top",
        fontsize=11, color=headline_color, weight="bold",
        bbox={"facecolor": _BG, "alpha": 0.85, "edgecolor": headline_color},
    )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.set_ylabel(f"Cumulative OOS PnL ({currency})", color=_TEXT)
    ax.set_title(title, color=_TEXT, fontsize=13)
    ax.tick_params(colors=_TEXT)
    for spine in ax.spines.values():
        spine.set_color(_BORDER)
    ax.grid(axis="y", color=_GRID, alpha=0.3)
    ax.legend(
        loc="lower right", facecolor=_BG, edgecolor=_BORDER, labelcolor=_TEXT,
    )

    plt.tight_layout()
    plt.show()


def plot_rolling_pnl(
    rolling_df: pd.DataFrame,
    *,
    title: str = "Rolling Performance",
    currency: str = "USDC",
) -> None:
    """Bar chart of per-window PnL from rolling_performance().

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    rolling_df
        DataFrame from ``rolling_performance()`` with columns
        ``window_start``, ``pnl``, ``num_positions``.
    title
        Chart title.
    currency
        Currency label for the y-axis.

    """
    if rolling_df.empty:
        print("No rolling data to plot.")
        return

    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    dates = rolling_df["window_start"]
    pnls = rolling_df["pnl"]
    colors = [_GREEN if p > 0 else _RED for p in pnls]

    ax.bar(dates, pnls, width=20, color=colors, edgecolor="none", alpha=0.85)
    ax.axhline(0, color="white", linewidth=0.5, alpha=0.3)

    # Position count annotations above each bar
    for dt, pnl_val, n in zip(dates, pnls, rolling_df["num_positions"]):
        if n > 0:
            offset = max(abs(pnls.max()), abs(pnls.min())) * 0.03
            y = pnl_val + offset if pnl_val >= 0 else pnl_val - offset
            ax.text(
                dt, y, str(int(n)),
                ha="center", va="bottom" if pnl_val >= 0 else "top",
                fontsize=7, color=_TEXT, alpha=0.6,
            )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.set_ylabel(f"PnL ({currency})", color=_TEXT)
    ax.set_title(title, color=_TEXT, fontsize=13)
    ax.tick_params(colors=_TEXT)
    for spine in ax.spines.values():
        spine.set_color(_BORDER)
    ax.grid(axis="y", color=_GRID, alpha=0.3)

    plt.tight_layout()
    plt.show()


def plot_fee_sensitivity(
    fee_df: pd.DataFrame,
    *,
    title: str = "Fee Sensitivity",
    currency: str = "USDC",
) -> None:
    """Line + scatter chart of PnL vs fee level from run_fee_sweep().

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    fee_df
        DataFrame from ``run_fee_sweep()`` with columns
        ``fee_bps``, ``total_pnl``, ``breakeven``.
    title
        Chart title.
    currency
        Currency label for the y-axis.

    """
    if fee_df.empty:
        print("No fee sweep data to plot.")
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    bps = fee_df["fee_bps"]
    pnl = fee_df["total_pnl"]
    be = fee_df["breakeven"]

    # Line
    ax.plot(bps, pnl, color=_BLUE, linewidth=2, alpha=0.8, zorder=2)

    # Scatter — green if breakeven, red if not
    colors = [_GREEN if b else _RED for b in be]
    ax.scatter(bps, pnl, c=colors, s=60, zorder=3, edgecolors="white", linewidths=0.5)

    # Zero line
    ax.axhline(0, color="white", linewidth=0.5, alpha=0.3)

    # Exchange reference lines
    ax.axvline(3.5, color=_AMBER, linewidth=1, linestyle="--", alpha=0.5)
    ax.text(3.5, pnl.max() * 0.95, " HL taker", color=_AMBER, fontsize=8, va="top")
    ax.axvline(5.0, color=_TEXT, linewidth=1, linestyle="--", alpha=0.4)
    ax.text(5.0, pnl.max() * 0.95, " Binance taker", color=_TEXT, fontsize=8, va="top", alpha=0.7)

    ax.set_xlabel("Fee (basis points)", color=_TEXT)
    ax.set_ylabel(f"Total PnL ({currency})", color=_TEXT)
    ax.set_title(title, color=_TEXT, fontsize=13)
    ax.tick_params(colors=_TEXT)
    for spine in ax.spines.values():
        spine.set_color(_BORDER)
    ax.grid(axis="y", color=_GRID, alpha=0.3)

    plt.tight_layout()
    plt.show()


def plot_regime_overlay(
    regime_df: pd.DataFrame,
    *,
    title: str = "Market Regimes",
) -> None:
    """Price chart with colored background spans for each market regime.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    regime_df
        DataFrame from ``tag_regimes()`` with a DatetimeIndex and
        columns ``close``, ``regime``.
    title
        Chart title.

    """
    if regime_df.empty:
        print("No regime data to plot.")
        return

    # Regime → (color, alpha)
    _REGIME_COLORS: dict[str, tuple[str, float]] = {
        "TRENDING":      (_BLUE, 0.15),
        "RANGING":       (_AMBER,   0.15),
        "TRANSITIONAL":  ("#888888", 0.08),
        "HIGH_VOL":      (_RED, 0.15),
        "LOW_VOL":       (_GREEN,  0.12),
    }

    fig, ax = plt.subplots(figsize=(16, 6))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    # Price line
    ax.plot(regime_df.index, regime_df["close"], color=_TEXT, linewidth=0.8, alpha=0.9)

    # Group consecutive bars with the same regime into spans
    regimes = regime_df["regime"]
    prev_regime = None
    span_start = None
    legend_drawn: set[str] = set()

    for ts, regime_val in regimes.items():
        if regime_val != prev_regime:
            # Close previous span
            if prev_regime is not None and prev_regime in _REGIME_COLORS and span_start is not None:
                color, alpha = _REGIME_COLORS[prev_regime]
                label = prev_regime if prev_regime not in legend_drawn else None
                ax.axvspan(span_start, ts, color=color, alpha=alpha, label=label, linewidth=0)
                if label:
                    legend_drawn.add(prev_regime)
            span_start = ts
            prev_regime = regime_val

    # Close final span
    if prev_regime is not None and prev_regime in _REGIME_COLORS and span_start is not None:
        color, alpha = _REGIME_COLORS[prev_regime]
        label = prev_regime if prev_regime not in legend_drawn else None
        ax.axvspan(span_start, regime_df.index[-1], color=color, alpha=alpha, label=label, linewidth=0)

    ax.set_ylabel("Close", color=_TEXT)
    ax.set_title(title, color=_TEXT, fontsize=13)
    ax.tick_params(colors=_TEXT)
    for spine in ax.spines.values():
        spine.set_color(_BORDER)
    ax.legend(
        facecolor="#1e222d", edgecolor=_BORDER, labelcolor=_TEXT,
        loc="upper left", fontsize=9,
    )

    plt.tight_layout()
    plt.show()


# ─────────────────────────────────────────────────────────────────────────────
# Public: self-contained TVLC HTML
# ─────────────────────────────────────────────────────────────────────────────

def generate_backtest_html(
    bars: list,
    fills_report: pd.DataFrame,
    positions_report: pd.DataFrame,
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    ma_type: str = "EMA",
    overlay_type: str = "ma",
    instrument_label: str = "",
    bar_label: str = "1h",
    starting_capital: float = 10_000.0,
    result_filename: str | None = None,
    open_browser: bool = False,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> Path:
    """
    Generate a self-contained HTML backtest report using TradingView Lightweight Charts.

    The output file requires only a browser — no server, no dependencies to install.

    Parameters
    ----------
    bars              NT Bar list from ParquetDataCatalog.
    fills_report      engine.trader.generate_order_fills_report()
    positions_report  engine.trader.generate_positions_report()
    fast_period       Fast MA period. When overlay_type="donchian", used as
                      entry channel period.
    slow_period       Slow MA period. When overlay_type="donchian", used as
                      exit channel period.
    ma_type           "EMA" or "SMA". Ignored when overlay_type="donchian".
    overlay_type      "ma" (default) for EMA/SMA lines, or "donchian" for
                      dual Donchian Channel bands (entry + exit).
    instrument_label  Display label (e.g. "BTC-USD-PERP.HYPERLIQUID").
    bar_label         Timeframe string (e.g. "1h").
    starting_capital  Used for total-return % calculation.
    result_filename   Descriptive name without path or extension
                      (e.g. "sma_cross_BTC_15_25_4h"). If None, auto-generates
                      from instrument_label + timestamp.

    Returns
    -------
    Path  — absolute path to the generated HTML file.
    """
    # ── Prepare data ─────────────────────────────────────────────────────────
    df = _bars_to_df(bars)
    if df.empty:
        raise ValueError("bars is empty — nothing to plot.")

    ohlcv_json = json.dumps(
        df[["time", "open", "high", "low", "close"]].to_dict(orient="records")
    )

    if overlay_type == "donchian":
        dc_df = _bars_to_donchian_df(bars, entry_period=fast_period, exit_period=slow_period)

        def _dc_series(col: str) -> list[dict]:
            return [
                {"time": int(t), "value": round(v, 6)}
                for t, v in zip(df["time"], dc_df[col].values)
                if not math.isnan(v)
            ]

        overlay_lines = [
            {"label": f"Entry Upper({fast_period})", "color": "#2196f3", "width": 1, "style": 0, "data": _dc_series("dc_entry_upper")},
            {"label": f"Entry Lower({fast_period})", "color": "#2196f3", "width": 1, "style": 0, "data": _dc_series("dc_entry_lower")},
            {"label": f"Exit Upper({slow_period})",  "color": "#ff9800", "width": 1, "style": 2, "data": _dc_series("dc_exit_upper")},
            {"label": f"Exit Lower({slow_period})",  "color": "#ff9800", "width": 1, "style": 2, "data": _dc_series("dc_exit_lower")},
        ]
    else:
        fast_ma_data = [
            {"time": int(t), "value": round(v, 6)}
            for t, v in zip(df["time"], _ma_series(df["close"], fast_period, ma_type))
            if not math.isnan(v)
        ]
        slow_ma_data = [
            {"time": int(t), "value": round(v, 6)}
            for t, v in zip(df["time"], _ma_series(df["close"], slow_period, ma_type))
            if not math.isnan(v)
        ]
        overlay_lines = [
            {"label": f"{ma_type}{fast_period}", "color": "#2196f3", "width": 1, "style": 0, "data": fast_ma_data},
            {"label": f"{ma_type}{slow_period}", "color": "#ff9800", "width": 1, "style": 0, "data": slow_ma_data},
        ]

    # ── Per-fill metadata maps, keyed by client_order_id ─────────────
    # Each fill carries its own client_order_id, and that id uniquely
    # identifies which order (and therefore which trade leg) it belongs
    # to.  Earlier versions of this code keyed on timestamp; that
    # collided in NETTING mode where the closing fill of trade N and
    # the opening fill of trade N+1 share the same bar timestamp, so
    # one trade's metadata silently overwrote the other's.
    #
    # Both lookups are derived from the v2 ``exit_classification``
    # DataFrame (which exposes opening_order_id + closing_order_id +
    # close_cause per closed position).  When the helper isn't
    # supplied (legacy callers), the maps stay empty and the markers
    # fall back to "B qty" / "S qty" labels.
    oid_to_trade_num: dict[str, int] = {}
    oid_to_close_cause: dict[str, str] = {}
    pos_id_to_close_cause: dict[str, str] = {}
    if exit_classification is not None and not exit_classification.empty:
        for trade_num, (_, ec_row) in enumerate(
            exit_classification.iterrows(), start=1,
        ):
            open_oid  = str(ec_row.get("opening_order_id") or "")
            close_oid = str(ec_row.get("closing_order_id") or "")
            cause     = str(ec_row.get("close_cause") or "")
            pos_id    = str(ec_row.get("position_id") or "")
            if open_oid:
                oid_to_trade_num[open_oid] = trade_num
            if close_oid:
                oid_to_trade_num[close_oid] = trade_num
            if close_oid and cause and cause != "strategy_exit":
                oid_to_close_cause[close_oid] = cause
            if pos_id and cause:
                pos_id_to_close_cause[pos_id] = cause

    markers, marker_detail = _fills_to_markers(
        fills_report,
        oid_to_trade_num or None,
        oid_to_close_cause or None,
    )
    position_rows          = _positions_to_rows(
        positions_report,
        pos_id_to_close_cause or None,
    )
    stats                  = _compute_stats(position_rows, starting_capital)
    cause_counts           = _compute_close_cause_counts(position_rows)
    account_liq_summary    = _summarise_account_liq(account_liq_event)

    # ── Resolve output path ──────────────────────────────────────────────────
    if result_filename is None:
        asset = instrument_label.split("-")[0] if instrument_label else "unknown"
        result_filename = f"backtest_{asset}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = (_REPORTS_DIR / f"{result_filename}_{timestamp}.html").resolve()

    # ── Render template ──────────────────────────────────────────────────────
    if overlay_type == "donchian":
        title = f"Backtest — {instrument_label} {bar_label}  DC({fast_period}/{slow_period})"
    else:
        title = f"Backtest — {instrument_label} {bar_label}  {ma_type} {fast_period}/{slow_period}"
    subtitle = (
        f"{len(df):,} bars"
        + (f"  ·  {stats.get('num_trades', 0)} trades" if stats else "")
        + (f"  ·  capital {starting_capital:,.0f} USDC" if starting_capital else "")
    )

    # Build legend HTML from overlay lines
    legend_parts = []
    for line in overlay_lines:
        if line.get("style", 0) == 2:
            style = f"background:transparent; border-top:2px dashed {line['color']}; height:0"
        else:
            style = f"background:{line['color']}"
        legend_parts.append(
            f'<div class="legend-item">'
            f'<div class="legend-line" style="{style}"></div>'
            f'<span>{line["label"]}</span>'
            f'</div>'
        )
    overlay_legend_html = "\n  ".join(legend_parts)

    html = _HTML_TEMPLATE.replace("__TITLE__",              title)
    html = html.replace("__SUBTITLE__",                     subtitle)
    html = html.replace("__OVERLAY_LEGEND_HTML__",          overlay_legend_html)
    html = html.replace("__OVERLAY_LINES_JSON__",           json.dumps(overlay_lines))
    html = html.replace("__OHLCV_JSON__",                   ohlcv_json)
    html = html.replace("__MARKERS_JSON__",                 json.dumps(markers))
    html = html.replace("__MARKER_DETAIL_JSON__",           json.dumps(marker_detail))
    html = html.replace("__TRADES_JSON__",                  json.dumps(position_rows))
    html = html.replace("__STATS_JSON__",                   json.dumps(stats))
    html = html.replace("__STARTING_CAPITAL__",             str(starting_capital))
    html = html.replace("__CAUSE_COUNTS_JSON__",            json.dumps(cause_counts))
    html = html.replace("__ACCOUNT_LIQ_JSON__",             json.dumps(account_liq_summary))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"✓ Backtest HTML written → {output_path}")
    if open_browser:
        webbrowser.open(output_path.as_uri())
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# HTML template
# ─────────────────────────────────────────────────────────────────────────────

_HTML_TEMPLATE = textwrap.dedent("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: #131722;
  color: #d1d4dc;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 13px;
  line-height: 1.4;
}

/* ── Header ─────────────────────────────────────────────────────────────── */
header {
  padding: 14px 20px 12px;
  border-bottom: 1px solid #2a2e39;
  display: flex;
  align-items: baseline;
  gap: 16px;
  background: #1a1e2d;
}
header h1 { font-size: 15px; font-weight: 600; color: #e1e4ec; }
header .subtitle { font-size: 12px; color: #787b86; }

/* ── Legend ──────────────────────────────────────────────────────────────── */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  padding: 8px 16px;
  border-bottom: 1px solid #2a2e39;
  background: #131722;
}
.legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #787b86; }
.legend-line { width: 14px; height: 2px; border-radius: 1px; flex-shrink: 0; }
.legend-arrow-up {
  width: 0; height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-bottom: 8px solid #26a69a;
  flex-shrink: 0;
}
.legend-arrow-down {
  width: 0; height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 8px solid #ef5350;
  flex-shrink: 0;
}

/* ── Chart container ─────────────────────────────────────────────────────── */
#chart-container {
  position: relative;
  width: 100%;
  height: 520px;
  border-bottom: 1px solid #2a2e39;
}
#chart { width: 100%; height: 100%; }

/* ── Hover tooltip ───────────────────────────────────────────────────────── */
#tooltip {
  position: absolute;
  display: none;
  background: #1e222d;
  border: 1px solid #363a45;
  border-radius: 4px;
  padding: 9px 12px;
  font-size: 12px;
  pointer-events: none;
  z-index: 20;
  min-width: 155px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
#tooltip .tt-time  { color: #787b86; font-size: 11px; margin-bottom: 5px; }
#tooltip .tt-row   { display: flex; justify-content: space-between; gap: 14px; line-height: 1.7; }
#tooltip .tt-label { color: #787b86; }
#tooltip .tt-value { color: #d1d4dc; font-variant-numeric: tabular-nums; }
#tooltip .tt-buy   { color: #26a69a; font-weight: 600; }
#tooltip .tt-sell  { color: #ef5350; font-weight: 600; }
#tooltip .tt-sep   { border: none; border-top: 1px solid #2a2e39; margin: 5px 0; }

/* ── Stats bar ───────────────────────────────────────────────────────────── */
.stats-bar {
  display: flex;
  flex-wrap: wrap;
  border-bottom: 1px solid #2a2e39;
  background: #181c2a;
}
.stat-cell {
  flex: 1;
  min-width: 110px;
  padding: 11px 14px;
  border-right: 1px solid #2a2e39;
  text-align: center;
}
.stat-cell:last-child { border-right: none; }
.stat-label {
  font-size: 10px;
  color: #787b86;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 4px;
}
.stat-value { font-size: 15px; font-weight: 600; color: #e1e4ec; }
.stat-value.pos { color: #26a69a; }
.stat-value.neg { color: #ef5350; }

/* ── Trades section ──────────────────────────────────────────────────────── */
.trades-header {
  padding: 10px 16px 9px;
  font-size: 11px;
  font-weight: 600;
  color: #787b86;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  border-bottom: 1px solid #2a2e39;
  background: #181c2a;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.filter-bar { display: flex; gap: 4px; }
.filter-btn {
  background: transparent;
  border: 1px solid #2a2e39;
  color: #787b86;
  padding: 3px 10px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  transition: all 0.1s;
}
.filter-btn:hover { border-color: #363a45; color: #b2b5be; }
.filter-btn.active { background: #2a2e39; color: #e1e4ec; border-color: #363a45; }
.trades-wrap { overflow-x: auto; max-height: 420px; overflow-y: auto; }

table.trades {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
table.trades thead th {
  padding: 8px 12px;
  text-align: left;
  color: #787b86;
  font-weight: 500;
  border-bottom: 1px solid #2a2e39;
  white-space: nowrap;
  position: sticky;
  top: 0;
  background: #131722;
  z-index: 1;
}
table.trades thead th.r { text-align: right; }
table.trades thead th.sortable { cursor: pointer; user-select: none; }
table.trades thead th.sortable:hover { color: #b2b5be; }
.sort-arrow { font-size: 10px; margin-left: 3px; opacity: 0.5; }
table.trades thead th.sortable.asc .sort-arrow,
table.trades thead th.sortable.desc .sort-arrow { opacity: 1; color: #2196f3; }
table.trades tbody tr {
  border-bottom: 1px solid #1a1e2a;
  cursor: pointer;
  transition: background 0.08s;
}
table.trades tbody tr:hover  { background: #1a1e2e; }
table.trades tbody tr.active { background: #1e2b3a; outline: 1px solid #2196f340; }
table.trades td {
  padding: 7px 12px;
  white-space: nowrap;
  color: #b2b5be;
}
table.trades td.r  { text-align: right; font-variant-numeric: tabular-nums; }
table.trades td.id { color: #787b86; font-size: 11px; }
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
}
.badge.long  { background: rgba(38,166,154,0.12); color: #26a69a; }
.badge.short { background: rgba(239,83,80,0.12);  color: #ef5350; }
.badge.cause-strategy_exit  { background: rgba(120,123,134,0.18); color: #b2b5be; }
.badge.cause-protective_stop { background: rgba(255,138,101,0.20); color: #ff8a65; }
.badge.cause-liquidation     { background: rgba(255,23,68,0.22);  color: #ff8a8a; }
td.pnl.pos { color: #26a69a; }
td.pnl.neg { color: #ef5350; }
.no-trades { padding: 28px; text-align: center; color: #787b86; }

/* ── Account-liquidation banner ──────────────────────────────────────────── */
.account-liq-banner {
  display: none;  /* JS shows when account_liq summary is non-empty */
  background: rgba(255, 23, 68, 0.18);
  border-left: 4px solid #ff1744;
  color: #ffb0b0;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  border-bottom: 1px solid #2a2e39;
}
.account-liq-banner .meta {
  display: inline-block;
  margin-left: 12px;
  color: #ff8a8a;
  font-weight: 400;
  font-size: 12px;
}

/* ── Close-cause summary chips (in stats bar) ────────────────────────────── */
.cause-chips {
  display: flex;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid #2a2e39;
  background: #131722;
  font-size: 12px;
}
.cause-chips:empty { display: none; }
.cause-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 4px;
  background: #1e222d;
  border: 1px solid #2a2e39;
  font-variant-numeric: tabular-nums;
}
.cause-chip .dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.cause-chip.strategy_exit .dot   { background: #b2b5be; }
.cause-chip.protective_stop .dot { background: #ff8a65; }
.cause-chip.liquidation .dot     { background: #ff1744; }
.cause-chip .num { font-weight: 600; color: #e1e4ec; }
.cause-chip .pnl { color: #787b86; margin-left: 4px; }
.cause-chip .pnl.pos { color: #26a69a; }
.cause-chip .pnl.neg { color: #ef5350; }
</style>
</head>
<body>

<header>
  <h1>__TITLE__</h1>
  <span class="subtitle">__SUBTITLE__</span>
</header>

<div class="legend">
  __OVERLAY_LEGEND_HTML__
  <div class="legend-item">
    <div class="legend-arrow-up"></div>
    <span>Long entry</span>
  </div>
  <div class="legend-item">
    <div class="legend-arrow-down"></div>
    <span>Short entry</span>
  </div>
  <div class="legend-item">
    <div class="legend-line" style="background:#ff8a65"></div>
    <span>Protective stop</span>
  </div>
  <div class="legend-item">
    <div class="legend-line" style="background:#ff1744"></div>
    <span>Liquidation</span>
  </div>
</div>

<div class="account-liq-banner" id="account-liq-banner"></div>

<div id="chart-container">
  <div id="chart"></div>
  <div id="tooltip"></div>
</div>

<div class="cause-chips" id="cause-chips"></div>

<div class="stats-bar" id="stats-bar"></div>

<div class="trades-header">
  <span>Trade History &mdash; <span id="trade-count">0</span> closed positions</span>
  <div class="filter-bar">
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn" data-filter="long">Long</button>
    <button class="filter-btn" data-filter="short">Short</button>
  </div>
</div>
<div class="trades-wrap">
  <table class="trades">
    <thead>
      <tr>
        <th>#</th>
        <th>Opened (UTC)</th>
        <th>Closed (UTC)</th>
        <th>Side</th>
        <th class="r">Size</th>
        <th class="r">Entry Px</th>
        <th class="r">Exit Px</th>
        <th>Cause</th>
        <th class="r sortable" id="th-pnl">PnL <span class="sort-arrow">&varr;</span></th>
        <th class="r sortable" id="th-ret">Return % <span class="sort-arrow">&varr;</span></th>
      </tr>
    </thead>
    <tbody id="trades-body"></tbody>
  </table>
</div>

<script>
// ── Injected data (serialized by Python) ─────────────────────────────────────
const OHLCV         = __OHLCV_JSON__;
const OVERLAY_LINES = __OVERLAY_LINES_JSON__;
const MARKERS       = __MARKERS_JSON__;
const MARKER_DETAIL = __MARKER_DETAIL_JSON__;   // {"<unix_s>:<oid>": {ts, is_buy, side, qty, px, trade_num, close_cause}}
const TRADES        = __TRADES_JSON__;
const STATS              = __STATS_JSON__;
const STARTING_CAPITAL   = __STARTING_CAPITAL__;
const CAUSE_COUNTS  = __CAUSE_COUNTS_JSON__;     // {cause: {count, pnl}}
const ACCOUNT_LIQ   = __ACCOUNT_LIQ_JSON__;      // {} or {liq_ts_iso, equity_at_liq, ...}

// ── Chart ─────────────────────────────────────────────────────────────────────
const chartEl = document.getElementById('chart');

const chart = LightweightCharts.createChart(chartEl, {
  layout: {
    background: { type: 'solid', color: '#131722' },
    textColor: '#d1d4dc',
  },
  grid: {
    vertLines: { color: '#1e222d' },
    horzLines: { color: '#1e222d' },
  },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#2a2e39' },
  timeScale: {
    borderColor: '#2a2e39',
    timeVisible: true,
    secondsVisible: false,
  },
  handleScroll: true,
  handleScale: true,
});

// Candlestick series
const candleSeries = chart.addCandlestickSeries({
  upColor:         '#26a69a', downColor:         '#ef5350',
  borderUpColor:   '#26a69a', borderDownColor:   '#ef5350',
  wickUpColor:     '#26a69a', wickDownColor:     '#ef5350',
});
candleSeries.setData(OHLCV);

// Overlay line series (MA, Donchian, etc.)
OVERLAY_LINES.forEach(line => {
  const series = chart.addLineSeries({
    color: line.color,
    lineWidth: line.width || 1,
    lineStyle: line.style || 0,
    priceLineVisible: false,
    lastValueVisible: true,
    title: line.label,
  });
  series.setData(line.data);
});

// Markers
if (MARKERS.length > 0) {
  candleSeries.setMarkers(MARKERS);
}

chart.timeScale().fitContent();

// Resize
const ro = new ResizeObserver(entries => {
  if (entries.length === 0) return;
  const { width, height } = entries[0].contentRect;
  chart.applyOptions({ width, height });
});
ro.observe(chartEl);

// ── Hover tooltip ─────────────────────────────────────────────────────────────
const tooltipEl   = document.getElementById('tooltip');

// MARKER_DETAIL is keyed by "<ts>:<oid>" so simultaneous fills don't
// overwrite each other.  Group by timestamp; tooltip then renders all
// fills that share the hovered bar.
const detailByTs  = {};
for (const [k, v] of Object.entries(MARKER_DETAIL)) {
  const ts = (typeof v.ts === 'number') ? v.ts : parseInt(k.split(':')[0]);
  if (!detailByTs[ts]) detailByTs[ts] = [];
  detailByTs[ts].push(v);
}

function fmtNum(n, dec = 2) {
  if (n == null) return '—';
  return parseFloat(n).toLocaleString('en-US', {
    minimumFractionDigits: dec, maximumFractionDigits: dec
  });
}

function renderFillBlock(d) {
  const cls = d.is_buy ? 'buy' : 'sell';
  let h = `<hr class="tt-sep">`;
  if (d.trade_num != null) {
    h += `<div class="tt-row"><span class="tt-label">Trade</span><span class="tt-value">#${d.trade_num}</span></div>`;
  }
  h += `<div class="tt-row"><span class="tt-label">Signal</span><span class="tt-${cls}">${d.side}</span></div>`;
  h += `<div class="tt-row"><span class="tt-label">Qty</span><span class="tt-value">${d.qty}</span></div>`;
  h += `<div class="tt-row"><span class="tt-label">Fill px</span><span class="tt-value">${d.px}</span></div>`;
  if (d.close_cause && d.close_cause !== 'strategy_exit') {
    const causeLbl = d.close_cause === 'protective_stop' ? 'Protective stop' : 'Liquidation';
    h += `<div class="tt-row"><span class="tt-label">Cause</span><span class="tt-sell">${causeLbl}</span></div>`;
  }
  return h;
}

chart.subscribeCrosshairMove(param => {
  if (!param || !param.time || !param.point) {
    tooltipEl.style.display = 'none';
    return;
  }
  const bar = param.seriesData.get(candleSeries);
  if (!bar) { tooltipEl.style.display = 'none'; return; }

  const tsMs = param.time * 1000;
  const tsStr = new Date(tsMs).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  const mDetails = detailByTs[param.time] || [];

  let html = `<div class="tt-time">${tsStr}</div>`;
  html += `<div class="tt-row"><span class="tt-label">O</span><span class="tt-value">${fmtNum(bar.open)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">H</span><span class="tt-value">${fmtNum(bar.high)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">L</span><span class="tt-value">${fmtNum(bar.low)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">C</span><span class="tt-value">${fmtNum(bar.close)}</span></div>`;

  // Stack one block per fill at this bar.  NETTING reversals routinely
  // produce 2 fills (close + open) on the same bar.
  for (const d of mDetails) {
    html += renderFillBlock(d);
  }

  tooltipEl.innerHTML = html;

  // Position tooltip — flip sides at chart edge
  const cw = chartEl.clientWidth;
  const ttW = 165;
  let x = param.point.x + 16;
  let y = Math.max(0, param.point.y - 12);
  if (x + ttW > cw) x = param.point.x - ttW - 8;
  tooltipEl.style.left    = x + 'px';
  tooltipEl.style.top     = y + 'px';
  tooltipEl.style.display = 'block';
});

// ── Stats bar ──────────────────────────────────────────────────────────────────
function computeStats(trades) {
  const pnls = trades.map(t => t.pnl).filter(p => p != null);
  if (pnls.length === 0) return {};

  const totalPnl  = pnls.reduce((s, p) => s + p, 0);
  const winners   = pnls.filter(p => p > 0);
  const losers    = pnls.filter(p => p < 0);
  const grossWin  = winners.reduce((s, p) => s + p, 0);
  const grossLoss = Math.abs(losers.reduce((s, p) => s + p, 0));

  return {
    total_pnl:     totalPnl,
    total_pnl_pct: STARTING_CAPITAL ? (totalPnl / STARTING_CAPITAL * 100) : 0,
    num_trades:    pnls.length,
    win_rate:      (winners.length / pnls.length * 100),
    avg_win:       winners.length ? (grossWin / winners.length) : 0,
    avg_loss:      losers.length  ? (-grossLoss / losers.length) : 0,
    profit_factor: grossLoss ? (grossWin / grossLoss) : null,
  };
}

function renderStats(stats) {
  const bar = document.getElementById('stats-bar');
  if (!stats || Object.keys(stats).length === 0) { bar.style.display = 'none'; return; }
  bar.style.display = '';

  const sign = v => v > 0 ? 'pos' : (v < 0 ? 'neg' : '');
  const pf   = stats.profit_factor;
  const pfStr = (pf == null) ? '—' : (pf > 999 ? '∞' : fmtNum(pf));

  const cells = [
    { label: 'Total PnL',     value: fmtNum(stats.total_pnl),         sfx: ' USDC',  cls: sign(stats.total_pnl) },
    { label: 'Return',        value: fmtNum(stats.total_pnl_pct) + '%', sfx: '',      cls: sign(stats.total_pnl_pct) },
    { label: 'Trades',        value: stats.num_trades,                  sfx: '',      cls: '' },
    { label: 'Win Rate',      value: fmtNum(stats.win_rate, 1) + '%',   sfx: '',      cls: stats.win_rate >= 50 ? 'pos' : 'neg' },
    { label: 'Avg Win',       value: fmtNum(stats.avg_win),             sfx: '',      cls: 'pos' },
    { label: 'Avg Loss',      value: fmtNum(stats.avg_loss),            sfx: '',      cls: 'neg' },
    { label: 'Profit Factor', value: pfStr,                              sfx: '',      cls: (pf != null && pf >= 1) ? 'pos' : 'neg' },
  ];

  bar.innerHTML = cells.map(c =>
    `<div class="stat-cell">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value ${c.cls}">${c.value}${c.sfx}</div>
    </div>`
  ).join('');
}

renderStats(STATS);

// ── Close-cause chips + account-liq banner ──────────────────────────────────
function renderCauseChips(causeCounts) {
  const el = document.getElementById('cause-chips');
  if (!causeCounts || Object.keys(causeCounts).length === 0) {
    el.innerHTML = '';
    return;
  }
  const order = ['strategy_exit', 'protective_stop', 'liquidation'];
  const labels = {
    strategy_exit:    'Strategy exit',
    protective_stop:  'Protective stop',
    liquidation:      'Liquidation',
  };
  el.innerHTML = order
    .filter(k => causeCounts[k])
    .map(k => {
      const b = causeCounts[k];
      const pnlCls = b.pnl > 0 ? 'pos' : (b.pnl < 0 ? 'neg' : '');
      const pnlStr = (b.pnl >= 0 ? '+' : '') + fmtNum(b.pnl);
      return `<div class="cause-chip ${k}">
        <span class="dot"></span>
        <span>${labels[k]}</span>
        <span class="num">${b.count}</span>
        <span class="pnl ${pnlCls}">${pnlStr}</span>
      </div>`;
    })
    .join('');
}
renderCauseChips(CAUSE_COUNTS);

function renderAccountLiqBanner(summary) {
  const el = document.getElementById('account-liq-banner');
  if (!summary || !summary.liq_ts_iso) {
    el.style.display = 'none';
    return;
  }
  const before = summary.equity_before != null ? fmtNum(summary.equity_before) : '—';
  const at = summary.equity_at_liq != null ? fmtNum(summary.equity_at_liq) : '—';
  const drain = summary.drain_amount != null ? fmtNum(summary.drain_amount) : '—';
  const culprit = summary.culprit_count != null ? summary.culprit_count : 0;
  el.innerHTML = `⚠️ ACCOUNT LIQUIDATED at ${summary.liq_ts_iso}` +
    `<span class="meta">equity ${before} → ${at} (drain ${drain}) ` +
    `· ${culprit} open position(s) at the moment of liquidation</span>`;
  el.style.display = 'block';
}
renderAccountLiqBanner(ACCOUNT_LIQ);

// ── Trade table ───────────────────────────────────────────────────────────────
(function renderTrades() {
  const tbody = document.getElementById('trades-body');
  document.getElementById('trade-count').textContent = TRADES.length;

  if (TRADES.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="no-trades">No closed positions found</td></tr>';
    return;
  }

  const causeLabel = c =>
    c === 'protective_stop' ? 'Stop'  :
    c === 'liquidation'     ? 'Liq'   :
    c === 'strategy_exit'   ? 'Strat' : '—';

  tbody.innerHTML = TRADES.map((t, i) => {
    const isLong   = t.side === 'Long';
    const pnl      = t.pnl;
    const pnlCls   = (pnl == null) ? '' : (pnl >= 0 ? 'pos' : 'neg');
    const pnlStr   = (pnl == null) ? '—' : (pnl >= 0 ? '+' : '') + fmtNum(pnl);
    const ret      = t.realized_return;
    const retStr   = (ret == null) ? '—' : (ret >= 0 ? '+' : '') + fmtNum(ret * 100, 2) + '%';
    const retCls   = (ret == null) ? '' : (ret >= 0 ? 'pos' : 'neg');
    const cause    = t.close_cause || 'strategy_exit';

    return `<tr data-ts="${t.opened_ts_s || 0}" data-pnl="${pnl ?? 0}" data-ret="${ret ?? 0}" data-side="${isLong ? 'long' : 'short'}" data-cause="${cause}" onclick="scrollChart(this)">
      <td class="id">${i + 1}</td>
      <td>${t.opened}</td>
      <td>${t.closed}</td>
      <td><span class="badge ${isLong ? 'long' : 'short'}">${t.side}</span></td>
      <td class="r">${t.qty}</td>
      <td class="r">${t.entry_px}</td>
      <td class="r">${t.exit_px}</td>
      <td><span class="badge cause-${cause}">${causeLabel(cause)}</span></td>
      <td class="r pnl ${pnlCls}">${pnlStr}</td>
      <td class="r pnl ${retCls}">${retStr}</td>
    </tr>`;
  }).join('');
})();

// ── Click table row → zoom chart to that trade ─────────────────────────────────
function scrollChart(row) {
  document.querySelectorAll('table.trades tr.active')
    .forEach(r => r.classList.remove('active'));
  row.classList.add('active');

  const ts = parseInt(row.dataset.ts);
  if (!ts || OHLCV.length < 2) return;

  const barSec = OHLCV[1].time - OHLCV[0].time;   // bar duration in seconds
  const window = barSec * 60;                       // show 60 bars around trade

  chart.timeScale().setVisibleRange({
    from: ts - window / 2,
    to:   ts + window / 2,
  });
}

// ── Sortable columns ─────────────────────────────────────────────────────────
(function setupSort() {
  const cols = [
    { id: 'th-pnl', key: 'pnl' },
    { id: 'th-ret', key: 'ret' },
  ];
  const state = {};
  cols.forEach(c => { state[c.id] = 0; });

  function resetTh(th) {
    th.className = 'r sortable';
    th.querySelector('.sort-arrow').innerHTML = '&varr;';
  }

  cols.forEach(({ id, key }) => {
    const th = document.getElementById(id);
    if (!th) return;

    th.addEventListener('click', () => {
      cols.forEach(c => {
        if (c.id !== id) {
          state[c.id] = 0;
          const otherTh = document.getElementById(c.id);
          if (otherTh) resetTh(otherTh);
        }
      });

      state[id] = (state[id] + 1) % 3;
      const dir = state[id];
      const tbody = document.getElementById('trades-body');
      const rows = Array.from(tbody.querySelectorAll('tr'));

      if (dir === 0) {
        rows.sort((a, b) => parseFloat(a.dataset.ts) - parseFloat(b.dataset.ts));
        resetTh(th);
      } else if (dir === 1) {
        rows.sort((a, b) => parseFloat(b.dataset[key]) - parseFloat(a.dataset[key]));
        th.className = 'r sortable desc';
        th.querySelector('.sort-arrow').textContent = '\\u25BC';
      } else {
        rows.sort((a, b) => parseFloat(a.dataset[key]) - parseFloat(b.dataset[key]));
        th.className = 'r sortable asc';
        th.querySelector('.sort-arrow').textContent = '\\u25B2';
      }

      rows.forEach(r => tbody.appendChild(r));
      applyFilter();
    });
  });
})();

// ── Filter by side ───────────────────────────────────────────────────────────
let currentFilter = 'all';

function applyFilter() {
  const tbody = document.getElementById('trades-body');
  const rows = tbody.querySelectorAll('tr[data-side]');
  let visible = 0;
  rows.forEach(r => {
    const show = currentFilter === 'all' || r.dataset.side === currentFilter;
    r.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('trade-count').textContent = visible;

  // Recompute stats for filtered subset
  if (currentFilter === 'all') {
    renderStats(STATS);
  } else {
    const side = currentFilter === 'long' ? 'Long' : 'Short';
    const filtered = TRADES.filter(t => t.side === side);
    renderStats(computeStats(filtered));
  }
}

document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    applyFilter();
  });
});
</script>
</body>
</html>
""")


# ─────────────────────────────────────────────────────────────────────────────
# Public: self-contained sortable sweep HTML
# ─────────────────────────────────────────────────────────────────────────────

# Column display order + formatting hints for the sweep HTML report.
# (column_name, header_label, formatter, css_class)
# Formatter is one of: "int", "float2", "money", "pct", "pct_signed",
# "ratio", "bool", "raw".  CSS class is added for color coding.
_SWEEP_DEFAULT_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    # Strategy params come first — auto-detected at runtime.
    # Below are the standard metric columns in display order.
    ("total_pnl", "PnL ($)", "money", "num"),
    ("total_pnl_pct", "PnL %", "pct_signed", "num"),
    ("num_positions", "# Trades", "int", "num"),
    ("win_rate", "Win Rate", "pct", "num"),
    ("avg_pnl_per_trade", "Avg $/Trade", "money", "num"),
    ("pnl_profit_factor", "Profit Factor", "ratio", "num"),
    ("expectancy", "Expectancy", "money", "num"),
    ("payoff_ratio", "Payoff", "ratio", "num"),
    ("max_drawdown_pct", "Max DD %", "pct", "num"),
    ("max_drawdown_abs", "Max DD ($)", "money", "num"),
    ("mar_ratio", "MAR", "ratio", "num"),
    ("recovery_factor", "Recovery", "ratio", "num"),
    ("cagr", "CAGR", "pct_signed", "num"),
    ("max_consec_losers", "Max Losers", "int", "num"),
    ("bars_in_market_pct", "In Market", "pct", "num"),
    ("largest_win", "Largest Win", "money", "num"),
    ("largest_loss", "Largest Loss", "money", "num"),
    ("long_pnl", "Long PnL", "money", "num"),
    ("short_pnl", "Short PnL", "money", "num"),
    ("total_fees", "Fees", "money", "num"),
    ("fee_pct_of_pnl", "Fees % PnL", "pct", "num"),
    ("min_balance", "Min Bal", "money", "num"),
    ("liquidated", "Liq.", "bool", "num"),
)


def _fmt_sweep_cell(value: Any, kind: str) -> str:
    """Format a single sweep-row cell for HTML display.

    Returns an HTML-safe string with appropriate precision and CSS class
    hints (positive/negative numerics get colored downstream).
    NaN renders as an em-dash.
    """
    import math as _math

    if value is None or (isinstance(value, float) and _math.isnan(value)):
        return "—"
    if kind == "int":
        try:
            return f"{int(value):,}"
        except (ValueError, TypeError):
            return str(value)
    if kind == "float2":
        return f"{float(value):.2f}"
    if kind == "money":
        v = float(value)
        cls = "num-positive" if v > 0 else ("num-negative" if v < 0 else "")
        return f'<span class="{cls}">{v:>12,.2f}</span>' if cls else f"{v:>12,.2f}"
    if kind == "pct":
        return f"{float(value) * 100:.2f}%"
    if kind == "pct_signed":
        v = float(value)
        # total_pnl_pct is already in percentage units; cagr is in fractional units.
        # Heuristic: if abs(v) > 5 we treat as already-percent (PnL%, drawdown
        # in percentage points); otherwise we multiply by 100 (CAGR fractional).
        # We disambiguate via the column name in the caller — but for now
        # use a simple rule that handles the common cases cleanly.
        cls = "num-positive" if v > 0 else ("num-negative" if v < 0 else "")
        # If value looks like a fraction (CAGR style: 0.30) format as %.
        # If it looks like an already-pct number (PnL%: 951.06), keep as %.
        if abs(v) <= 5:
            text = f"{v * 100:.2f}%"
        else:
            text = f"{v:.2f}%"
        return f'<span class="{cls}">{text}</span>' if cls else text
    if kind == "ratio":
        v = float(value)
        if _math.isinf(v):
            return "∞"
        return f"{v:.2f}"
    if kind == "bool":
        return (
            '<span class="badge badge-liquidated">LIQ</span>'
            if bool(value)
            else ""
        )
    # "raw" / fallback: round numerics to ≤2dp, preserve everything else.
    return _fmt_sweep_auto(value)


def _fmt_sweep_auto(value: Any) -> str:
    """Smart fallback formatter — rounds numerics to ≤2dp.

    Keeps integers as integers (no trailing zeros), formats floats and
    Decimals to at most 2 decimal places (whole numbers strip the ``.00``
    suffix), passes through strings / bools / other types unchanged.
    Used for strategy parameter columns and any user-supplied
    ``extra_columns`` whose units we don't know.
    """
    import math as _math
    from decimal import Decimal

    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if _math.isnan(value):
            return "—"
        if _math.isinf(value):
            return "∞"
        # Strip trailing .00 for whole numbers
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}"
    if isinstance(value, Decimal):
        f = float(value)
        if f.is_integer():
            return str(int(f))
        return f"{f:.2f}"
    return str(value)


def generate_sweep_html(
    results_df: pd.DataFrame,
    *,
    output_dir: str | Path | None = None,
    filename: str | None = None,
    title: str | None = None,
    extra_columns: list[str] | None = None,
    open_browser: bool = False,
    heatmap_path: str | Path | None = None,
    heatmap_caption: str | None = None,
) -> Path:
    """Generate an interactive, sortable HTML report from a sweep DataFrame.

    Built on DataTables.js (loaded from CDN — needs network on first
    open; subsequent opens are cached by the browser).  Features:

    * Click any column header to sort
    * Search box filters all columns
    * Per-column sort with shift-click (multi-column)
    * Pagination (25 rows / page default)
    * CSV export button
    * Liquidated rows highlighted red, spotlight rows (``_kind ==
      "spotlight"``) highlighted gold with a badge

    Output is a self-contained HTML file (the only external resources
    are the DataTables CDN scripts).  Dark theme to match the existing
    TVLC report style.

    Parameters
    ----------
    results_df
        DataFrame from :func:`run_sweep`.  Expected to include the
        v2-schema columns (see ``src/backtesting/metrics.py``); columns
        that are present are shown, those that aren't are skipped.
    output_dir
        Directory to write the HTML file.  Default ``reports/sweeps/``
        relative to the project root.
    filename
        Custom filename (without ``.html``).  Default uses the sweep's
        metadata: ``{strategy}_{instrument}_{interval}.html``.
    title
        Custom title.  Default derived from the sweep's metadata
        columns (``_strategy``, ``_instrument_id``, ``_bar_interval``).
    extra_columns
        Additional column names to include beyond the default set.
        Useful for showing sweep-specific stats not in the default list.
    open_browser
        If True, opens the generated HTML in the default browser.
    heatmap_path
        Optional path to a PNG of the sweep's PnL heatmap (typically
        produced by passing ``save_to`` to :func:`plot_pnl_heatmap`).
        When provided, the image is base64-embedded into the sweep
        HTML above the table so the heatmap and the sortable grid
        ship as a single self-contained artifact.
    heatmap_caption
        Optional caption text rendered under the embedded heatmap.
        Default is a short reminder of what the colors mean.

    Returns
    -------
    pathlib.Path
        Absolute path to the generated HTML file.

    """
    if results_df.empty:
        msg = "Cannot generate sweep HTML from empty DataFrame."
        raise ValueError(msg)

    # ── Derive metadata from the DataFrame ────────────────────────────────
    strategy = (
        results_df["_strategy"].iloc[0]
        if "_strategy" in results_df.columns
        else "?"
    )
    instrument = (
        results_df["_instrument_id"].iloc[0]
        if "_instrument_id" in results_df.columns
        else "?"
    )
    interval = (
        results_df["_bar_interval"].iloc[0]
        if "_bar_interval" in results_df.columns
        else "?"
    )
    swept_at = (
        results_df["_swept_at"].iloc[0]
        if "_swept_at" in results_df.columns
        else "?"
    )
    schema_version = (
        int(results_df["_schema_version"].iloc[0])
        if "_schema_version" in results_df.columns
        else 1
    )

    if title is None:
        title = f"{strategy} · {instrument} · {interval}"

    # ── Resolve output path ───────────────────────────────────────────────
    if output_dir is None:
        proj_root = Path(__file__).resolve().parent.parent
        output_dir = proj_root / "reports" / "sweeps"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        # Mirror the sweep parquet naming convention.
        safe_inst = str(instrument).replace("/", "-")
        filename = f"{strategy}_{safe_inst}_{interval}.html"
    if not filename.endswith(".html"):
        filename = f"{filename}.html"
    out_path = output_dir / filename

    # ── Determine which columns to render ─────────────────────────────────
    # Auto-detect strategy parameter columns: anything not starting with
    # "_" and not in the metric or status set.
    metric_cols = {col[0] for col in _SWEEP_DEFAULT_COLUMNS}
    skip_cols = metric_cols | {"error"}
    skip_prefix = "_"

    param_cols = [
        c
        for c in results_df.columns
        if c not in skip_cols and not c.startswith(skip_prefix)
    ]

    # Filter the default metric set down to columns actually present.
    metric_specs = [
        spec
        for spec in _SWEEP_DEFAULT_COLUMNS
        if spec[0] in results_df.columns
    ]

    # Add user-requested extras (with raw formatter).
    extra_specs: list[tuple[str, str, str, str]] = []
    if extra_columns:
        for col in extra_columns:
            if col in results_df.columns and col not in skip_cols:
                extra_specs.append((col, col, "raw", "num"))

    # _kind metadata column gets a leading badge column when any spotlight
    # rows exist.  Detected by checking values, not just column presence.
    has_kind_column = (
        "_kind" in results_df.columns
        and results_df["_kind"].notna().any()
    )

    # ── Render rows ──────────────────────────────────────────────────────
    # Determine summary stats for the header bar
    n_combos = len(results_df)
    n_liq = (
        int(results_df["liquidated"].sum())
        if "liquidated" in results_df.columns
        else 0
    )
    n_grid = (
        int((results_df["_kind"] != "spotlight").sum())
        if has_kind_column
        else n_combos
    )
    n_spot = n_combos - n_grid
    median_dd = (
        float(results_df["max_drawdown_pct"].median())
        if "max_drawdown_pct" in results_df.columns
        else float("nan")
    )
    best_pnl = (
        float(results_df["total_pnl"].max())
        if "total_pnl" in results_df.columns
        else float("nan")
    )

    # Header row
    header_cells: list[str] = []
    if has_kind_column:
        header_cells.append("<th>Kind</th>")
    for col in param_cols:
        header_cells.append(f"<th>{html.escape(str(col))}</th>")
    for _col, label, _kind, _cls in metric_specs:
        header_cells.append(f"<th>{html.escape(label)}</th>")
    for _col, label, _kind, _cls in extra_specs:
        header_cells.append(f"<th>{html.escape(label)}</th>")

    # Body rows
    body_rows: list[str] = []
    for _idx, row in results_df.iterrows():
        is_liq = bool(row.get("liquidated", False))
        kind_val = row.get("_kind") if has_kind_column else None
        is_spot = kind_val == "spotlight"
        row_cls_parts = []
        if is_liq:
            row_cls_parts.append("liquidated")
        if is_spot:
            row_cls_parts.append("spotlight")
        row_cls = f' class="{" ".join(row_cls_parts)}"' if row_cls_parts else ""

        cells: list[str] = []
        if has_kind_column:
            if is_spot:
                cells.append(
                    '<td><span class="badge badge-spotlight">SPOT</span></td>',
                )
            elif kind_val:
                cells.append(f"<td>{html.escape(str(kind_val))}</td>")
            else:
                cells.append("<td></td>")
        for col in param_cols:
            cells.append(
                f"<td>{html.escape(_fmt_sweep_auto(row.get(col)))}</td>",
            )
        for col, _label, kind, css in metric_specs:
            formatted = _fmt_sweep_cell(row.get(col), kind)
            cells.append(f'<td class="{css}">{formatted}</td>')
        for col, _label, kind, css in extra_specs:
            formatted = _fmt_sweep_cell(row.get(col), kind)
            cells.append(f'<td class="{css}">{formatted}</td>')

        body_rows.append(f'<tr{row_cls}>{"".join(cells)}</tr>')

    # ── Default sort: total_pnl desc if present, else first numeric ─────
    sort_col_idx = 0  # fallback to first column
    sort_target = "total_pnl"
    if sort_target in {spec[0] for spec in metric_specs}:
        # offset = leading kind column + param columns + offset within metrics
        offset = (1 if has_kind_column else 0) + len(param_cols)
        for i, (col, *_rest) in enumerate(metric_specs):
            if col == sort_target:
                sort_col_idx = offset + i
                break

    # ── Optional embedded heatmap ─────────────────────────────────────────
    heatmap_block = ""
    if heatmap_path is not None:
        hm_path = Path(heatmap_path)
        if hm_path.exists():
            import base64  # noqa: PLC0415 — keep heavy import lazy
            mime = "image/png" if hm_path.suffix.lower() == ".png" else "image/svg+xml"
            data = base64.b64encode(hm_path.read_bytes()).decode("ascii")
            cap = heatmap_caption or (
                "Diverging RdYlGn — green = profit, red = loss; "
                "grey/underlined cells flag liquidations."
            )
            heatmap_block = (
                '<section class="heatmap-block">'
                f'<img src="data:{mime};base64,{data}" alt="PnL heatmap" />'
                f'<div class="heatmap-caption">{html.escape(cap)}</div>'
                "</section>"
            )
        else:
            print(
                f"⚠ heatmap_path={hm_path} does not exist — skipping embed.",
            )

    # ── Build HTML ────────────────────────────────────────────────────────
    html_doc = _SWEEP_HTML_TEMPLATE.format(
        title=html.escape(title),
        strategy=html.escape(str(strategy)),
        instrument=html.escape(str(instrument)),
        interval=html.escape(str(interval)),
        swept_at=html.escape(str(swept_at)),
        schema_version=schema_version,
        n_combos=n_combos,
        n_grid=n_grid,
        n_spot=n_spot,
        n_liq=n_liq,
        best_pnl=f"{best_pnl:,.2f}" if math.isfinite(best_pnl) else "—",
        median_dd=(
            f"{median_dd * 100:.2f}%" if math.isfinite(median_dd) else "—"
        ),
        header_cells="\n".join(header_cells),
        body_rows="\n".join(body_rows),
        sort_col_idx=sort_col_idx,
        heatmap_block=heatmap_block,
    )

    out_path.write_text(html_doc, encoding="utf-8")
    print(f"✓ Sweep HTML written → {out_path}")

    if open_browser:
        webbrowser.open(out_path.as_uri())

    return out_path


_SWEEP_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Sweep — {title}</title>
  <link rel="stylesheet" href="https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.min.css">
  <link rel="stylesheet" href="https://cdn.datatables.net/buttons/3.2.0/css/buttons.dataTables.min.css">
  <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
  <script src="https://cdn.datatables.net/2.1.8/js/dataTables.min.js"></script>
  <script src="https://cdn.datatables.net/buttons/3.2.0/js/dataTables.buttons.min.js"></script>
  <script src="https://cdn.datatables.net/buttons/3.2.0/js/buttons.html5.min.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0f1116;
      color: #d1d4dc;
      margin: 0;
      padding: 24px;
    }}
    h1 {{
      font-size: 18px;
      margin: 0 0 4px 0;
      color: #fff;
    }}
    .subtitle {{
      color: #888;
      font-size: 12px;
      margin-bottom: 16px;
    }}
    .stats-bar {{
      background: #1a1d24;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 6px;
      display: flex;
      gap: 24px;
      flex-wrap: wrap;
    }}
    .stat-item {{
      font-size: 12px;
    }}
    .stat-label {{
      color: #888;
      margin-right: 6px;
    }}
    .stat-value {{
      font-weight: bold;
      color: #fff;
    }}
    table.dataTable {{
      font-family: "Menlo", "Monaco", "Consolas", monospace;
      font-size: 11px;
      background: #1a1d24;
      color: #d1d4dc;
      border-collapse: collapse;
    }}
    table.dataTable thead th {{
      background: #232730;
      border-bottom: 1px solid #383b45;
      padding: 8px 12px;
      color: #fff;
      font-weight: bold;
      cursor: pointer;
      /* Pin the header to the top of the viewport when the user scrolls.
         !important is needed because DataTables applies its own rules
         at runtime that override these otherwise. */
      position: sticky !important;
      top: 0 !important;
      z-index: 10 !important;
    }}
    table.dataTable tbody td {{
      padding: 6px 12px;
      border-bottom: 1px solid #2a2d36;
    }}
    table.dataTable tbody tr {{
      background: #1a1d24;
    }}
    table.dataTable tbody tr:hover {{
      background: #232730;
    }}
    table.dataTable tbody tr.liquidated {{
      background: rgba(214, 39, 40, 0.10);
    }}
    table.dataTable tbody tr.liquidated:hover {{
      background: rgba(214, 39, 40, 0.20);
    }}
    table.dataTable tbody tr.spotlight {{
      background: rgba(255, 200, 0, 0.07);
    }}
    table.dataTable tbody tr.spotlight:hover {{
      background: rgba(255, 200, 0, 0.14);
    }}
    .num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .num-positive {{
      color: #2ca02c;
    }}
    .num-negative {{
      color: #d62728;
    }}
    .badge {{
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 9px;
      font-weight: bold;
      letter-spacing: 0.5px;
    }}
    .badge-spotlight {{
      background: #ffc800;
      color: #000;
    }}
    .badge-liquidated {{
      background: #d62728;
      color: #fff;
    }}
    .dataTables_wrapper {{
      color: #d1d4dc;
    }}
    .dataTables_filter input,
    .dataTables_length select {{
      background: #1a1d24;
      color: #d1d4dc;
      border: 1px solid #383b45;
      padding: 4px 8px;
      border-radius: 3px;
    }}
    .dataTables_paginate .paginate_button {{
      color: #d1d4dc !important;
    }}
    .dataTables_paginate .paginate_button.current {{
      background: #2962ff !important;
      color: #fff !important;
      border: 0 !important;
    }}
    button.dt-button {{
      background: #2962ff !important;
      color: #fff !important;
      border: 0 !important;
      padding: 6px 12px !important;
      font-size: 11px !important;
      margin-bottom: 8px !important;
    }}
    .legend {{
      font-size: 11px;
      color: #888;
      margin-top: 12px;
    }}
    .legend .swatch {{
      display: inline-block;
      width: 12px;
      height: 12px;
      vertical-align: middle;
      margin-right: 6px;
      margin-left: 12px;
      border-radius: 2px;
    }}
    .swatch.liq {{ background: rgba(214, 39, 40, 0.30); }}
    .swatch.spot {{ background: rgba(255, 200, 0, 0.30); }}

    .heatmap-block {{
      background: #1a1d24;
      border-radius: 6px;
      padding: 12px;
      margin: 16px 0;
      text-align: center;
    }}
    .heatmap-block img {{
      max-width: 100%;
      height: auto;
      border-radius: 4px;
      background: #ffffff;
    }}
    .heatmap-caption {{
      color: #888;
      font-size: 11px;
      margin-top: 8px;
      font-style: italic;
    }}
  </style>
</head>
<body>
  <h1>Sweep — {title}</h1>
  <div class="subtitle">
    Schema v{schema_version} · generated {swept_at}
  </div>

  <div class="stats-bar">
    <div class="stat-item">
      <span class="stat-label">Combos:</span><span class="stat-value">{n_combos}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Grid:</span><span class="stat-value">{n_grid}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Spotlight:</span><span class="stat-value">{n_spot}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Liquidated:</span><span class="stat-value">{n_liq}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Best PnL:</span><span class="stat-value">{best_pnl}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Median DD:</span><span class="stat-value">{median_dd}</span>
    </div>
  </div>

  <table id="sweepTable" class="display compact" style="width: 100%">
    <thead>
      <tr>
{header_cells}
      </tr>
    </thead>
    <tbody>
{body_rows}
    </tbody>
  </table>

  <div class="legend">
    Click headers to sort · Shift-click for multi-column · CSV export top-left
    <span class="swatch liq"></span>liquidated
    <span class="swatch spot"></span>spotlight (off-grid)
  </div>

  {heatmap_block}

  <script>
    $(document).ready(function() {{
      $('#sweepTable').DataTable({{
        order: [[ {sort_col_idx}, 'desc' ]],
        // No pagination — full sweep visible, scroll vertically with the
        // header pinned (CSS position: sticky on thead).
        paging: false,
        info: false,
        layout: {{
          topStart: ['buttons'],
        }},
        buttons: [
          {{
            extend: 'csv',
            text: 'Download CSV',
            filename: 'sweep_{strategy}_{instrument}_{interval}',
          }}
        ],
      }});
    }});
  </script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public: cross-sweep sortable HTML (multi-sweep comparison)
# ─────────────────────────────────────────────────────────────────────────────


def generate_cross_sweep_html(
    sweeps: dict[str, pd.DataFrame],
    *,
    output_dir: str | Path | None = None,
    filename: str | None = None,
    title: str | None = None,
    extra_columns: list[str] | None = None,
    open_browser: bool = False,
) -> Path:
    """Render a single sortable HTML table that combines rows from many sweeps.

    Companion to :func:`generate_sweep_html` for cross-sweep
    (multi-instrument / multi-timeframe / multi-strategy) comparison.
    Each input sweep contributes its rows to one combined table; an
    extra ``Sweep`` column identifies which sweep each row came from.

    Parameters
    ----------
    sweeps
        Mapping from sweep label to a sweep DataFrame (the same shape
        returned by :func:`run_sweep` / :func:`load_sweeps`).  The
        label is shown verbatim in the ``Sweep`` column and is used to
        construct the default title.
    output_dir
        Directory to write the HTML file.  Default ``reports/sweeps/``
        relative to the project root.
    filename
        Custom filename (with or without ``.html``).  Default
        ``cross_sweep.html``.
    title
        Custom title shown in the HTML header.  Default ``Cross-sweep
        comparison — N sweeps``.
    extra_columns
        Additional column names to include beyond the v2 default set.
        Useful for sweep-specific stats not in the default list.
    open_browser
        If True, opens the generated HTML in the default browser.

    Returns
    -------
    pathlib.Path
        Absolute path to the generated HTML file.

    Notes
    -----
    Param columns vary across strategies (e.g. ``fast_period`` vs
    ``length``).  The function takes the **union** of all param
    columns; rows from a sweep that doesn't have a given param render
    as empty cells in that column.

    Schema-version mismatches across input sweeps are surfaced in the
    header subtitle (``schema vMixed`` if not all the same).

    """
    if not sweeps:
        msg = "Cannot generate cross-sweep HTML from empty sweeps mapping."
        raise ValueError(msg)

    # ── Filter out empty inputs and tag rows with their sweep label ──────
    tagged: list[pd.DataFrame] = []
    schema_versions: set[int] = set()
    for label, df in sweeps.items():
        if df is None or df.empty:
            print(f"  skipping empty sweep '{label}'")
            continue
        tagged_df = df.copy()
        tagged_df["_sweep_label"] = str(label)
        tagged.append(tagged_df)
        if "_schema_version" in tagged_df.columns:
            try:
                schema_versions.add(int(tagged_df["_schema_version"].iloc[0]))
            except (ValueError, TypeError):
                pass

    if not tagged:
        msg = "All input sweeps are empty — nothing to render."
        raise ValueError(msg)

    # Concat with outer join so missing param columns become NaN.
    combined = pd.concat(tagged, ignore_index=True, sort=False)
    n_sweeps = len(tagged)

    if title is None:
        title = f"{n_sweeps} sweeps"

    if len(schema_versions) == 1:
        schema_str = f"v{next(iter(schema_versions))}"
    elif schema_versions:
        schema_str = "vMixed"
    else:
        schema_str = "v?"

    # ── Resolve output path ──────────────────────────────────────────────
    if output_dir is None:
        proj_root = Path(__file__).resolve().parent.parent
        output_dir = proj_root / "reports" / "sweeps"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        filename = "cross_sweep.html"
    if not filename.endswith(".html"):
        filename = f"{filename}.html"
    out_path = output_dir / filename

    # ── Determine columns ────────────────────────────────────────────────
    metric_cols = {col[0] for col in _SWEEP_DEFAULT_COLUMNS}
    skip_cols = metric_cols | {"error", "_sweep_label"}
    skip_prefix = "_"

    # Union of strategy param columns across all sweeps, preserving
    # first-seen order.
    seen: set[str] = set()
    param_cols: list[str] = []
    for df in tagged:
        for c in df.columns:
            if c in skip_cols or c.startswith(skip_prefix):
                continue
            if c not in seen:
                seen.add(c)
                param_cols.append(c)

    metric_specs = [
        spec
        for spec in _SWEEP_DEFAULT_COLUMNS
        if spec[0] in combined.columns
    ]

    extra_specs: list[tuple[str, str, str, str]] = []
    if extra_columns:
        for col in extra_columns:
            if col in combined.columns and col not in skip_cols:
                extra_specs.append((col, col, "raw", "num"))

    has_kind_column = (
        "_kind" in combined.columns and combined["_kind"].notna().any()
    )

    # ── Stats bar ────────────────────────────────────────────────────────
    n_combos = len(combined)
    n_liq = (
        int(combined["liquidated"].sum())
        if "liquidated" in combined.columns
        else 0
    )
    n_grid = (
        int((combined["_kind"] != "spotlight").sum())
        if has_kind_column
        else n_combos
    )
    n_spot = n_combos - n_grid
    median_dd = (
        float(combined["max_drawdown_pct"].median())
        if "max_drawdown_pct" in combined.columns
        else float("nan")
    )
    best_pnl = (
        float(combined["total_pnl"].max())
        if "total_pnl" in combined.columns
        else float("nan")
    )

    # ── Header row: Sweep | [Kind] | params... | metrics... | extras... ─
    header_cells: list[str] = ["<th>Sweep</th>"]
    if has_kind_column:
        header_cells.append("<th>Kind</th>")
    for col in param_cols:
        header_cells.append(f"<th>{html.escape(str(col))}</th>")
    for _col, label, _kind, _cls in metric_specs:
        header_cells.append(f"<th>{html.escape(label)}</th>")
    for _col, label, _kind, _cls in extra_specs:
        header_cells.append(f"<th>{html.escape(label)}</th>")

    # ── Body rows ────────────────────────────────────────────────────────
    body_rows: list[str] = []
    for _idx, row in combined.iterrows():
        is_liq = bool(row.get("liquidated", False))
        kind_val = row.get("_kind") if has_kind_column else None
        is_spot = kind_val == "spotlight"
        row_cls_parts: list[str] = []
        if is_liq:
            row_cls_parts.append("liquidated")
        if is_spot:
            row_cls_parts.append("spotlight")
        row_cls = f' class="{" ".join(row_cls_parts)}"' if row_cls_parts else ""

        cells: list[str] = [
            f"<td>{html.escape(str(row.get('_sweep_label', '')))}</td>",
        ]
        if has_kind_column:
            if is_spot:
                cells.append(
                    '<td><span class="badge badge-spotlight">SPOT</span></td>',
                )
            elif kind_val:
                cells.append(f"<td>{html.escape(str(kind_val))}</td>")
            else:
                cells.append("<td></td>")
        for col in param_cols:
            cells.append(
                f"<td>{html.escape(_fmt_sweep_auto(row.get(col)))}</td>",
            )
        for col, _label, kind, css in metric_specs:
            formatted = _fmt_sweep_cell(row.get(col), kind)
            cells.append(f'<td class="{css}">{formatted}</td>')
        for col, _label, kind, css in extra_specs:
            formatted = _fmt_sweep_cell(row.get(col), kind)
            cells.append(f'<td class="{css}">{formatted}</td>')

        body_rows.append(f'<tr{row_cls}>{"".join(cells)}</tr>')

    # ── Default sort: total_pnl desc if present ──────────────────────────
    sort_col_idx = 0
    sort_target = "total_pnl"
    if sort_target in {spec[0] for spec in metric_specs}:
        # offset = leading Sweep column + (Kind column?) + param columns
        offset = 1 + (1 if has_kind_column else 0) + len(param_cols)
        for i, (col, *_rest) in enumerate(metric_specs):
            if col == sort_target:
                sort_col_idx = offset + i
                break

    # ── Render ───────────────────────────────────────────────────────────
    html_doc = _CROSS_SWEEP_HTML_TEMPLATE.format(
        title=html.escape(title),
        schema_str=html.escape(schema_str),
        n_sweeps=n_sweeps,
        n_combos=n_combos,
        n_grid=n_grid,
        n_spot=n_spot,
        n_liq=n_liq,
        best_pnl=f"{best_pnl:,.2f}" if math.isfinite(best_pnl) else "—",
        median_dd=(
            f"{median_dd * 100:.2f}%" if math.isfinite(median_dd) else "—"
        ),
        header_cells="\n".join(header_cells),
        body_rows="\n".join(body_rows),
        sort_col_idx=sort_col_idx,
        csv_filename=Path(filename).stem,
    )

    out_path.write_text(html_doc, encoding="utf-8")
    print(f"✓ Cross-sweep HTML written → {out_path}")

    if open_browser:
        webbrowser.open(out_path.as_uri())

    return out_path


_CROSS_SWEEP_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cross-sweep — {title}</title>
  <link rel="stylesheet" href="https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.min.css">
  <link rel="stylesheet" href="https://cdn.datatables.net/buttons/3.2.0/css/buttons.dataTables.min.css">
  <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
  <script src="https://cdn.datatables.net/2.1.8/js/dataTables.min.js"></script>
  <script src="https://cdn.datatables.net/buttons/3.2.0/js/dataTables.buttons.min.js"></script>
  <script src="https://cdn.datatables.net/buttons/3.2.0/js/buttons.html5.min.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: #0f1116;
      color: #d1d4dc;
      margin: 0;
      padding: 24px;
    }}
    h1 {{
      font-size: 18px;
      margin: 0 0 4px 0;
      color: #fff;
    }}
    .subtitle {{
      color: #888;
      font-size: 12px;
      margin-bottom: 16px;
    }}
    .stats-bar {{
      background: #1a1d24;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 6px;
      display: flex;
      gap: 24px;
      flex-wrap: wrap;
    }}
    .stat-item {{
      font-size: 12px;
    }}
    .stat-label {{
      color: #888;
      margin-right: 6px;
    }}
    .stat-value {{
      font-weight: bold;
      color: #fff;
    }}
    table.dataTable {{
      font-family: "Menlo", "Monaco", "Consolas", monospace;
      font-size: 11px;
      background: #1a1d24;
      color: #d1d4dc;
      border-collapse: collapse;
    }}
    table.dataTable thead th {{
      background: #232730;
      border-bottom: 1px solid #383b45;
      padding: 8px 12px;
      color: #fff;
      font-weight: bold;
      cursor: pointer;
      /* Pin the header to the top of the viewport when the user scrolls.
         !important is needed because DataTables applies its own rules
         at runtime that override these otherwise. */
      position: sticky !important;
      top: 0 !important;
      z-index: 10 !important;
    }}
    table.dataTable tbody td {{
      padding: 6px 12px;
      border-bottom: 1px solid #2a2d36;
    }}
    table.dataTable tbody tr {{
      background: #1a1d24;
    }}
    table.dataTable tbody tr:hover {{
      background: #232730;
    }}
    table.dataTable tbody tr.liquidated {{
      background: rgba(214, 39, 40, 0.10);
    }}
    table.dataTable tbody tr.liquidated:hover {{
      background: rgba(214, 39, 40, 0.20);
    }}
    table.dataTable tbody tr.spotlight {{
      background: rgba(255, 200, 0, 0.07);
    }}
    table.dataTable tbody tr.spotlight:hover {{
      background: rgba(255, 200, 0, 0.14);
    }}
    .num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .num-positive {{
      color: #2ca02c;
    }}
    .num-negative {{
      color: #d62728;
    }}
    .badge {{
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 9px;
      font-weight: bold;
      letter-spacing: 0.5px;
    }}
    .badge-spotlight {{
      background: #ffc800;
      color: #000;
    }}
    .badge-liquidated {{
      background: #d62728;
      color: #fff;
    }}
    .dataTables_wrapper {{
      color: #d1d4dc;
    }}
    .dataTables_filter input,
    .dataTables_length select {{
      background: #1a1d24;
      color: #d1d4dc;
      border: 1px solid #383b45;
      padding: 4px 8px;
      border-radius: 3px;
    }}
    .dataTables_paginate .paginate_button {{
      color: #d1d4dc !important;
    }}
    .dataTables_paginate .paginate_button.current {{
      background: #2962ff !important;
      color: #fff !important;
      border: 0 !important;
    }}
    button.dt-button {{
      background: #2962ff !important;
      color: #fff !important;
      border: 0 !important;
      padding: 6px 12px !important;
      font-size: 11px !important;
      margin-bottom: 8px !important;
    }}
    .legend {{
      font-size: 11px;
      color: #888;
      margin-top: 12px;
    }}
    .legend .swatch {{
      display: inline-block;
      width: 12px;
      height: 12px;
      vertical-align: middle;
      margin-right: 6px;
      margin-left: 12px;
      border-radius: 2px;
    }}
    .swatch.liq {{ background: rgba(214, 39, 40, 0.30); }}
    .swatch.spot {{ background: rgba(255, 200, 0, 0.30); }}
  </style>
</head>
<body>
  <h1>Cross-sweep — {title}</h1>
  <div class="subtitle">
    Schema {schema_str} · {n_sweeps} sweeps combined
  </div>

  <div class="stats-bar">
    <div class="stat-item">
      <span class="stat-label">Sweeps:</span><span class="stat-value">{n_sweeps}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Combos:</span><span class="stat-value">{n_combos}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Grid:</span><span class="stat-value">{n_grid}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Spotlight:</span><span class="stat-value">{n_spot}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Liquidated:</span><span class="stat-value">{n_liq}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Best PnL:</span><span class="stat-value">{best_pnl}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Median DD:</span><span class="stat-value">{median_dd}</span>
    </div>
  </div>

  <table id="sweepTable" class="display compact" style="width: 100%">
    <thead>
      <tr>
{header_cells}
      </tr>
    </thead>
    <tbody>
{body_rows}
    </tbody>
  </table>

  <div class="legend">
    Click headers to sort · Shift-click for multi-column · Filter by Sweep column to drill into one
    <span class="swatch liq"></span>liquidated
    <span class="swatch spot"></span>spotlight (off-grid)
  </div>

  <script>
    $(document).ready(function() {{
      $('#sweepTable').DataTable({{
        order: [[ {sort_col_idx}, 'desc' ]],
        // No pagination — full sweep visible, scroll vertically with the
        // header pinned (CSS position: sticky on thead).
        paging: false,
        info: false,
        layout: {{
          topStart: ['buttons'],
        }},
        buttons: [
          {{
            extend: 'csv',
            text: 'Download CSV',
            filename: '{csv_filename}',
          }}
        ],
      }});
    }});
  </script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public: self-contained v2 tearsheet (replaces the broken NT tearsheet)
# ─────────────────────────────────────────────────────────────────────────────


def _fig_to_base64_png(fig, *, dpi: int = 110, transparent: bool = False) -> str:
    """Convert a matplotlib figure to a base64-encoded PNG data URI.

    Designed for embedding charts inline in self-contained HTML reports —
    no external image files, no relative-path issues across viewers.
    Uses a moderate DPI (110) to keep file sizes reasonable while staying
    crisp on retina displays.
    """
    import base64
    import io

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=dpi, bbox_inches="tight",
        facecolor="none" if transparent else fig.get_facecolor(),
    )
    plt.close(fig)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _render_equity_curve_png(
    account_report: pd.DataFrame,
    currency: str,
    *,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> str:
    """Event-time balance + drawdown chart as a base64 PNG.

    Optional ``exit_classification`` and ``account_liq_event`` overlays
    mirror the inline ``plot_equity_curve`` helper — vertical lines at
    every protective-stop / liquidation close, and a dashed band at
    the account-liq timestamp when applicable.
    """
    if account_report is None or account_report.empty:
        return ""
    equity = account_report["total"].astype(float).copy()
    equity = equity.groupby(equity.index).last().sort_index()
    peak = equity.cummax()
    drawdown_abs = peak - equity

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.patch.set_facecolor("#1a1d24")
    ax.set_facecolor("#1a1d24")
    equity.plot(ax=ax, color="#26a69a", linewidth=1.5, label="Equity")
    peak.plot(
        ax=ax, color="#888", linestyle="--", linewidth=1.0, label="Running peak",
    )
    ax.set_ylabel(f"Balance ({currency})", color="#d1d4dc")
    ax.tick_params(colors="#d1d4dc")
    ax.grid(True, alpha=0.15, color="#d1d4dc")
    for spine in ax.spines.values():
        spine.set_color("#383b45")

    ax2 = ax.twinx()
    ax2.fill_between(
        drawdown_abs.index, 0, drawdown_abs.values,
        color="#ef5350", alpha=0.30,
    )
    ax2.set_ylabel(f"Drawdown ({currency})", color="#ef5350")
    ax2.tick_params(axis="y", labelcolor="#ef5350")
    ax2.invert_yaxis()
    for spine in ax2.spines.values():
        spine.set_color("#383b45")

    # Close-cause overlay (forced exits) — small vertical lines on the
    # equity axis, distinct colours per cause.
    n_stops = n_liqs = 0
    if exit_classification is not None and not exit_classification.empty \
            and "close_cause" in exit_classification.columns:
        ts_index = pd.to_datetime(
            exit_classification["ts_closed"].astype("int64"), unit="ns", utc=True,
        )
        cls = exit_classification["close_cause"]
        for t in ts_index[cls == "protective_stop"]:
            ax.axvline(t, color=_PSTOP_COLOR, alpha=0.30, linewidth=0.6)
            n_stops += 1
        for t in ts_index[cls == "liquidation"]:
            ax.axvline(t, color=_LIQ_COLOR, alpha=0.55, linewidth=0.9)
            n_liqs += 1
        if n_stops:
            ax.plot([], [], color=_PSTOP_COLOR, alpha=0.6, linewidth=1.4,
                    label=f"Stop ({n_stops})")
        if n_liqs:
            ax.plot([], [], color=_LIQ_COLOR, alpha=0.8, linewidth=1.4,
                    label=f"Liq ({n_liqs})")

    # Account-liquidation marker.
    if account_liq_event:
        liq_ts_ns = account_liq_event.get("liq_ts")
        if liq_ts_ns is not None:
            x_liq = pd.Timestamp(int(liq_ts_ns), unit="ns", tz="UTC")
            ax.axvline(x_liq, color=_LIQ_COLOR, linewidth=2.0,
                       linestyle="--", label="ACCOUNT LIQ")

    ax.legend(loc="upper left", facecolor="#1a1d24", edgecolor="#383b45",
              labelcolor="#d1d4dc")
    ax.set_title(
        "Equity & drawdown (event-time, NOT daily MTM)",
        color="#d1d4dc", fontsize=11,
    )
    fig.tight_layout()
    return _fig_to_base64_png(fig)


def _render_trade_distributions_png(
    positions: list,
    currency: str,
    bar_interval_ns: int | None,
    *,
    exit_classification: pd.DataFrame | None = None,
) -> str:
    """Three-panel PnL/duration/concentration chart as a base64 PNG."""
    closed = [
        p for p in positions
        if getattr(p, "is_closed", False)
        and getattr(p, "realized_pnl", None) is not None
    ]
    if not closed:
        return ""
    pnls = np.array(
        [float(p.realized_pnl.as_decimal()) for p in closed], dtype=float,
    )
    durations_ns = np.array(
        [int(p.ts_closed) - int(p.ts_opened) for p in closed], dtype=float,
    )
    if bar_interval_ns:
        durations = durations_ns / bar_interval_ns
        dur_unit = "bars"
    else:
        durations = durations_ns / 1e9 / 86400
        dur_unit = "days"

    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    fig.patch.set_facecolor("#1a1d24")

    # Panel 1: PnL distribution
    ax = axes[0]
    ax.set_facecolor("#1a1d24")
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    bins = max(20, min(60, len(pnls) // 3))
    if len(wins):
        ax.hist(wins, bins=bins, color="#26a69a", alpha=0.7,
                edgecolor="#1a6e1a", label=f"Wins ({len(wins)})")
    if len(losses):
        ax.hist(losses, bins=bins, color="#ef5350", alpha=0.7,
                edgecolor="#891a1b", label=f"Losses ({len(losses)})")
    ax.axvline(pnls.mean(), color="#fff", linestyle="-", linewidth=1.0,
               label=f"Mean = {pnls.mean():,.0f}")
    ax.axvline(np.median(pnls), color="#fff", linestyle="--", linewidth=0.8,
               label=f"Median = {np.median(pnls):,.0f}")
    ax.axvline(0, color="#888", linestyle=":", linewidth=0.5)

    pstop_count = liq_count = 0
    if exit_classification is not None and not exit_classification.empty \
            and {"close_cause", "realized_pnl"}.issubset(exit_classification.columns):
        cls = exit_classification["close_cause"]
        pstop_pnls = exit_classification.loc[cls == "protective_stop", "realized_pnl"].astype(float).to_numpy()
        liq_pnls   = exit_classification.loc[cls == "liquidation",     "realized_pnl"].astype(float).to_numpy()
        pstop_count, liq_count = len(pstop_pnls), len(liq_pnls)
        y_top = ax.get_ylim()[1]
        if pstop_count:
            ax.scatter(pstop_pnls, np.full_like(pstop_pnls, y_top * 0.95),
                       marker="|", s=70, color=_PSTOP_COLOR,
                       label=f"Stops ({pstop_count})", clip_on=False)
        if liq_count:
            ax.scatter(liq_pnls, np.full_like(liq_pnls, y_top * 0.88),
                       marker="x", s=50, color=_LIQ_COLOR,
                       label=f"Liqs ({liq_count})", clip_on=False)

    ax.set_xlabel(f"Trade PnL ({currency})", color="#d1d4dc")
    ax.set_ylabel("# trades", color="#d1d4dc")
    title_extra = (
        f" · forced exits: {pstop_count} stop / {liq_count} liq"
        if (pstop_count or liq_count) else ""
    )
    ax.set_title(f"PnL distribution{title_extra}", color="#d1d4dc", fontsize=11)
    ax.legend(fontsize=8, facecolor="#1a1d24", edgecolor="#383b45",
              labelcolor="#d1d4dc")
    ax.tick_params(colors="#d1d4dc")
    ax.grid(True, alpha=0.15, color="#d1d4dc")
    for s in ax.spines.values():
        s.set_color("#383b45")

    # Panel 2: Duration distribution
    ax = axes[1]
    ax.set_facecolor("#1a1d24")
    bins = max(20, min(60, len(durations) // 3))
    ax.hist(durations, bins=bins, color="#2962ff", alpha=0.7, edgecolor="#0f4c81")
    ax.axvline(durations.mean(), color="#fff", linestyle="-", linewidth=1.0,
               label=f"Mean = {durations.mean():.1f} {dur_unit}")
    ax.axvline(np.median(durations), color="#fff", linestyle="--", linewidth=0.8,
               label=f"Median = {np.median(durations):.1f} {dur_unit}")
    ax.set_xlabel(f"Duration ({dur_unit})", color="#d1d4dc")
    ax.set_ylabel("# trades", color="#d1d4dc")
    ax.set_title("Trade duration", color="#d1d4dc", fontsize=11)
    ax.legend(fontsize=8, facecolor="#1a1d24", edgecolor="#383b45",
              labelcolor="#d1d4dc")
    ax.tick_params(colors="#d1d4dc")
    ax.grid(True, alpha=0.15, color="#d1d4dc")
    for s in ax.spines.values():
        s.set_color("#383b45")

    # Panel 3: Concentration
    ax = axes[2]
    ax.set_facecolor("#1a1d24")
    sorted_desc = np.sort(pnls)[::-1]
    sorted_asc = np.sort(pnls)
    sum_abs_total = float(np.sum(np.abs(pnls)))
    if sum_abs_total <= 0:
        sum_abs_total = 1e-9
    labels = ["Top 1", "Top 3", "Top 5", "Bot 1", "Bot 3", "Bot 5"]
    values = []
    for n in (1, 3, 5):
        values.append(
            float(np.sum(sorted_desc[: min(n, len(sorted_desc))]))
            / sum_abs_total * 100,
        )
    for n in (1, 3, 5):
        values.append(
            float(np.sum(sorted_asc[: min(n, len(sorted_asc))]))
            / sum_abs_total * 100,
        )
    colors = ["#26a69a"] * 3 + ["#ef5350"] * 3
    bars = ax.bar(labels, values, color=colors, alpha=0.75, edgecolor="#383b45")
    for b, v in zip(bars, values, strict=False):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + (1 if v >= 0 else -1),
            f"{v:.1f}%",
            ha="center", va="bottom" if v >= 0 else "top",
            fontsize=8, color="#d1d4dc",
        )
    ax.axhline(0, color="#888", linewidth=0.5)
    ax.set_ylabel("% of total |PnL|", color="#d1d4dc")
    ax.set_title("Trade-PnL concentration", color="#d1d4dc", fontsize=11)
    ax.tick_params(colors="#d1d4dc")
    ax.grid(True, alpha=0.15, color="#d1d4dc", axis="y")
    for s in ax.spines.values():
        s.set_color("#383b45")
    if values[1] > 50:
        ax.text(
            0.5, 0.95, "⚠️ top-3 wins > 50% of total |PnL|",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=8, color="#000",
            bbox={"facecolor": "#ffc800", "alpha": 0.9, "edgecolor": "#b35900"},
        )

    fig.tight_layout()
    return _fig_to_base64_png(fig)


def _render_yearly_bars_png(yearly_df: pd.DataFrame, currency: str) -> str:
    """Yearly PnL bars chart as a base64 PNG."""
    if yearly_df is None or yearly_df.empty:
        return ""
    years = yearly_df.index.tolist()
    pnls = yearly_df["pnl"].tolist()
    n_pos = yearly_df["num_positions"].tolist()

    fig, ax = plt.subplots(figsize=(12, 3.5))
    fig.patch.set_facecolor("#1a1d24")
    ax.set_facecolor("#1a1d24")
    colors = ["#26a69a" if v > 0 else "#ef5350" for v in pnls]
    bars = ax.bar(
        [str(y) for y in years], pnls, color=colors,
        alpha=0.75, edgecolor="#383b45",
    )
    for b, v, n in zip(bars, pnls, n_pos, strict=False):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + (max(abs(p) for p in pnls) * 0.02 if v >= 0 else -max(abs(p) for p in pnls) * 0.02),
            f"{v:,.0f}\n({int(n)})",
            ha="center", va="bottom" if v >= 0 else "top",
            fontsize=8, color="#d1d4dc",
        )
    ax.axhline(0, color="#888", linewidth=0.6)
    ax.set_ylabel(f"PnL ({currency})", color="#d1d4dc")
    ax.set_title("Yearly PnL (trade count in parens)",
                 color="#d1d4dc", fontsize=11)
    ax.tick_params(colors="#d1d4dc")
    ax.grid(True, alpha=0.15, color="#d1d4dc", axis="y")
    for s in ax.spines.values():
        s.set_color("#383b45")
    fig.tight_layout()
    return _fig_to_base64_png(fig)


def _format_metric(value: Any, kind: str = "auto") -> str:
    """Format a single metric value for the tearsheet stats grid."""
    import math as _math
    from decimal import Decimal as _Decimal
    if value is None:
        return "—"
    if isinstance(value, (int, _Decimal)):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
    elif isinstance(value, float):
        f = value
    else:
        return str(value)
    if _math.isnan(f):
        return "—"
    if _math.isinf(f):
        return "∞"
    if kind == "money":
        return f"{f:,.2f}"
    if kind == "pct_frac":
        return f"{f * 100:.2f}%"
    if kind == "pct":
        return f"{f:.2f}%"
    if kind == "ratio":
        return f"{f:.2f}"
    if kind == "int":
        return f"{int(f):,}"
    # auto: ints stay ints, decimals to 2dp
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:.2f}"


def generate_v2_tearsheet(
    positions: list,
    account_report: pd.DataFrame,
    bars: list,
    *,
    starting_capital: float,
    currency: str = "USDC",
    instrument_label: str = "",
    bar_interval: str = "",
    strategy_label: str = "",
    title: str | None = None,
    output_dir: str | Path | None = None,
    filename: str | None = None,
    open_browser: bool = False,
    yearly_df: pd.DataFrame | None = None,
    regime_df: pd.DataFrame | None = None,
    baselines: dict[str, Any] | None = None,
    strategy_pnl: float | None = None,
    liquidated: bool = False,
    liquidated_at: str | None = None,
    leverage: int | float = 1,
    fee_rate: float | None = None,
    exit_classification: pd.DataFrame | None = None,
    account_liq_event: dict[str, Any] | None = None,
) -> Path:
    """Self-contained HTML tearsheet using only trustworthy v2 metrics.

    Replaces NT's ``create_tearsheet`` (which is built on the
    upstream-broken returns methodology — see
    ``docs/ANALYZER_RETURNS_CAVEAT.md``).  Composes the components
    we already use elsewhere in the strategy notebook into one
    archivable HTML file.

    All numbers are derived from realized PnL on closed positions and
    event-time balance snapshots — both faithful to NT's ground truth.
    No Sharpe / Sortino / Vol / Returns Profit Factor / Risk Return
    Ratio anywhere on the page.

    Sections:

    1. **Header** — strategy + instrument, time range, run timestamp
    2. **Key metrics grid** — total PnL, win rate, expectancy, payoff,
       PnL profit factor, max drawdown ($ and %), MAR, recovery factor,
       CAGR, total fees, fee-pct-of-PnL
    3. **Equity & drawdown chart** — event-time, with running peak
    4. **Trade distributions chart** — PnL / duration / concentration
    5. **Yearly breakdown table** — per-year PnL, win rate, profit
       factor, largest win/loss
    6. **Regime breakdown table** (if provided)
    7. **Baselines comparison** (if provided)
    8. **Caveat footer** — what's missing pending upstream fix

    Embeds all charts as base64 PNGs — fully portable, no relative
    path or CDN issues.

    Parameters
    ----------
    positions
        List of NT Position objects.
    account_report
        DataFrame from ``engine.trader.generate_account_report(venue)``.
    bars
        List of NT Bar objects.
    starting_capital, currency
        Used in the metrics computation and labels.
    instrument_label, bar_interval, strategy_label
        Strings shown in the header.
    title
        Custom suptitle.  Defaults to a derived label.
    output_dir
        Directory to write to.  Default ``reports/tearsheets/``.
    filename
        Custom filename stem.  Default derived from labels + timestamp.
    open_browser
        If True, opens the generated HTML in the default browser.
    yearly_df
        Output of ``performance_by_year``.  Optional.
    regime_df
        Output of ``performance_by_regime``.  Optional.
    baselines
        Dict with keys ``buy_and_hold`` (output of ``buy_and_hold(...)``)
        and ``random_entry`` (output of ``random_entry_baseline(...)``).
        Optional.
    strategy_pnl
        Total PnL of the strategy (for baselines comparison).  If not
        provided, computed from positions.
    liquidated, liquidated_at
        If True, render a prominent liquidation banner at the top.
    leverage
        Strategy leverage label (header).
    fee_rate
        Settlement-currency taker fee rate.  Used to populate
        fee-pct-of-PnL when ``total_fees`` is computed.

    Returns
    -------
    pathlib.Path
        Absolute path to the generated HTML file.

    """
    from src.backtesting.metrics import (
        TradeRecord,
        compute_all_metrics,
    )

    # ── Build metrics ────────────────────────────────────────────────────
    closed = [
        p for p in positions
        if getattr(p, "is_closed", False)
        and getattr(p, "realized_pnl", None) is not None
    ]
    trades = [
        TradeRecord(
            pnl=float(p.realized_pnl.as_decimal()),
            ts_opened_ns=int(p.ts_opened),
            ts_closed_ns=int(p.ts_closed),
            side="LONG" if p.entry.name == "BUY" else "SHORT",
        )
        for p in closed
    ]
    bar_interval_ns = (
        int(bars[1].ts_event - bars[0].ts_event)
        if len(bars) > 1 else None
    )
    balance = (
        account_report["total"].astype(float)
        if account_report is not None and not account_report.empty
        else pd.Series(dtype=float)
    )

    # Sum PnL from realized trades for the activity metrics input
    total_pnl_calc = sum(t.pnl for t in trades) if trades else 0.0
    if strategy_pnl is None:
        strategy_pnl = total_pnl_calc

    # Compute fees from positions (sum of all commissions in settlement currency)
    total_fees: float = 0.0
    for p in positions:
        for comm in (p.commissions() if callable(getattr(p, "commissions", None)) else []):
            try:
                if str(comm.currency) == currency:
                    total_fees += float(comm.as_decimal())
            except Exception:
                continue

    metrics = compute_all_metrics(
        trades, balance,
        starting_capital=float(starting_capital),
        total_bars=len(bars) if bars else None,
        bar_interval_ns=bar_interval_ns,
        first_bar_ts_ns=int(bars[0].ts_event) if bars else None,
        last_bar_ts_ns=int(bars[-1].ts_event) if bars else None,
        total_fees=total_fees,
        total_pnl=float(strategy_pnl),
    )

    # ── Resolve output path ──────────────────────────────────────────────
    # Naming convention (matches generate_backtest_html):
    #   • If user passes a stem (no .html), append "_{ts}.html" — snapshot
    #     mode, accumulates across runs of the same config.
    #   • If user passes a full filename ending in ".html", use verbatim —
    #     deterministic mode, overwrites on re-run.
    #   • If filename is None, fall back to a derived stem from
    #     ``strategy_label`` plus timestamp.
    from datetime import datetime, timezone
    if output_dir is None:
        proj_root = Path(__file__).resolve().parent.parent
        output_dir = proj_root / "reports" / "tearsheets"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if filename is None:
        safe_label = (
            (strategy_label or "tearsheet")
            .replace(" ", "_").replace("/", "-").replace("|", "-")
        )
        filename = f"v2_tearsheet_{safe_label}_{ts}.html"
    elif not filename.endswith(".html"):
        # User-supplied stem → append timestamp + extension.
        filename = f"{filename}_{ts}.html"
    out_path = output_dir / filename

    # ── Render charts to base64 PNGs ─────────────────────────────────────
    equity_png = _render_equity_curve_png(
        account_report, currency,
        exit_classification=exit_classification,
        account_liq_event=account_liq_event,
    )
    trade_dist_png = _render_trade_distributions_png(
        positions, currency, bar_interval_ns,
        exit_classification=exit_classification,
    )
    yearly_png = _render_yearly_bars_png(yearly_df, currency) if yearly_df is not None else ""

    # ── Build header info ────────────────────────────────────────────────
    if title is None:
        title = (
            f"{strategy_label} — {instrument_label} {bar_interval}"
            if strategy_label
            else "v2 Tearsheet"
        )

    bar_start = (
        pd.Timestamp(bars[0].ts_event, unit="ns", tz="UTC").strftime("%Y-%m-%d")
        if bars else "—"
    )
    bar_end = (
        pd.Timestamp(bars[-1].ts_event, unit="ns", tz="UTC").strftime("%Y-%m-%d")
        if bars else "—"
    )
    n_bars = len(bars) if bars else 0
    n_trades = len(trades)
    final_balance = float(balance.iloc[-1]) if not balance.empty else float("nan")
    ending_str = _format_metric(final_balance, "money")

    # ── Build metric grid HTML ───────────────────────────────────────────
    def metric_card(label: str, value: str, sublabel: str = "",
                    color_class: str = "") -> str:
        sub_html = f'<div class="card-sub">{sublabel}</div>' if sublabel else ""
        return (
            f'<div class="card {color_class}">'
            f'<div class="card-label">{label}</div>'
            f'<div class="card-value">{value}</div>'
            f'{sub_html}</div>'
        )

    pnl_color = "good" if total_pnl_calc > 0 else ("bad" if total_pnl_calc < 0 else "")
    drawdown_color = (
        "bad" if metrics.get("max_drawdown_pct", 0) > 0.30 else
        "warn" if metrics.get("max_drawdown_pct", 0) > 0.15 else ""
    )
    metrics_grid = "\n".join([
        metric_card(
            "Total PnL", _format_metric(total_pnl_calc, "money"),
            f"{currency}", pnl_color,
        ),
        metric_card(
            "Trades", _format_metric(n_trades, "int"),
            f"{_format_metric(metrics.get('win_rate'), 'pct_frac')} win rate",
        ),
        metric_card(
            "Avg PnL/trade",
            _format_metric(metrics.get("avg_pnl_per_trade"), "money"),
            f"Expectancy = {_format_metric(metrics.get('expectancy'), 'money')}",
        ),
        metric_card(
            "Profit Factor",
            _format_metric(metrics.get("pnl_profit_factor"), "ratio"),
            "gross_wins / |gross_losses|",
        ),
        metric_card(
            "Payoff",
            _format_metric(metrics.get("payoff_ratio"), "ratio"),
            "avg_win / |avg_loss|",
        ),
        metric_card(
            "Max DD %",
            _format_metric(metrics.get("max_drawdown_pct"), "pct_frac"),
            f"{_format_metric(metrics.get('max_drawdown_abs'), 'money')} {currency}",
            drawdown_color,
        ),
        metric_card(
            "MAR ratio",
            _format_metric(metrics.get("mar_ratio"), "ratio"),
            "CAGR / MaxDD%",
        ),
        metric_card(
            "Recovery",
            _format_metric(metrics.get("recovery_factor"), "ratio"),
            "PnL / MaxDD$",
        ),
        metric_card(
            "CAGR",
            _format_metric(metrics.get("cagr"), "pct_frac"),
            f"over {_format_metric(metrics.get('years_in_sample'), 'ratio')} years",
        ),
        metric_card(
            "In market",
            _format_metric(metrics.get("bars_in_market_pct"), "pct_frac"),
            "of bars",
        ),
        metric_card(
            "Fees",
            _format_metric(total_fees, "money"),
            f"{_format_metric(metrics.get('fee_pct_of_pnl'), 'pct_frac')} of PnL",
        ),
        metric_card(
            "Long / Short PnL",
            (
                f"{_format_metric(metrics.get('long_pnl'), 'money')} / "
                f"{_format_metric(metrics.get('short_pnl'), 'money')}"
            ),
            f"{_format_metric(metrics.get('num_long'), 'int')} L / "
            f"{_format_metric(metrics.get('num_short'), 'int')} S",
        ),
        metric_card(
            "Largest W / L",
            (
                f"{_format_metric(metrics.get('largest_win'), 'money')} / "
                f"{_format_metric(metrics.get('largest_loss'), 'money')}"
            ),
            f"max consec L: {_format_metric(metrics.get('max_consec_losers'), 'int')}",
        ),
    ])

    # ── Liquidation banner ───────────────────────────────────────────────
    liq_banner = ""
    if liquidated:
        ts_str = (
            f" at {liquidated_at}" if liquidated_at else ""
        )
        liq_banner = (
            f'<div class="liq-banner">⚠️ ACCOUNT LIQUIDATED{ts_str} '
            f'— equity hit zero or below during the run.</div>'
        )

    # ── Yearly table ─────────────────────────────────────────────────────
    yearly_html = ""
    if yearly_df is not None and not yearly_df.empty:
        rows: list[str] = []
        for year in yearly_df.index:
            row = yearly_df.loc[year]
            cls = "good" if row.get("pnl", 0) > 0 else "bad"
            rows.append(
                f'<tr><td>{int(year)}</td>'
                f'<td class="num {cls}">{_format_metric(row.get("pnl"), "money")}</td>'
                f'<td class="num">{_format_metric(row.get("pnl_pct"), "pct")}</td>'
                f'<td class="num">{_format_metric(row.get("num_positions"), "int")}</td>'
                f'<td class="num">{_format_metric(row.get("win_rate"), "pct_frac")}</td>'
                f'<td class="num">{_format_metric(row.get("profit_factor"), "ratio")}</td>'
                f'<td class="num good">{_format_metric(row.get("largest_win"), "money")}</td>'
                f'<td class="num bad">{_format_metric(row.get("largest_loss"), "money")}</td>'
                f'</tr>',
            )
        yearly_html = (
            '<section class="card-section"><h2>Yearly breakdown</h2>'
            '<table class="data-table"><thead><tr>'
            '<th>Year</th><th class="r">PnL</th><th class="r">PnL %</th>'
            '<th class="r">Trades</th><th class="r">Win Rate</th>'
            '<th class="r">PF</th><th class="r">Largest W</th>'
            '<th class="r">Largest L</th>'
            '</tr></thead><tbody>'
            + "\n".join(rows)
            + '</tbody></table></section>'
        )

    # ── Regime table ─────────────────────────────────────────────────────
    regime_html = ""
    if regime_df is not None and not regime_df.empty:
        rows = []
        for _idx, row in regime_df.iterrows():
            cls = "good" if row.get("pnl", 0) > 0 else "bad"
            rows.append(
                f'<tr><td>{html.escape(str(row.get("regime", "")))}</td>'
                f'<td class="num">{_format_metric(row.get("num_positions"), "int")}</td>'
                f'<td class="num {cls}">{_format_metric(row.get("pnl"), "money")}</td>'
                f'<td class="num">{_format_metric(row.get("win_rate"), "pct_frac")}</td>'
                f'<td class="num">{_format_metric(row.get("profit_factor"), "ratio")}</td>'
                f'<td class="num">{_format_metric(row.get("avg_winner"), "money")}</td>'
                f'<td class="num">{_format_metric(row.get("avg_loser"), "money")}</td>'
                f'</tr>',
            )
        regime_html = (
            '<section class="card-section"><h2>Regime breakdown</h2>'
            '<table class="data-table"><thead><tr>'
            '<th>Regime</th><th class="r">Trades</th><th class="r">PnL</th>'
            '<th class="r">Win Rate</th><th class="r">PF</th>'
            '<th class="r">Avg Win</th><th class="r">Avg Loss</th>'
            '</tr></thead><tbody>'
            + "\n".join(rows)
            + '</tbody></table></section>'
        )

    # ── Baselines section ────────────────────────────────────────────────
    baselines_html = ""
    if baselines:
        bh = baselines.get("buy_and_hold")
        re_dist = baselines.get("random_entry")
        rows = []
        rows.append(
            f'<tr><td><b>Strategy</b></td>'
            f'<td class="num {"good" if strategy_pnl > 0 else "bad"}">'
            f'{_format_metric(strategy_pnl, "money")}</td>'
            f'<td class="num">—</td><td class="num">—</td></tr>',
        )
        if bh:
            bh_pnl = bh.get("pnl", float("nan"))
            cls = "good" if bh_pnl and bh_pnl > 0 else "bad"
            rows.append(
                f'<tr><td>Buy & Hold (spot)</td>'
                f'<td class="num {cls}">{_format_metric(bh_pnl, "money")}</td>'
                f'<td class="num">{_format_metric(bh.get("max_drawdown_pct"), "pct_frac")}</td>'
                f'<td class="num">{_format_metric(bh.get("cagr"), "pct_frac")}</td></tr>',
            )
        if re_dist:
            rows.append(
                f'<tr><td>Random entry (median)</td>'
                f'<td class="num">{_format_metric(re_dist.get("median_pnl"), "money")}</td>'
                f'<td class="num">5th: {_format_metric(re_dist.get("pct_5"), "money")}</td>'
                f'<td class="num">95th: {_format_metric(re_dist.get("pct_95"), "money")}</td>'
                f'</tr>',
            )
        baselines_html = (
            '<section class="card-section"><h2>Baselines comparison</h2>'
            '<table class="data-table"><thead><tr>'
            '<th></th><th class="r">PnL</th>'
            '<th class="r">MaxDD% / 5th pct</th>'
            '<th class="r">CAGR / 95th pct</th>'
            '</tr></thead><tbody>'
            + "\n".join(rows)
            + '</tbody></table></section>'
        )

    # Combine yearly chart + table into a single section block.
    yearly_section = ""
    if yearly_html:
        chart_html = (
            f'<div class="chart"><img src="{yearly_png}" alt="Yearly PnL" /></div>'
            if yearly_png else ""
        )
        yearly_section = f"{chart_html}\n{yearly_html}"

    # ── Close-cause section (forced exits) ───────────────────────────────
    # Only renders when ``exit_classification`` carries at least one
    # forced-exit row; pure strategy_exit runs skip the section since
    # there's nothing notable to show.
    close_cause_html = ""
    cause_total = 0
    if exit_classification is not None and not exit_classification.empty \
            and "close_cause" in exit_classification.columns:
        cause_groups = (
            exit_classification.groupby("close_cause")["realized_pnl"]
            .agg(["count", "sum"]).reset_index()
        )
        # Sort: strategy_exit first, then forced exits.
        cause_order = {"strategy_exit": 0, "protective_stop": 1, "liquidation": 2}
        cause_groups["_order"] = cause_groups["close_cause"].map(
            lambda c: cause_order.get(c, 99),
        )
        cause_groups = cause_groups.sort_values("_order").drop(columns="_order")
        forced_count = int(
            cause_groups.loc[
                cause_groups["close_cause"] != "strategy_exit", "count",
            ].sum(),
        )
        cause_total = forced_count
        if forced_count > 0:
            cause_label = {
                "strategy_exit":   "Strategy exit",
                "protective_stop": "Protective stop",
                "liquidation":     "Liquidation",
            }
            rows = []
            for _, r in cause_groups.iterrows():
                cause = str(r["close_cause"])
                cnt = int(r["count"])
                pnl = float(r["sum"])
                cls = "good" if pnl > 0 else ("bad" if pnl < 0 else "")
                rows.append(
                    f'<tr><td>{html.escape(cause_label.get(cause, cause))}</td>'
                    f'<td class="num">{cnt}</td>'
                    f'<td class="num {cls}">{_format_metric(pnl, "money")}</td>'
                    f'<td class="num">{_format_metric(pnl / cnt if cnt else 0, "money")}</td>'
                    f'</tr>',
                )
            close_cause_html = (
                '<section class="card-section"><h2>Close causes</h2>'
                '<table class="data-table"><thead><tr>'
                '<th>Cause</th><th class="r">Trades</th>'
                '<th class="r">Total PnL</th><th class="r">Avg PnL/trade</th>'
                '</tr></thead><tbody>'
                + "\n".join(rows)
                + '</tbody></table></section>'
            )

    # ── Account-liquidation banner (separate from per-position liq banner) ─
    account_liq_banner = ""
    if account_liq_event:
        liq_iso = account_liq_event.get("liq_ts_iso") or "unknown"
        eq_at = _decimal_to_float(account_liq_event.get("equity_at_liq"))
        eq_before = _decimal_to_float(account_liq_event.get("equity_before"))
        culprit_n = len(account_liq_event.get("culprit_position_ids", []) or [])
        eq_at_str = _format_metric(eq_at, "money") if eq_at is not None else "—"
        eq_bef_str = _format_metric(eq_before, "money") if eq_before is not None else "—"
        account_liq_banner = (
            f'<div class="liq-banner">⚠️ ACCOUNT LIQUIDATED at {html.escape(liq_iso)} '
            f'— equity {eq_bef_str} → {eq_at_str} ({culprit_n} open position(s) at the moment of halt).'
            f'</div>'
        )

    # ── Build full HTML ──────────────────────────────────────────────────
    # cause_total kept for potential future use (skip-section gating).
    del cause_total
    html_doc = _V2_TEARSHEET_TEMPLATE.format(
        title=html.escape(title),
        strategy_label=html.escape(strategy_label or "—"),
        instrument_label=html.escape(instrument_label or "—"),
        bar_interval=html.escape(str(bar_interval) or "—"),
        leverage=html.escape(str(leverage)),
        bar_start=bar_start, bar_end=bar_end,
        n_bars=f"{n_bars:,}",
        starting_capital=_format_metric(starting_capital, "money"),
        ending_balance=ending_str,
        currency=html.escape(currency),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        liq_banner=liq_banner,
        account_liq_banner=account_liq_banner,
        metrics_grid=metrics_grid,
        equity_img=(
            f'<img src="{equity_png}" alt="Equity & drawdown" />'
            if equity_png else "<p>No balance data.</p>"
        ),
        trade_dist_img=(
            f'<img src="{trade_dist_png}" alt="Trade distributions" />'
            if trade_dist_png else "<p>No closed trades.</p>"
        ),
        yearly_section=yearly_section,
        regime_html=regime_html,
        baselines_html=baselines_html,
        close_cause_html=close_cause_html,
    )

    out_path.write_text(html_doc, encoding="utf-8")
    print(f"✓ v2 tearsheet written → {out_path}")

    if open_browser:
        webbrowser.open(out_path.as_uri())

    return out_path


_V2_TEARSHEET_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #0f1116; color: #d1d4dc; margin: 0; padding: 24px;
  }}
  h1 {{ font-size: 20px; margin: 0 0 6px 0; color: #fff; }}
  h2 {{ font-size: 14px; margin: 24px 0 8px 0; color: #fff;
        border-bottom: 1px solid #383b45; padding-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 12px; margin-bottom: 16px; }}
  .meta-row {{ display: flex; gap: 24px; flex-wrap: wrap;
                background: #1a1d24; padding: 12px 16px; border-radius: 6px;
                font-size: 12px; margin-bottom: 16px; }}
  .meta-item {{ }}
  .meta-label {{ color: #888; margin-right: 6px; }}
  .meta-value {{ color: #fff; font-weight: bold; }}

  .liq-banner {{
    background: rgba(214, 39, 40, 0.20); color: #ff6b6b;
    padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
    font-weight: bold; border-left: 4px solid #d62728;
  }}

  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .card {{
    background: #1a1d24; padding: 12px 16px; border-radius: 6px;
    border-left: 3px solid #2962ff;
  }}
  .card.good {{ border-left-color: #26a69a; }}
  .card.bad {{ border-left-color: #ef5350; }}
  .card.warn {{ border-left-color: #ffc800; }}
  .card-label {{ color: #888; font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.5px; }}
  .card-value {{ color: #fff; font-size: 17px; font-weight: bold;
                  font-variant-numeric: tabular-nums; margin-top: 2px; }}
  .card-sub {{ color: #888; font-size: 10px; margin-top: 2px; }}
  .card.good .card-value {{ color: #26a69a; }}
  .card.bad .card-value {{ color: #ef5350; }}
  .card.warn .card-value {{ color: #ffc800; }}

  .chart {{ background: #1a1d24; border-radius: 6px; padding: 8px;
            margin-bottom: 16px; }}
  .chart img {{ width: 100%; height: auto; display: block; }}

  .data-table {{
    width: 100%; border-collapse: collapse; font-size: 12px;
    background: #1a1d24; border-radius: 6px; overflow: hidden;
    font-variant-numeric: tabular-nums;
  }}
  .data-table th {{
    background: #232730; padding: 10px 12px; text-align: left;
    font-weight: bold; color: #fff; border-bottom: 1px solid #383b45;
  }}
  .data-table th.r {{ text-align: right; }}
  .data-table td {{ padding: 8px 12px; border-bottom: 1px solid #2a2d36; }}
  .data-table td.num {{ text-align: right; font-family: "Menlo", "Monaco", monospace; }}
  .data-table td.good {{ color: #26a69a; }}
  .data-table td.bad {{ color: #ef5350; }}
  .data-table tbody tr:hover {{ background: #232730; }}

  footer {{
    margin-top: 32px; padding: 16px; background: #232730;
    border-radius: 6px; font-size: 11px; color: #888;
    border-left: 3px solid #ffc800;
  }}
  footer a {{ color: #2962ff; }}
</style>
</head>
<body>

<h1>{title}</h1>
<div class="subtitle">v2 tearsheet · trustworthy methodology · generated {generated_at}</div>

<div class="meta-row">
  <div class="meta-item">
    <span class="meta-label">Strategy:</span><span class="meta-value">{strategy_label}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Instrument:</span><span class="meta-value">{instrument_label}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Interval:</span><span class="meta-value">{bar_interval}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Leverage:</span><span class="meta-value">{leverage}x</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Bars:</span><span class="meta-value">{n_bars}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Range:</span><span class="meta-value">{bar_start} → {bar_end}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Starting:</span><span class="meta-value">{starting_capital} {currency}</span>
  </div>
  <div class="meta-item">
    <span class="meta-label">Ending:</span><span class="meta-value">{ending_balance} {currency}</span>
  </div>
</div>

{liq_banner}
{account_liq_banner}

<h2>Key metrics</h2>
<div class="grid">
{metrics_grid}
</div>

<h2>Equity & drawdown</h2>
<div class="chart">
{equity_img}
</div>

<h2>Trade distributions</h2>
<div class="chart">
{trade_dist_img}
</div>

{close_cause_html}

{yearly_section}

{regime_html}

{baselines_html}

<footer>
  <strong>About this tearsheet:</strong> only trustworthy stats are
  shown.  The following are deliberately omitted because NT's
  <code>_calculate_portfolio_returns</code> uses a zero-padded daily
  series via <code>.ffill().pct_change()</code> that biases all
  returns-derived metrics for sparse-trade strategies:<br>
  <em>Sharpe Ratio · Sortino Ratio · Returns Volatility ·
  Returns-based Profit Factor · Average Return · Risk Return Ratio ·
  Monthly Returns heatmap · Returns Distribution histogram ·
  Rolling Sharpe Ratio</em><br>
  These will return when upstream lands a daily-MTM equity fix —
  the project's sweep schema (<code>SWEEP_SCHEMA_VERSION</code>)
  will bump to v3 at that point.  See
  <code>docs/ANALYZER_RETURNS_CAVEAT.md</code> for the methodology
  audit.
</footer>

</body>
</html>
"""
