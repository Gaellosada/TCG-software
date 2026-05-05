"""Plotly figure builders, frontend-shape compatible.

One function per figure. Persist via Plotly's own `fig.write_json(path)`.
For batch persistence of the canonical plot set, see `write_plot_set`.

Style contract (Wave 6):
- No dashed lines anywhere. Traces are distinguished by colour only.
- Every figure has a layout title naming the strategy + the metric.
- Comparator-rich: equity / drawdown / yearly_bars include the underlying
  Buy & Hold and the risk-free curve when available on the result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data_load import PriceSeries
from .engine import BacktestResult, Trade

# Light-palette trace colours mirroring the frontend chartTheme TRACE_COLORS array.
# Solid-only — distinguish traces by colour, never by dash style.
_COLOR_STRATEGY = "#0ea5e9"   # sky
_COLOR_BH = "#f59e0b"         # amber
_COLOR_RF = "#10b981"         # emerald

# Hex -> rgba helper for derived fill colours (alpha-tinted equivalents of the
# above hues). Hard-coded constants avoid runtime parsing in hot paths.
_FILL_STRATEGY = "rgba(14,165,233,0.20)"   # sky @ 20%
_FILL_BH = "rgba(245,158,11,0.18)"         # amber @ 18%
_FILL_HEADER_BG = "rgba(14,165,233,0.08)"  # sky @ 8% (table header tint)
_FILL_BH_CELL = "rgba(245,158,11,0.06)"    # amber @ 6% (table cell tint)
_FILL_STRAT_CELL = "rgba(14,165,233,0.06)" # sky @ 6% (table cell tint)


def _yyyymmdd_to_iso(arr) -> list[str]:
    out: list[str] = []
    for d in arr:
        n = int(d)
        out.append(f"{n // 10000:04d}-{(n // 100) % 100:02d}-{n % 100:02d}")
    return out


# Shared axis defaults (light palette mirroring chartTheme.js).
_AXIS_DEFAULTS: dict = {
    "gridcolor": "#e5e7eb",
    "linecolor": "#d1d5db",
    "tickcolor": "#d1d5db",
    "zeroline": False,
    "showspikes": True,
    "spikemode": "across",
    "spikethickness": 1,
    "spikecolor": "rgba(155,163,184,0.4)",
    "spikedash": "dot",
}


def _base_layout(yaxis_title: str) -> dict:
    """Plotly layout dict mirroring the frontend LIGHT chartTheme palette.

    Solid traces only (Sign 2). Spike crosshair, x-unified hover, horizontal
    bottom legend, transparent paper/plot backgrounds, Outfit font stack.
    """
    xaxis = {**_AXIS_DEFAULTS, "type": "date"}
    yaxis = {**_AXIS_DEFAULTS, "title": yaxis_title}
    return {
        "paper_bgcolor": "#ffffff",
        "plot_bgcolor": "#ffffff",
        "font": {"family": "Outfit, system-ui, sans-serif", "size": 12, "color": "#374151"},
        "xaxis": xaxis,
        "yaxis": yaxis,
        "hovermode": "x unified",
        "hoverlabel": {
            "bgcolor": "rgba(255,255,255,0.85)",
            "bordercolor": "rgba(0,0,0,0.1)",
            "font": {"color": "#1a1a1a", "size": 11},
        },
        "legend": {
            "orientation": "h",
            "yanchor": "top",
            "y": -0.18,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 11},
            "bgcolor": "rgba(0,0,0,0)",
        },
        "margin": {"l": 60, "r": 24, "t": 50, "b": 60},
        "modebar": {
            "bgcolor": "rgba(0,0,0,0)",
            "color": "#9ca3af",
            "activecolor": "#1f2937",
        },
        "showlegend": True,
    }


def _spec(result: BacktestResult) -> dict:
    meta = result.meta if isinstance(result.meta, dict) else {}
    spec = meta.get("spec", {})
    return spec if isinstance(spec, dict) else {}


def _strategy_label(result: BacktestResult) -> str:
    """Human label from spec['label'], fallback 'Strategy'."""
    lbl = _spec(result).get("label")
    return lbl.strip() if isinstance(lbl, str) and lbl.strip() else "Strategy"


def _underlying_label(result: BacktestResult) -> str:
    """Underlying name from meta.instrument_id or meta.benchmark_id, fallback 'Underlying'."""
    meta = result.meta if isinstance(result.meta, dict) else {}
    for key in ("instrument_id", "benchmark_id"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip() and v.strip().lower() != "underlying":
            return v.strip()
    return "Underlying"


def _risk_free_rate(result: BacktestResult) -> float:
    try:
        return float(_spec(result).get("risk_free_rate", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _capital_base(result: BacktestResult) -> float:
    try:
        return float(_spec(result)["capital_base"])
    except (KeyError, TypeError, ValueError):
        eq = np.asarray(result.equity_curve, dtype=np.float64)
        return float(eq[0]) if eq.size else 100_000.0


def _drawdown_pct(equity: np.ndarray) -> np.ndarray:
    """Drawdown in percent (negative or zero) for an equity curve."""
    eq = np.asarray(equity, dtype=np.float64)
    if eq.size == 0:
        return eq
    running_max = np.maximum.accumulate(eq)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(running_max > 0, eq / running_max - 1.0, 0.0)
    return dd * 100.0


def equity_curve(
    result: BacktestResult,
    *,
    title: str | None = None,
    show_benchmark: bool = True,
) -> go.Figure:
    """P&L curve (equity − capital_base) with strategy + B&H + risk-free comparators.

    Y-axis is signed P&L from inception — strategy/B&H/RF traces are all shifted by
    the same `capital_base`, preserving relative comparisons while making "$0 = even"
    the natural reference line. Solid lines, colour-distinguished only (Sign 2).
    """
    from .metrics import buy_and_hold_curve, risk_free_curve

    fig = go.Figure()
    iso = _yyyymmdd_to_iso(result.dates)
    label = _strategy_label(result)
    underlying = _underlying_label(result)
    rf_rate = _risk_free_rate(result)
    cap = _capital_base(result)

    pnl_hovertemplate = "%{y:+,.0f} $<extra>%{fullData.name}</extra>"

    # Strategy trace (always present). Shift to P&L from capital_base.
    strat_pnl = np.asarray(result.equity_curve, dtype=np.float64) - cap
    fig.add_trace(
        go.Scatter(
            x=iso,
            y=strat_pnl.tolist(),
            mode="lines",
            name=label,
            line={"width": 2, "color": _COLOR_STRATEGY},
            hovertemplate=pnl_hovertemplate,
        )
    )

    # Underlying Buy & Hold (engine pre-normalises benchmark_curve to capital_base).
    if show_benchmark:
        bh = buy_and_hold_curve(result)
        if bh is not None:
            _, bh_eq = bh
            bh_pnl = np.asarray(bh_eq, dtype=np.float64) - cap
            fig.add_trace(
                go.Scatter(
                    x=iso,
                    y=bh_pnl.tolist(),
                    mode="lines",
                    name=f"{underlying} Buy & Hold",
                    line={"width": 2, "color": _COLOR_BH},
                    hovertemplate=pnl_hovertemplate,
                )
            )

    # Risk-free curve (always derivable from configured rate + capital_base).
    rf_eq = risk_free_curve(np.asarray(result.dates, dtype=np.int64), rf_rate, cap)
    if rf_eq.size > 0:
        rf_pnl = rf_eq - cap
        fig.add_trace(
            go.Scatter(
                x=iso,
                y=rf_pnl.tolist(),
                mode="lines",
                name=f"Risk-Free @ {rf_rate * 100:.2f}%",
                line={"width": 2, "color": _COLOR_RF},
                hovertemplate=pnl_hovertemplate,
            )
        )

    fig.update_layout(**_base_layout("P&L ($)"))
    fig.update_layout(title=title or f"P&L — {label}")
    return fig


def drawdown(result: BacktestResult, *, title: str | None = None) -> go.Figure:
    """Drawdown filled chart, strategy + underlying B&H (negative-only, percent, solid lines)."""
    from .metrics import buy_and_hold_curve

    fig = go.Figure()
    iso = _yyyymmdd_to_iso(result.dates)
    label = _strategy_label(result)
    underlying = _underlying_label(result)

    fig.add_trace(
        go.Scatter(
            x=iso,
            y=(np.asarray(result.drawdown_curve, dtype=np.float64) * 100.0).tolist(),
            mode="lines",
            fill="tozeroy",
            name=f"{label} DD",
            line={"width": 1, "color": _COLOR_STRATEGY},
            fillcolor=_FILL_STRATEGY,
        )
    )

    bh = buy_and_hold_curve(result)
    if bh is not None:
        _, bh_eq = bh
        fig.add_trace(
            go.Scatter(
                x=iso,
                y=_drawdown_pct(bh_eq).tolist(),
                mode="lines",
                fill="tozeroy",
                name=f"{underlying} B&H DD",
                line={"width": 1, "color": _COLOR_BH},
                fillcolor=_FILL_BH,
            )
        )

    fig.update_layout(**_base_layout("Drawdown (%)"))
    fig.update_layout(title=title or f"Drawdown — {label}")
    return fig


def monthly_returns_heatmap(result_or_rows, *, title: str | None = None) -> go.Figure:
    """Years (rows) x months (cols) heatmap of monthly returns; cells annotated with formatted percent."""
    label = "Strategy"
    if isinstance(result_or_rows, BacktestResult):
        from .metrics import monthly_returns_table
        label = _strategy_label(result_or_rows)
        rows = monthly_returns_table(result_or_rows.equity_curve, result_or_rows.dates)
    else:
        rows = list(result_or_rows)
    grid: dict[int, dict[int, float]] = {}
    for row in rows:
        period = str(row.get("period", ""))
        if "-" not in period:
            continue
        y_str, m_str = period.split("-", 1)
        try:
            y, m = int(y_str), int(m_str)
        except ValueError:
            continue
        v = row.get("value")
        if v is None and "portfolio" in row:
            v = row["portfolio"]
        if v is None:
            continue
        grid.setdefault(y, {})[m] = float(v) * 100.0
    years = sorted(grid.keys())
    months = list(range(1, 13))
    z: list[list[float | None]] = [[grid.get(y, {}).get(m, None) for m in months] for y in years]
    text: list[list[str]] = [
        [
            ("" if (cell is None or (isinstance(cell, float) and np.isnan(cell))) else f"{cell:+.2f}%")
            for cell in row
        ]
        for row in z
    ]
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=month_names,
            y=[str(y) for y in years],
            text=text,
            texttemplate="%{text}",
            textfont={"size": 11, "color": "black"},
            colorscale="RdYlGn",
            zmid=0,
            hovertemplate="%{y}-%{x}: %{z:.2f}%%<extra></extra>",
            name="Monthly returns",
        )
    )
    layout = _base_layout("")
    # Heatmap doesn't need a numeric y-axis title; clear the "title" key on yaxis.
    layout["yaxis"] = {**layout["yaxis"], "title": "Year"}
    layout["xaxis"] = {**layout["xaxis"], "type": "category", "title": "Month"}
    layout["hovermode"] = "closest"  # x-unified is meaningless on a heatmap
    fig.update_layout(**layout)
    fig.update_layout(title=title or f"Monthly Returns — {label}")
    return fig


def monthly_log_returns_heatmap(result_or_rows, *, title: str | None = None) -> go.Figure:
    """Heatmap of monthly LOG returns (ln(1+r)). Same grid layout as the linear heatmap, formatted as decimal not %."""
    label = "Strategy"
    if isinstance(result_or_rows, BacktestResult):
        from .metrics import monthly_returns_table
        label = _strategy_label(result_or_rows)
        rows = monthly_returns_table(result_or_rows.equity_curve, result_or_rows.dates)
    else:
        rows = list(result_or_rows)
    grid: dict[int, dict[int, float]] = {}
    for row in rows:
        period = str(row.get("period", ""))
        if "-" not in period:
            continue
        y_str, m_str = period.split("-", 1)
        try:
            y, m = int(y_str), int(m_str)
        except ValueError:
            continue
        v = row.get("value")
        if v is None and "portfolio" in row:
            v = row["portfolio"]
        if v is None:
            continue
        # log return: ln(1 + r). Guard against r <= -1 (total wipeout) by clipping
        # to a tiny positive value before log so we get a finite, very-negative cell
        # rather than -inf/NaN propagating into the colorscale.
        r_lin = float(v)
        log_r = float(np.log1p(max(r_lin, -0.9999999)))
        grid.setdefault(y, {})[m] = log_r
    years = sorted(grid.keys())
    months = list(range(1, 13))
    z: list[list[float | None]] = [[grid.get(y, {}).get(m, None) for m in months] for y in years]
    text: list[list[str]] = [
        [
            ("" if (cell is None or (isinstance(cell, float) and np.isnan(cell))) else f"{cell:+.2f}")
            for cell in row
        ]
        for row in z
    ]
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=month_names,
            y=[str(y) for y in years],
            text=text,
            texttemplate="%{text}",
            textfont={"size": 11, "color": "black"},
            colorscale="RdYlGn",
            zmid=0,
            hovertemplate="%{y}-%{x}: %{z:+.4f}<extra></extra>",
            name="Monthly log returns",
        )
    )
    layout = _base_layout("")
    layout["yaxis"] = {**layout["yaxis"], "title": "Year"}
    layout["xaxis"] = {**layout["xaxis"], "type": "category", "title": "Month"}
    layout["hovermode"] = "closest"
    fig.update_layout(**layout)
    fig.update_layout(title=title or f"Monthly Log Returns — {label}")
    return fig


def yearly_returns_bars(result_or_rows, *, title: str | None = None) -> go.Figure:
    """Yearly returns grouped bars: strategy + B&H + risk-free, distinct colours."""
    from .metrics import (
        buy_and_hold_curve,
        risk_free_curve,
        yearly_returns_table,
    )

    fig = go.Figure()

    # Branch A: BacktestResult — full 3-trace comparison.
    if isinstance(result_or_rows, BacktestResult):
        result = result_or_rows
        label = _strategy_label(result)
        underlying = _underlying_label(result)
        rf_rate = _risk_free_rate(result)
        cap = _capital_base(result)
        dates = np.asarray(result.dates, dtype=np.int64)

        strat_rows = yearly_returns_table(
            np.asarray(result.equity_curve, dtype=np.float64), dates
        )
        bh = buy_and_hold_curve(result)
        bh_rows = (
            yearly_returns_table(bh[1], bh[0]) if bh is not None else []
        )
        rf_eq = risk_free_curve(dates, rf_rate, cap)
        rf_rows = yearly_returns_table(rf_eq, dates) if rf_eq.size else []

        years = sorted({str(r["period"]) for r in (strat_rows + bh_rows + rf_rows)})

        def _by_year(rows: list[dict]) -> dict[str, float]:
            return {str(r.get("period", "")): float(r.get("value", r.get("portfolio", 0.0))) * 100.0 for r in rows}

        s = _by_year(strat_rows)
        b = _by_year(bh_rows)
        r = _by_year(rf_rows)

        fig.add_trace(go.Bar(
            x=years, y=[s.get(y, 0.0) for y in years],
            name=label, marker_color=_COLOR_STRATEGY,
            hovertemplate="%{x}: %{y:.2f}%%<extra>" + label + "</extra>",
        ))
        if bh_rows:
            fig.add_trace(go.Bar(
                x=years, y=[b.get(y, 0.0) for y in years],
                name=f"{underlying} B&H", marker_color=_COLOR_BH,
                hovertemplate="%{x}: %{y:.2f}%%<extra>B&H</extra>",
            ))
        fig.add_trace(go.Bar(
            x=years, y=[r.get(y, 0.0) for y in years],
            name=f"Risk-Free @ {rf_rate * 100:.2f}%", marker_color=_COLOR_RF,
            hovertemplate="%{x}: %{y:.2f}%%<extra>RF</extra>",
        ))

        layout = _base_layout("Return (%)")
        layout["xaxis"] = {**layout["xaxis"], "type": "category", "title": "Year"}
        fig.update_layout(**layout)
        fig.update_layout(barmode="group", title=title or f"Yearly Returns — {label}")
        return fig

    # Branch B: legacy list-of-rows. Single trace, colour by sign, still solid + grouped barmode.
    rows = list(result_or_rows)
    years = [str(r.get("period", "")) for r in rows]
    vals = [float(r.get("value", r.get("portfolio", 0.0))) * 100.0 for r in rows]
    colors = [_COLOR_STRATEGY if v >= 0 else _COLOR_BH for v in vals]
    fig.add_trace(go.Bar(
        x=years, y=vals, name="Yearly return", marker_color=colors,
        hovertemplate="%{x}: %{y:.2f}%%<extra></extra>",
    ))
    layout = _base_layout("Return (%)")
    layout["xaxis"] = {**layout["xaxis"], "type": "category", "title": "Year"}
    fig.update_layout(**layout)
    fig.update_layout(barmode="group", title=title or "Yearly Returns")
    return fig


def trade_markers(result_or_data, trades=None, *, title: str | None = None) -> go.Figure:
    """Two-row stacked figure: top row price + Buy/Sell markers, bottom row running P&L ($).

    Accepts a BacktestResult (P&L derived from equity_curve − capital_base) or a
    `(PriceSeries, trades)` tuple (P&L derived from cumulative realized leg-PnL on
    the trades; falls back to a "no realized PnL" annotation when none is recorded).
    Shared x-axis, hover sync via `hovermode='x unified'`.
    """
    if isinstance(result_or_data, BacktestResult):
        result = result_or_data
        # Build a synthetic PriceSeries from result.dates + equity_curve as a stand-in
        # when no underlying bars are stored on the result (engine doesn't keep them).
        n = result.dates.shape[0]
        bars = PriceSeries(
            instrument_id=result.meta.get("instrument_id", "underlying") or "underlying",
            provider=result.meta.get("provider", "RESULT"),
            dates=result.dates,
            open=np.zeros(n, dtype=np.float64),
            high=np.zeros(n, dtype=np.float64),
            low=np.zeros(n, dtype=np.float64),
            close=result.equity_curve.astype(np.float64),
            volume=np.zeros(n, dtype=np.float64),
        )
        label = _strategy_label(result)
        cap = _capital_base(result)
        # For BacktestResult input, running P&L is unambiguous: equity − capital_base.
        pnl_dates = bars.dates
        pnl_values = (np.asarray(result.equity_curve, dtype=np.float64) - cap).tolist()
        return _trade_markers_from_bars(
            bars,
            list(result.trades),
            title=title or f"Trade Markers — {label}",
            pnl_dates=pnl_dates,
            pnl_values=pnl_values,
        )
    return _trade_markers_from_bars(
        result_or_data,
        list(trades or []),
        title=title or "Trade Markers",
        pnl_dates=None,
        pnl_values=None,
    )


def _cumulative_realized_pnl_from_trades(
    bar_dates: np.ndarray, trades: list[Trade]
) -> tuple[np.ndarray | None, list[float] | None, str | None]:
    """Walk trades chronologically, summing realized leg P&L into a per-bar cumulative array.

    Returns `(dates, values, note)` where:
    - `dates` is the bar-aligned date axis (same as input) when any trade has a non-zero
      `pnl`, else `None`;
    - `values` is the running cumulative sum forward-filled across bars;
    - `note` is a string when there is no realized P&L data (caller should annotate).
    """
    if not trades:
        return None, None, "(no realized P&L available)"
    has_realized = any(float(tr.pnl) != 0.0 for tr in trades)
    if not has_realized:
        return None, None, "(no realized P&L available)"
    # Build a per-date pnl-delta map.
    delta_by_date: dict[int, float] = {}
    for tr in trades:
        d = int(tr.date)
        delta_by_date[d] = delta_by_date.get(d, 0.0) + float(tr.pnl)
    cumulative = 0.0
    out: list[float] = []
    for d in bar_dates:
        cumulative += delta_by_date.get(int(d), 0.0)
        out.append(cumulative)
    return bar_dates, out, None


def _trade_markers_from_bars(
    data: PriceSeries,
    trades: list[Trade],
    *,
    title: str | None = None,
    pnl_dates: np.ndarray | None = None,
    pnl_values: list[float] | None = None,
) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.05,
    )
    iso = _yyyymmdd_to_iso(data.dates)

    # Row 1 — price + markers.
    fig.add_trace(
        go.Scatter(
            x=iso, y=data.close.tolist(), mode="lines", name="Close",
            line={"width": 1.5, "color": _COLOR_STRATEGY},
        ),
        row=1, col=1,
    )
    buys_x: list[str] = []
    buys_y: list[float] = []
    sells_x: list[str] = []
    sells_y: list[float] = []
    for tr in trades:
        if tr.leg != "underlying":
            continue
        s = _yyyymmdd_to_iso(np.array([tr.date], dtype=np.int64))[0]
        if tr.side == "BUY":
            buys_x.append(s)
            buys_y.append(float(tr.price))
        else:
            sells_x.append(s)
            sells_y.append(float(tr.price))
    if buys_x:
        fig.add_trace(
            go.Scatter(
                x=buys_x, y=buys_y, mode="markers", name="Buy",
                marker={"symbol": "triangle-up", "color": "#10b981", "size": 9},
            ),
            row=1, col=1,
        )
    if sells_x:
        fig.add_trace(
            go.Scatter(
                x=sells_x, y=sells_y, mode="markers", name="Sell",
                marker={"symbol": "triangle-down", "color": "#ef4444", "size": 9},
            ),
            row=1, col=1,
        )

    # Row 2 — running P&L.
    annotation_note: str | None = None
    if pnl_values is None:
        # PriceSeries + trades branch: derive realized cumulative P&L if available.
        d2, v2, note = _cumulative_realized_pnl_from_trades(data.dates, trades)
        if d2 is not None and v2 is not None:
            pnl_dates_iso = _yyyymmdd_to_iso(d2)
            fig.add_trace(
                go.Scatter(
                    x=pnl_dates_iso, y=v2, mode="lines", name="Cumulative P&L",
                    line={"width": 1.5, "color": _COLOR_BH},
                    hovertemplate="%{y:+,.0f} $<extra>P&L</extra>",
                ),
                row=2, col=1,
            )
        else:
            annotation_note = note
    else:
        # BacktestResult branch.
        pnl_iso = _yyyymmdd_to_iso(pnl_dates if pnl_dates is not None else data.dates)
        fig.add_trace(
            go.Scatter(
                x=pnl_iso, y=pnl_values, mode="lines", name="Running P&L",
                line={"width": 1.5, "color": _COLOR_BH},
                hovertemplate="%{y:+,.0f} $<extra>P&L</extra>",
            ),
            row=2, col=1,
        )

    # Apply the figure-level themed layout, then row-specific axis tweaks.
    fig.update_layout(**_base_layout("Price"))
    fig.update_layout(title=title or "Trade Markers")

    # Per-axis overrides (xaxis already date-typed via _base_layout, but row 2's
    # xaxis2 needs the same treatment).
    fig.update_xaxes(**{**_AXIS_DEFAULTS, "type": "date"}, row=1, col=1)
    fig.update_xaxes(**{**_AXIS_DEFAULTS, "type": "date"}, row=2, col=1)
    fig.update_yaxes(**{**_AXIS_DEFAULTS, "title": "Price"}, row=1, col=1)
    fig.update_yaxes(**{**_AXIS_DEFAULTS, "title": "P&L ($)"}, row=2, col=1)

    if annotation_note is not None:
        fig.add_annotation(
            text=annotation_note,
            xref="x2 domain", yref="y2 domain",
            x=0.5, y=0.5, showarrow=False,
            font={"size": 12, "color": "#6b7280"},
        )

    return fig


def hold_time_histogram(result_or_trades, *, title: str | None = None) -> go.Figure:
    """Histogram of hold-time (calendar-day diff between BUY and matching SELL on `underlying` leg)."""
    if isinstance(result_or_trades, BacktestResult):
        trades = list(result_or_trades.trades)
    else:
        trades = list(result_or_trades)
    holds: list[int] = []
    open_dt: int | None = None
    for tr in trades:
        if tr.leg != "underlying":
            continue
        if open_dt is None and tr.side == "BUY":
            open_dt = int(tr.date)
        elif open_dt is not None and tr.side == "SELL":
            d0 = open_dt
            d1 = int(tr.date)
            holds.append(_calendar_days_between(d0, d1))
            open_dt = None
    label = _strategy_label(result_or_trades) if isinstance(result_or_trades, BacktestResult) else "Strategy"
    fig = go.Figure(data=go.Histogram(
        x=holds, nbinsx=20, name="Hold time (days)", marker_color=_COLOR_STRATEGY,
    ))
    layout = _base_layout("Trades")
    layout["xaxis"] = {**layout["xaxis"], "type": "linear", "title": "Hold time (days)"}
    layout["hovermode"] = "closest"
    fig.update_layout(**layout)
    fig.update_layout(title=title or f"Hold-Time Distribution — {label}")
    return fig


def _calendar_days_between(d0: int, d1: int) -> int:
    from datetime import date as _date
    a = _date(d0 // 10000, (d0 // 100) % 100, d0 % 100)
    b = _date(d1 // 10000, (d1 // 100) % 100, d1 % 100)
    return abs((b - a).days)


# --------------------------------------------------------------------------- stats panel


def _is_nan(v: object) -> bool:
    return isinstance(v, float) and np.isnan(v)


def _fmt_pct(v: float | None) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"{v * 100:+.2f}%"


def _fmt_ratio(v: float | None) -> str:
    if v is None or _is_nan(v):
        return "—"
    if isinstance(v, float) and (np.isinf(v) or abs(v) >= 1e8):
        return "inf"
    return f"{v:.2f}"


def _fmt_int(v: int | float | None) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"{int(v)}"


def _fmt_winrate(v: float | None) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"{v * 100:.1f}%"


def stats_panel(strategy_result: BacktestResult, *, title: str | None = None) -> go.Figure:
    """Two-column performance stats table: strategy vs Buy & Hold (10 metrics)."""
    from .metrics import buy_and_hold_curve, compare_stats

    label = _strategy_label(strategy_result)
    underlying = _underlying_label(strategy_result)

    bh = buy_and_hold_curve(strategy_result)
    bh_dates = bh[0] if bh is not None else None
    bh_equity = bh[1] if bh is not None else None
    cmp = compare_stats(strategy_result, bh_dates, bh_equity)
    s = cmp["strategy"]
    b = cmp["buy_and_hold"]

    rows = [
        ("Total return",          _fmt_pct(s.total_return),           _fmt_pct(b.total_return) if b else "—"),
        ("Annualized return",     _fmt_pct(s.annualized_return),      _fmt_pct(b.annualized_return) if b else "—"),
        ("Annualized vol",        _fmt_pct(s.annualized_volatility),  _fmt_pct(b.annualized_volatility) if b else "—"),
        ("Sharpe",                _fmt_ratio(s.sharpe_ratio),         _fmt_ratio(b.sharpe_ratio) if b else "—"),
        ("Sortino",               _fmt_ratio(s.sortino_ratio),        _fmt_ratio(b.sortino_ratio) if b else "—"),
        ("Max drawdown",          _fmt_pct(s.max_drawdown),           _fmt_pct(b.max_drawdown) if b else "—"),
        ("Calmar",                _fmt_ratio(s.calmar_ratio),         _fmt_ratio(b.calmar_ratio) if b else "—"),
        ("Win rate",              _fmt_winrate(s.win_rate),           _fmt_winrate(b.win_rate) if b else "—"),
        ("Number of trades",      _fmt_int(s.num_trades),              _fmt_int(b.num_trades) if b else "—"),
        ("Time underwater (days)", _fmt_int(s.time_underwater_days),   _fmt_int(b.time_underwater_days) if b else "—"),
    ]
    metric_col = [r[0] for r in rows]
    strat_col = [r[1] for r in rows]
    bh_col = [r[2] for r in rows]
    bh_header = f"{underlying} Buy & Hold" if bh is not None else "Buy & Hold (n/a)"

    fig = go.Figure(data=[go.Table(
        header={
            "values": ["Metric", label, bh_header],
            "fill_color": _COLOR_STRATEGY,
            "font": {"color": "white", "size": 12, "family": "Outfit, system-ui, sans-serif"},
            "align": "left",
        },
        cells={
            "values": [metric_col, strat_col, bh_col],
            "fill_color": [
                "#ffffff",
                _FILL_STRAT_CELL,
                _FILL_BH_CELL,
            ],
            "align": "left",
            "font": {"size": 12, "family": "Outfit, system-ui, sans-serif", "color": "#374151"},
        },
    )])
    fig.update_layout(
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"family": "Outfit, system-ui, sans-serif", "size": 12, "color": "#374151"},
        margin={"l": 30, "r": 30, "t": 50, "b": 30},
        modebar={"bgcolor": "rgba(0,0,0,0)", "color": "#9ca3af", "activecolor": "#1f2937"},
        title=title or f"Performance Stats — {label} vs Buy & Hold",
    )
    return fig


# --------------------------------------------------------------------------- raw-input sanity chart


def _has_meaningful_volume(volume: np.ndarray) -> bool:
    """Return True iff the volume column carries non-zero, non-NaN data.

    Used to gate the volume sub-row of `plot_price_history`. The npz files
    written by P2 (data_load.save_bars_npz) always include a `volume` key,
    but providers like INDEX/YAHOO publish synthetic volume that's a
    string of zeros — that's the same as missing data for plotting purposes.
    """
    if volume is None:
        return False
    v = np.asarray(volume, dtype=np.float64)
    if v.size == 0:
        return False
    finite = v[np.isfinite(v)]
    if finite.size == 0:
        return False
    return bool(np.any(finite > 0.0))


def plot_price_history(
    bars: PriceSeries,
    *,
    benchmark: PriceSeries | None = None,
    title: str | None = None,
) -> go.Figure:
    """Sanity-check chart of the raw input data: close price + optional benchmark + conditional volume.

    Renders BENEATH §3 Data summary in the deliverable notebook so the user
    can eyeball the underlying before reading any equity curve. Reads from
    the cached `.npz` (via `PriceSeries`) — never re-fetches.

    Layout:
      - When `bars.volume` carries non-zero values: 2-row stacked subplot,
        row 1 = close (+ optional benchmark trace, normalized to bars[0] when
        scales differ by >2x), row 2 = volume bars.
      - When volume is missing or all-zero: single-row figure with just the
        close (+ optional benchmark).

    `benchmark` is rendered when supplied. If its first close differs from
    `bars.close[0]` by more than 2x (typical when comparing SPX index to ETF),
    we display the benchmark on a secondary y-axis to keep both visible.

    Style: solid lines, sky strategy + amber benchmark, x-unified hover —
    matches the rest of the plot suite. No dashes (Sign 1).
    """
    if not isinstance(bars, PriceSeries):
        raise TypeError(f"plot_price_history requires a PriceSeries, got {type(bars).__name__}")
    if bars.dates.size == 0:
        raise ValueError("plot_price_history: bars is empty")

    show_volume = _has_meaningful_volume(bars.volume)
    iso = _yyyymmdd_to_iso(bars.dates)
    instrument = bars.instrument_id or "Underlying"
    close = np.asarray(bars.close, dtype=np.float64)

    # Decide whether to put benchmark on a secondary y-axis. When the absolute
    # scales differ by >2x at the first valid sample, share the same panel but
    # plot the benchmark against y2 so neither trace gets squashed.
    bench_on_y2 = False
    if benchmark is not None and benchmark.dates.size > 0 and close.size > 0:
        b_close = np.asarray(benchmark.close, dtype=np.float64)
        if b_close.size > 0:
            ref_a = float(close[0])
            ref_b = float(b_close[0])
            if ref_a > 0 and ref_b > 0:
                ratio = max(ref_a, ref_b) / max(min(ref_a, ref_b), 1e-9)
                bench_on_y2 = ratio > 2.0

    if show_volume:
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.75, 0.25],
            vertical_spacing=0.06,
            specs=[
                [{"secondary_y": bench_on_y2}],
                [{}],
            ],
        )
    else:
        fig = make_subplots(
            rows=1,
            cols=1,
            specs=[[{"secondary_y": bench_on_y2}]],
        )

    price_hover = "%{y:,.2f}<extra>%{fullData.name}</extra>"

    # Row 1 — close.
    fig.add_trace(
        go.Scatter(
            x=iso,
            y=close.tolist(),
            mode="lines",
            name=f"{instrument} Close",
            line={"width": 1.5, "color": _COLOR_STRATEGY},
            hovertemplate=price_hover,
        ),
        row=1, col=1,
        secondary_y=False,
    )

    # Row 1 — benchmark overlay (optional).
    if benchmark is not None and benchmark.dates.size > 0:
        b_iso = _yyyymmdd_to_iso(benchmark.dates)
        b_close_arr = np.asarray(benchmark.close, dtype=np.float64)
        b_base_label = (benchmark.instrument_id or "Benchmark") + " (benchmark)"
        # When the benchmark is rescaled onto a secondary y-axis, surface the fact
        # in the legend label so it isn't only visible in hover. The full visual
        # explanation goes on a subtitle annotation below.
        b_label = b_base_label + (" — right axis" if bench_on_y2 else "")
        fig.add_trace(
            go.Scatter(
                x=b_iso,
                y=b_close_arr.tolist(),
                mode="lines",
                name=b_label,
                line={"width": 1.25, "color": _COLOR_BH},
                hovertemplate=price_hover,
            ),
            row=1, col=1,
            secondary_y=bench_on_y2,
        )

    # Row 2 — volume (only when present).
    if show_volume:
        v = np.asarray(bars.volume, dtype=np.float64)
        # Replace NaNs with 0 for display so plotly doesn't drop bars.
        v = np.where(np.isfinite(v), v, 0.0)
        fig.add_trace(
            go.Bar(
                x=iso,
                y=v.tolist(),
                name="Volume",
                marker_color=_FILL_STRATEGY,
                hovertemplate="%{y:,.0f}<extra>Volume</extra>",
            ),
            row=2, col=1,
        )

    # Apply themed layout, then per-axis tweaks.
    layout = _base_layout("Price")
    # Extra top margin so the linear/log toggle (top-right) doesn't crowd the
    # title (top-left).
    layout["margin"] = {**layout["margin"], "t": 88}
    fig.update_layout(**layout)
    fig.update_layout(title=title or f"Raw Input Data — {instrument}")

    # Per-axis overrides: the top row is price (date x-axis already set by base layout),
    # the bottom row needs a date xaxis2 + numeric "Volume" yaxis2/yaxis3 (depending on
    # whether the secondary_y was used for benchmark).
    fig.update_xaxes(**{**_AXIS_DEFAULTS, "type": "date"}, row=1, col=1)
    fig.update_yaxes(
        **{**_AXIS_DEFAULTS, "title": f"{instrument} Price"},
        row=1, col=1, secondary_y=False,
    )
    if bench_on_y2:
        # Secondary y-axis title — use the benchmark instrument for clarity.
        b_title = (benchmark.instrument_id if benchmark is not None else "Benchmark") + " Price"
        fig.update_yaxes(
            **{**_AXIS_DEFAULTS, "title": b_title},
            row=1, col=1, secondary_y=True,
        )
        # Subtitle annotation: make the rescaling explicit, not just legend-implicit.
        fig.add_annotation(
            text="benchmark rescaled to share the close-price visual span (right axis)",
            xref="paper", yref="paper",
            x=0.0, y=1.04, xanchor="left", yanchor="bottom",
            showarrow=False,
            font={"size": 10, "color": "#6b7280"},
        )
    if show_volume:
        fig.update_xaxes(**{**_AXIS_DEFAULTS, "type": "date"}, row=2, col=1)
        fig.update_yaxes(**{**_AXIS_DEFAULTS, "title": "Volume"}, row=2, col=1)

    # Linear/log y-toggle for the close-price axis only (volume bars on a log
    # scale are meaningless). When the benchmark is on `yaxis2` (row-1 secondary_y),
    # toggle that too so both close-price traces share the same scale type.
    # Volume axis ids (yaxis2 or yaxis3, depending on bench_on_y2) are deliberately
    # excluded.
    price_axes = ["yaxis"]
    if bench_on_y2:
        price_axes.append("yaxis2")
    linear_args: dict = {f"{ax}.type": "linear" for ax in price_axes}
    log_args: dict = {f"{ax}.type": "log" for ax in price_axes}
    fig.update_layout(updatemenus=[{
        "type": "buttons", "direction": "right",
        "x": 1.0, "y": 1.12, "xanchor": "right", "yanchor": "top",
        "pad": {"t": 4, "b": 4, "l": 6, "r": 6},
        "bgcolor": "#ffffff",
        "bordercolor": "#d1d5db",
        "font": {"color": "#374151", "size": 11},
        "buttons": [
            {"label": "linear", "method": "relayout", "args": [linear_args]},
            {"label": "log", "method": "relayout", "args": [log_args]},
        ],
    }])

    return fig


# --------------------------------------------------------------------------- batch writer


# Maps canonical plot id (per pipeline/04-analyze.md table) to the builder fn.
_PLOT_BUILDERS: dict[str, object] = {
    "equity": equity_curve,
    "drawdown": drawdown,
    "returns_heatmap": monthly_returns_heatmap,
    "log_returns_heatmap": monthly_log_returns_heatmap,
    "yearly_bars": yearly_returns_bars,
    "trade_markers": trade_markers,
    "hold_time_hist": hold_time_histogram,
    "stats_panel": stats_panel,
}


def write_plot_set(
    result: BacktestResult, out_dir: str | Path, plot_ids: Iterable[str]
) -> dict[str, Path]:
    """Build and write the requested plot ids to out_dir/<plot_id>.json. Returns {id: path}.

    Canonical plot_ids (from pipeline/04-analyze.md):
    `equity`, `drawdown`, `returns_heatmap`, `yearly_bars`, `trade_markers`, `hold_time_hist`,
    `stats_panel`.
    Unknown ids raise `ValueError` listing the offender and the valid set.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for pid in plot_ids:
        builder = _PLOT_BUILDERS.get(pid)
        if builder is None:
            raise ValueError(
                f"unknown plot_id {pid!r}; valid ids: {sorted(_PLOT_BUILDERS.keys())}"
            )
        fig = builder(result)  # type: ignore[operator]
        p = out / f"{pid}.json"
        fig.write_json(str(p))
        paths[pid] = p
    return paths


# ---------------------------------------------------------------------------
# Plot registry — PlotJob + BASELINE_PLOTS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlotJob:
    """Bind a plot ``id`` to a ``builder`` + frozen ``kwargs``.

    The compile pipeline runs each ``PlotJob`` (baseline + strategy-declared
    ``EXTRA_PLOTS``), writing per-plot JSON to ``results/plots/<id>.json``.
    The builder receives ``result`` (a ``BacktestResult``-like object) as its
    first positional argument by convention, plus any ``kwargs``.

    Builders that need different positional inputs wrap the call with a
    closure when declared in ``EXTRA_PLOTS``.
    """

    id: str
    builder: Callable[..., Any]
    kwargs: dict[str, Any] = field(default_factory=dict)


# Baseline plots — always rendered for any strategy that produces an equity
# curve. Six builders chosen per the locked design:
#   1. equity, 2. drawdown, 3. yearly bars (returns histogram),
#   4. stats panel (metrics_panel), 5. trade markers, 6. hold-time hist.
BASELINE_PLOTS: list[PlotJob] = [
    PlotJob(id="equity", builder=equity_curve),
    PlotJob(id="drawdown", builder=drawdown),
    PlotJob(id="yearly_bars", builder=yearly_returns_bars),
    PlotJob(id="stats_panel", builder=stats_panel),
    PlotJob(id="trade_markers", builder=trade_markers),
    PlotJob(id="hold_time_hist", builder=hold_time_histogram),
]
