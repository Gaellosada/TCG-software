"""Public surface for the TCG-claude (mongoDB) backtester library.

The lib is helpers, not gatekeepers. A ``strategy.py`` defines a top-level
``META`` dict + a ``compute_signal(bars, ctx)`` function (or an escape-hatch
``run(ctx)`` function). The lib loads bars per ``META``, calls the strategy,
and feeds the resulting signal into the engine. For options strategies, the
``run``-shape lets the strategy build legs via ``lib.options.build_legs``
(or one of the named structure helpers — vertical, iron_condor, ...) and
pass them to ``ctx.run_backtest`` itself.

Public surface (mirrors binance-backtester for cross-setup parity):

- ``run_strategy(strategy_module, *, workspace_path)`` — engine entry.
- ``StrategyContext`` — frozen dataclass passed to strategy functions.
- ``BacktestResult`` — dataclass returned by ``run_strategy``.
- ``indicators.*`` — vectorised primitives (sma, ema, rsi, breakout,
  rolling_vol, apply_direction, daily_pulse).
- ``options.*`` — options-leg helpers (``build_legs``, ``vertical``,
  ``iron_condor``, ``straddle``, ``strangle``, ``calendar``, ...).
- ``validate.run_probes(strategy_module, bars, result)`` — generic
  behavioural probes returning ``IntegrityReport``.
- ``plotting.PlotJob`` / ``plotting.BASELINE_PLOTS`` — plot registry.
- ``compile.build_notebook(workspace_path)`` — workspace -> notebook compile.
"""
from __future__ import annotations

from . import compile as compile
from . import (
    aliases,
    constants,
    data,
    data_load,
    diagnostics,
    engine,
    indicators,
    metrics,
    mongo,
    options,
    plotting,
    snippets_registry,
    types,
    validate,
)
from .engine import (
    BacktestResult,
    BacktestSpec,
    ExecutionConfig,
    OptionLeg,
    OptionLegSpec,
    SizingConfig,
    Trade,
    run_backtest,
)
from .strategy import StrategyContext, run_strategy
from .validate import IntegrityReport, run_probes

__all__ = [
    # Modules
    "indicators",
    "validate",
    "types",
    "diagnostics",
    "mongo",
    "data",
    "data_load",
    "options",
    "engine",
    "metrics",
    "plotting",
    "aliases",
    "constants",
    "snippets_registry",
    "compile",
    # Code-first surface
    "BacktestResult",
    "BacktestSpec",
    "ExecutionConfig",
    "OptionLeg",
    "OptionLegSpec",
    "SizingConfig",
    "Trade",
    "run_backtest",
    "StrategyContext",
    "run_strategy",
    "IntegrityReport",
    "run_probes",
]
