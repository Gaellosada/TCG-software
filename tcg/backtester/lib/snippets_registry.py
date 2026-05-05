"""Registry mapping (asset_class, scenario) -> snippet relative path.

Pipeline files look up snippets by tuple key instead of hardcoding paths.
Every value MUST resolve to a real file under `snippets/` — see
`tests/test_snippets_registry.py` for the regression check.
"""
from __future__ import annotations

from typing import Literal

AssetClass = Literal["index", "etf", "futures", "option", "generic"]
Scenario = Literal["fetch", "signal", "backtest", "metrics", "plot", "compile", "validate"]


# Layout: snippets are flat under `snippets/`. Keys mirror the pipeline's
# (asset_class, scenario) lookup; values are paths relative to the repo root.
SNIPPETS: dict[tuple[str, str], str] = {
    # --- index
    ("index", "fetch"): "snippets/fetch_index_bars.py",
    ("index", "signal"): "snippets/compute_signals_sma.py",
    ("index", "backtest"): "snippets/run_basic_backtest.py",
    # --- etf
    ("etf", "fetch"): "snippets/fetch_etf_bars.py",
    ("etf", "signal"): "snippets/compute_signals_rsi.py",
    # --- futures
    ("futures", "fetch"): "snippets/fetch_futures_continuous.py",
    ("futures", "backtest"): "snippets/run_basic_backtest.py",
    # --- option
    ("option", "fetch_chain"): "snippets/fetch_options_chain.py",
    ("option", "fetch_contract"): "snippets/fetch_option_contract.py",
    ("option", "short_put"): "snippets/option_strategy_short_put.py",
    ("option", "vertical_spread"): "snippets/option_strategy_vertical_spread.py",
    ("option", "iron_condor"): "snippets/option_strategy_iron_condor.py",
    ("option", "calendar_spread"): "snippets/option_strategy_calendar_spread.py",
    # --- shared scenarios (asset_class = generic)
    ("generic", "metrics"): "snippets/compute_metrics.py",
    ("generic", "plot_equity"): "snippets/plot_equity.py",
    ("generic", "plot_returns_heatmap"): "snippets/plot_returns_heatmap.py",
    ("generic", "plot_trade_markers"): "snippets/plot_trade_markers.py",
    ("generic", "plot_stats_panel"): "snippets/plot_stats_panel.py",
    ("generic", "validate"): "snippets/validate_data.py",
    ("generic", "compile"): "snippets/compile_notebook.py",
    ("generic", "backtest"): "snippets/run_basic_backtest.py",
    ("generic", "diagnostics"): "snippets/compute_diagnostics.py",
}


def lookup(asset_class: str, scenario: str) -> str | None:
    """Return the snippet path for (asset_class, scenario), or None when missing."""
    return SNIPPETS.get((asset_class, scenario))


def list_snippets(*, asset_class: str | None = None) -> list[tuple[str, str, str]]:
    """List all (asset_class, scenario, path) triples; filter by asset_class if given."""
    rows = [(a, s, p) for (a, s), p in SNIPPETS.items()]
    if asset_class is not None:
        rows = [r for r in rows if r[0] == asset_class]
    rows.sort()
    return rows
