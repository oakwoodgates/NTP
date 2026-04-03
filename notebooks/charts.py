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

import json
import math
import textwrap
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports" / "backtest"
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nautilus_trader.model.currencies import USDC
from nautilus_trader.indicators import (
    BollingerBands,
    ExponentialMovingAverage,
    MovingAverageConvergenceDivergence,
    RelativeStrengthIndex,
    SimpleMovingAverage,
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

# ── Flag constants ────────────────────────────────────────────────────────
_FLAG_BG   = "#eeeeee"
_FLAG_TEXT = "#777777"

# ── MA class dispatch ────────────────────────────────────────────────────────

_MA_CLASSES = {"EMA": ExponentialMovingAverage, "SMA": SimpleMovingAverage}


# ── Public API ────────────────────────────────────────────────────────────────

def plot_ema_cross(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    instrument_label: str = "",
    bar_label: str = "1h",
    height: int = 600,
) -> go.Figure:
    """Candlestick chart with EMA overlays and trade entry markers."""
    ohlcv = _bars_to_ma_ohlcv(bars, fast_period, slow_period, ma_type="EMA")
    buys, sells = _parse_fills(fills_report)

    fig = go.Figure()
    _add_candlesticks(fig, ohlcv)
    _add_ma_lines(fig, ohlcv, fast_period, slow_period, ma_type="EMA")
    _add_trade_markers(fig, buys, sells, ohlcv)
    title = f"{instrument_label} · {bar_label} · EMACross({fast_period}/{slow_period})"
    _apply_base_layout(fig, title, height)
    return fig


def plot_sma_cross(
    bars: list[Bar],
    fills_report: pd.DataFrame,
    fast_period: int,
    slow_period: int,
    *,
    instrument_label: str = "BTC-USD-PERP",
    bar_label: str = "1h",
    height: int = 600,
) -> go.Figure:
    """Candlestick chart with SMA overlays and trade entry markers."""
    ohlcv = _bars_to_ma_ohlcv(bars, fast_period, slow_period, ma_type="SMA")
    buys, sells = _parse_fills(fills_report)

    fig = go.Figure()
    _add_candlesticks(fig, ohlcv)
    _add_ma_lines(fig, ohlcv, fast_period, slow_period, ma_type="SMA")
    _add_trade_markers(fig, buys, sells, ohlcv)
    title = f"{instrument_label} · {bar_label} · SMACross({fast_period}/{slow_period})"
    _apply_base_layout(fig, title, height)
    return fig


# ── Private helpers ───────────────────────────────────────────────────────────

def _bars_to_ma_ohlcv(
    bars: list[Bar],
    fast_period: int,
    slow_period: int,
    ma_type: str = "EMA",
) -> pd.DataFrame:
    """Convert NT Bar list to OHLCV DataFrame with MA columns appended."""
    ma_cls = _MA_CLASSES[ma_type]
    fast_ma = ma_cls(fast_period)
    slow_ma = ma_cls(slow_period)

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


# ── Matplotlib display helpers ───────────────────────────────────────────────


def plot_equity_curve(
    analyzer,
    account_report: pd.DataFrame | None,
    title: str,
) -> None:
    """Plot cumulative returns or account balance fallback.

    Calls ``plt.show()`` directly — designed for inline notebook use.

    Parameters
    ----------
    analyzer
        NT portfolio analyzer (after ``calculate_statistics`` has been called).
    account_report
        DataFrame from ``engine.trader.generate_account_report(venue)``,
        or ``None``. Used as fallback when analyzer returns are empty.
    title
        Chart title string (e.g. ``"EMACross(20/50)  BTC 1h"``).

    """
    _, ax = plt.subplots(figsize=(14, 5))
    plotted = False

    try:
        returns = analyzer.returns()
        if returns is not None and len(returns) > 0:
            cumulative = (1 + returns).cumprod()
            cumulative.plot(ax=ax, label="Cumulative Return")
            plotted = True
    except Exception:
        pass

    if not plotted and account_report is not None and not account_report.empty:
        account_report.plot(ax=ax, label="Account Balance")
        ax.set_ylabel("Balance (USDC)")
        plotted = True

    if plotted:
        ax.set_title(f"Equity Curve — {title}", fontsize=13)
        ax.set_xlabel("Time")
        ax.grid(True, alpha=0.2)
        ax.legend()
        plt.tight_layout()
        plt.show()
    else:
        print("No returns or account data available for equity curve.")


def print_summary_stats(
    analyzer,
    num_positions: int | None = None,
    currency=USDC,
) -> None:
    """Print general, PnL, and returns stats from the analyzer.

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
    returns_stats = analyzer.get_performance_stats_returns()

    print("=== General ===")
    for k, v in general_stats.items():
        print(f"  {k}: {v}")

    print("\n=== PnL (USDC) ===")
    for k, v in pnl_stats.items():
        print(f"  {k}: {v}")

    print("\n=== Returns ===")
    for k, v in returns_stats.items():
        print(f"  {k}: {v}")

    print(f"\nTotal PnL      : {analyzer.total_pnl(currency)}")
    print(f"Total PnL %    : {analyzer.total_pnl_percentage(currency)}")
    if num_positions is not None:
        print(f"Positions      : {num_positions}")


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
    flag_col: str | None = "error",
    flag_value: str = "liquidated",
    flag_label: str = "hit zero equity",
) -> None:
    """Diverging RdYlGn heatmap from sweep results DataFrame.

    Calls ``plt.show()`` directly — designed for inline notebook use.

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
        Column containing error/flag strings. Cells matching *flag_value*
        are underlined to indicate unreliable results. Set to ``None``
        to disable flagging. Default ``"error"``.
    flag_value
        The string value in *flag_col* that triggers the underline.
        Default ``"liquidated"``.
    flag_label
        Legend label for the underline marker. Default ``"hit zero equity"``.

    """
    pivot = results_df.pivot(index=row_col, columns=col_col, values=value_col)

    # Build a matching boolean pivot for flagged cells
    flag_pivot = None
    if flag_col and flag_col in results_df.columns:
        flagged = (results_df[flag_col].fillna("") == flag_value).astype(float)
        flag_pivot = results_df.assign(_flag=flagged).pivot(
            index=row_col, columns=col_col, values="_flag",
        )

    fig, ax = plt.subplots(figsize=(8, 6))

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
                    fontsize=10, color=text_color, zorder=3)

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

    plt.show()


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
    """MA using pandas — EMA (ewm) or SMA (rolling)."""
    if ma_type == "SMA":
        return close.rolling(window=period).mean()
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
    ts_to_trade_num: dict[int, int] | None = None,
) -> tuple[list[dict], dict[int, dict]]:
    """
    Convert fills_report → (tvlc_markers, marker_detail_by_time).

    tvlc_markers    passed to candleSeries.setMarkers() — no extra keys.
    marker_detail   keyed by unix-seconds, holds extra data for the tooltip.

    ts_to_trade_num  optional mapping of unix-second timestamp → 1-based trade
                     number from the trades table, built from positions_report
                     ts_opened/ts_closed. When provided, the marker label
                     becomes "#N" instead of "B qty" / "S qty".

    NOTE: position_id on fills is NOT used for matching — in NETTING mode all
    fills share the same base position_id and cannot be mapped to individual
    trades. Timestamp matching is exact because each fill creates the event
    that opens or closes a position.
    """
    if fills_df is None or fills_df.empty:
        return [], {}

    tvlc_markers: list[dict] = []
    detail: dict[int, dict] = {}

    for _, row in fills_df.iterrows():
        ts_s = _ts_to_unix_s(row.get("ts_last") or row.get("ts_init"))
        if ts_s is None:
            continue

        side_raw = str(row.get("side", "")).upper()
        is_buy = "BUY" in side_raw

        qty_raw = row.get("filled_qty", row.get("quantity", "?"))
        px_raw  = row.get("avg_px", "?")

        try:
            px_fmt = f"{float(px_raw):,.2f}"
        except (ValueError, TypeError):
            px_fmt = str(px_raw)

        qty_str = str(qty_raw).rstrip("0").rstrip(".")  # "0.01000000" → "0.01"

        # Resolve trade number by timestamp — position_id cannot be used in
        # NETTING mode because all fills share the same base position_id.
        trade_num: int | None = None
        if ts_to_trade_num is not None and ts_s is not None:
            trade_num = ts_to_trade_num.get(ts_s)

        marker_text = f"#{trade_num}" if trade_num is not None else f"{'B' if is_buy else 'S'} {qty_str}"

        tvlc_markers.append({
            "time":     ts_s,
            "position": "belowBar" if is_buy else "aboveBar",
            "color":    "#26a69a" if is_buy else "#ef5350",
            "shape":    "arrowUp" if is_buy else "arrowDown",
            "text":     marker_text,
            "size":     1.5,
        })

        detail[ts_s] = {
            "is_buy":    is_buy,
            "side":      "BUY" if is_buy else "SELL",
            "qty":       qty_str,
            "px":        px_fmt,
            "trade_num": trade_num,
        }

    # TVLC requires markers sorted by time; deduplicate by taking last per ts
    seen: dict[int, dict] = {}
    for m in tvlc_markers:
        seen[m["time"]] = m
    tvlc_markers = sorted(seen.values(), key=lambda m: m["time"])

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


def _positions_to_rows(positions_df: pd.DataFrame) -> list[dict]:
    """Convert positions_report → list of plain dicts for the HTML trade table."""
    if positions_df is None or positions_df.empty:
        return []

    rows: list[dict] = []

    for _, row in positions_df.iterrows():
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


# ── Analysis tool charts ──────────────────────────────────────────────────────


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
    instrument_label: str = "",
    bar_label: str = "1h",
    starting_capital: float = 10_000.0,
    result_filename: str | None = None,
    open_browser: bool = False,
) -> Path:
    """
    Generate a self-contained HTML backtest report using TradingView Lightweight Charts.

    The output file requires only a browser — no server, no dependencies to install.

    Parameters
    ----------
    bars              NT Bar list from ParquetDataCatalog.
    fills_report      engine.trader.generate_order_fills_report()
    positions_report  engine.trader.generate_positions_report()
    fast_period       Fast MA period.
    slow_period       Slow MA period.
    ma_type           "EMA" or "SMA".
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

    # Build unix-second timestamp → 1-based trade number mapping so chart
    # markers show the trade number from the table.
    #
    # position_id cannot be used here: in NETTING mode all fills share the same
    # base position_id (e.g. "BTC-USD-PERP.HYPERLIQUID-EMACross-000") regardless
    # of which trade they belong to. Timestamp matching is exact because each
    # fill creates the event that opens or closes a position.
    ts_to_trade_num: dict[int, int] = {}
    if positions_report is not None and not positions_report.empty:
        for trade_num, (_, pos_row) in enumerate(positions_report.iterrows(), start=1):
            opened_s = _ts_to_unix_s(pos_row.get("ts_opened"))
            closed_s = _ts_to_unix_s(pos_row.get("ts_closed"))
            if opened_s:
                ts_to_trade_num[opened_s] = trade_num
            if closed_s:
                ts_to_trade_num[closed_s] = trade_num

    markers, marker_detail = _fills_to_markers(fills_report, ts_to_trade_num or None)
    position_rows          = _positions_to_rows(positions_report)
    stats                  = _compute_stats(position_rows, starting_capital)

    # ── Resolve output path ──────────────────────────────────────────────────
    if result_filename is None:
        asset = instrument_label.split("-")[0] if instrument_label else "unknown"
        result_filename = f"backtest_{asset}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = (_REPORTS_DIR / f"{result_filename}_{timestamp}.html").resolve()

    # ── Render template ──────────────────────────────────────────────────────
    title    = f"Backtest — {instrument_label} {bar_label}  {ma_type} {fast_period}/{slow_period}"
    subtitle = (
        f"{len(df):,} bars"
        + (f"  ·  {stats.get('num_trades', 0)} trades" if stats else "")
        + (f"  ·  capital {starting_capital:,.0f} USDC" if starting_capital else "")
    )

    html = _HTML_TEMPLATE.replace("__TITLE__",          title)
    html = html.replace("__SUBTITLE__",                 subtitle)
    html = html.replace("__FAST__",                     str(fast_period))
    html = html.replace("__SLOW__",                     str(slow_period))
    html = html.replace("__MA_TYPE__",                  ma_type)
    html = html.replace("__OHLCV_JSON__",               ohlcv_json)
    html = html.replace("__EMA_FAST_JSON__",            json.dumps(fast_ma_data))
    html = html.replace("__EMA_SLOW_JSON__",            json.dumps(slow_ma_data))
    html = html.replace("__MARKERS_JSON__",             json.dumps(markers))
    html = html.replace("__MARKER_DETAIL_JSON__",       json.dumps(marker_detail))
    html = html.replace("__TRADES_JSON__",              json.dumps(position_rows))
    html = html.replace("__STATS_JSON__",               json.dumps(stats))
    html = html.replace("__STARTING_CAPITAL__",          str(starting_capital))

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
td.pnl.pos { color: #26a69a; }
td.pnl.neg { color: #ef5350; }
.no-trades { padding: 28px; text-align: center; color: #787b86; }
</style>
</head>
<body>

<header>
  <h1>__TITLE__</h1>
  <span class="subtitle">__SUBTITLE__</span>
</header>

<div class="legend">
  <div class="legend-item">
    <div class="legend-line" style="background:#2196f3"></div>
    <span>__MA_TYPE__ __FAST__ (fast)</span>
  </div>
  <div class="legend-item">
    <div class="legend-line" style="background:#ff9800"></div>
    <span>__MA_TYPE__ __SLOW__ (slow)</span>
  </div>
  <div class="legend-item">
    <div class="legend-arrow-up"></div>
    <span>Long entry</span>
  </div>
  <div class="legend-item">
    <div class="legend-arrow-down"></div>
    <span>Short entry</span>
  </div>
</div>

<div id="chart-container">
  <div id="chart"></div>
  <div id="tooltip"></div>
</div>

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
        <th class="r sortable" id="th-pnl">PnL <span class="sort-arrow">&varr;</span></th>
        <th class="r sortable" id="th-ret">Return % <span class="sort-arrow">&varr;</span></th>
      </tr>
    </thead>
    <tbody id="trades-body"></tbody>
  </table>
</div>

<script>
// ── Injected data (serialised by Python) ─────────────────────────────────────
const OHLCV         = __OHLCV_JSON__;
const EMA_FAST_DATA = __EMA_FAST_JSON__;
const EMA_SLOW_DATA = __EMA_SLOW_JSON__;
const MARKERS       = __MARKERS_JSON__;
const MARKER_DETAIL = __MARKER_DETAIL_JSON__;   // {unix_s_str: {is_buy,side,qty,px,trade_num}}
const TRADES        = __TRADES_JSON__;
const STATS              = __STATS_JSON__;
const STARTING_CAPITAL   = __STARTING_CAPITAL__;
const FAST_PERIOD   = __FAST__;
const SLOW_PERIOD   = __SLOW__;
const MA_TYPE       = '__MA_TYPE__';

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

// Fast MA
const fastLine = chart.addLineSeries({
  color: '#2196f3', lineWidth: 1,
  priceLineVisible: false, lastValueVisible: true,
  title: MA_TYPE + FAST_PERIOD,
});
fastLine.setData(EMA_FAST_DATA);

// Slow MA
const slowLine = chart.addLineSeries({
  color: '#ff9800', lineWidth: 1,
  priceLineVisible: false, lastValueVisible: true,
  title: MA_TYPE + SLOW_PERIOD,
});
slowLine.setData(EMA_SLOW_DATA);

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

// Build a lookup keyed by timestamp string (MARKER_DETAIL keys are strings from JSON)
const detailByTs  = {};
for (const [k, v] of Object.entries(MARKER_DETAIL)) {
  detailByTs[parseInt(k)] = v;
}

function fmtNum(n, dec = 2) {
  if (n == null) return '—';
  return parseFloat(n).toLocaleString('en-US', {
    minimumFractionDigits: dec, maximumFractionDigits: dec
  });
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
  const mDetail = detailByTs[param.time];

  let html = `<div class="tt-time">${tsStr}</div>`;
  html += `<div class="tt-row"><span class="tt-label">O</span><span class="tt-value">${fmtNum(bar.open)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">H</span><span class="tt-value">${fmtNum(bar.high)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">L</span><span class="tt-value">${fmtNum(bar.low)}</span></div>`;
  html += `<div class="tt-row"><span class="tt-label">C</span><span class="tt-value">${fmtNum(bar.close)}</span></div>`;

  if (mDetail) {
    const cls = mDetail.is_buy ? 'buy' : 'sell';
    html += `<hr class="tt-sep">`;
    if (mDetail.trade_num != null) {
      html += `<div class="tt-row"><span class="tt-label">Trade</span><span class="tt-value">#${mDetail.trade_num}</span></div>`;
    }
    html += `<div class="tt-row"><span class="tt-label">Signal</span><span class="tt-${cls}">${mDetail.side}</span></div>`;
    html += `<div class="tt-row"><span class="tt-label">Qty</span><span class="tt-value">${mDetail.qty}</span></div>`;
    html += `<div class="tt-row"><span class="tt-label">Fill px</span><span class="tt-value">${mDetail.px}</span></div>`;
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

// ── Trade table ───────────────────────────────────────────────────────────────
(function renderTrades() {
  const tbody = document.getElementById('trades-body');
  document.getElementById('trade-count').textContent = TRADES.length;

  if (TRADES.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="no-trades">No closed positions found</td></tr>';
    return;
  }

  tbody.innerHTML = TRADES.map((t, i) => {
    const isLong   = t.side === 'Long';
    const pnl      = t.pnl;
    const pnlCls   = (pnl == null) ? '' : (pnl >= 0 ? 'pos' : 'neg');
    const pnlStr   = (pnl == null) ? '—' : (pnl >= 0 ? '+' : '') + fmtNum(pnl);
    const ret      = t.realized_return;
    const retStr   = (ret == null) ? '—' : (ret >= 0 ? '+' : '') + fmtNum(ret * 100, 2) + '%';
    const retCls   = (ret == null) ? '' : (ret >= 0 ? 'pos' : 'neg');

    return `<tr data-ts="${t.opened_ts_s || 0}" data-pnl="${pnl ?? 0}" data-ret="${ret ?? 0}" data-side="${isLong ? 'long' : 'short'}" onclick="scrollChart(this)">
      <td class="id">${i + 1}</td>
      <td>${t.opened}</td>
      <td>${t.closed}</td>
      <td><span class="badge ${isLong ? 'long' : 'short'}">${t.side}</span></td>
      <td class="r">${t.qty}</td>
      <td class="r">${t.entry_px}</td>
      <td class="r">${t.exit_px}</td>
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
