"""Code-first strategy entry point: ``run_strategy(strategy_module, ...)``.

The lib detects the strategy contract (``compute_signal`` vs ``run``) and
drives the engine accordingly. No closed taxonomy of strategy "kinds" — the
only dispatch is "which function did the user export".

A strategy module looks like::

    META = {
        "slug": "sma-cross-spy",
        "description": "...",
        "dates": {"start": "2020-01-01", "end": "2024-12-31"},
        "universe": ["SPY"],
        "benchmark": "SPY",
        "asset_class": "ETF",                  # optional, default "INDEX"
        "sizing": {"method": "fixed_fraction", "fraction": 1.0},
        "execution": {"slippage_bps": 1.0, "fees_bps": 0.5},
        # Optional: "tags": [...] — advisory only, never gatekeeping
    }

    def compute_signal(bars, ctx):
        # bars: PriceSeries; ctx: StrategyContext
        # return: 1-D float64 array, length == len(bars.dates)
        ...

Or — escape hatch — ``def run(ctx) -> BacktestResult`` which receives the
``StrategyContext`` (without bars pre-loaded) and is responsible for
returning a ``BacktestResult`` itself (after calling ``ctx.run_backtest`` or
computing it some other way). The ``run`` shape is the natural fit for
options strategies that build legs and pass them via ``BacktestSpec``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import numpy as np

from . import indicators as _indicators
from .data_load import PriceSeries
from .engine import (
    BacktestResult,
    BacktestSpec,
    ExecutionConfig,
    SizingConfig,
    run_backtest,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- ctx


@dataclass(frozen=True)
class StrategyContext:
    """Read-only context passed to ``compute_signal`` / ``run``.

    Fields:

    - ``workspace_path``: absolute Path to the strategy workspace.
    - ``meta``: the strategy's ``META`` dict (read-only by convention).
    - ``bars``: the loaded ``PriceSeries`` for the canonical instrument when
      the strategy uses ``compute_signal``; ``None`` for ``run``-shape
      strategies (which load their own bars via ``load_bars``).
    - ``logger``: a stdlib ``logging.Logger`` namespaced under the slug.
    - ``load_bars``: facade calling :func:`lib.data_load.fetch_*`.
    - ``load_option_chain``: facade calling :func:`lib.options.load_chain_pkl`
      / :func:`lib.options.load_chain` so options strategies can fetch a
      chain by date inside ``run`` without juggling Mongo handles.
    - ``run_backtest``: the engine entry; takes a ``BacktestSpec``.
    - ``indicators``: the indicator module (sma, ema, rsi, breakout, ...).
    - ``options``: the options helper module (vertical, iron_condor, ...,
      ``build_legs``).

    The lib provides this dataclass; agents never construct it directly.
    """

    workspace_path: Path
    meta: dict
    bars: PriceSeries | None
    logger: logging.Logger
    load_bars: Callable[..., PriceSeries] = field(repr=False)
    load_option_chain: Callable[..., Any] = field(repr=False)
    run_backtest: Callable[[BacktestSpec], BacktestResult] = field(repr=False)
    indicators: Any = field(repr=False)
    options: Any = field(repr=False)


# --------------------------------------------------------------------------- helpers


def _coerce_to_yyyymmdd(value: Any) -> int | None:
    """Coerce a date value (ISO string / int YYYYMMDD) to int YYYYMMDD."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Already YYYYMMDD-ish — assume so if it has 8 digits.
        if 19000101 <= value <= 99991231:
            return int(value)
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                d = datetime.strptime(s, "%Y-%m-%d").date()
                return d.year * 10000 + d.month * 100 + d.day
            # Try ISO datetime
            normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(normalized)
            return dt.year * 10000 + dt.month * 100 + dt.day
        except ValueError:
            return None
    return None


def _meta_dates_yyyymmdd(meta: dict) -> tuple[int, int]:
    dates = meta.get("dates") or {}
    if not isinstance(dates, dict):
        raise ValueError("META['dates'] must be a dict with 'start' and 'end'")
    start = _coerce_to_yyyymmdd(dates.get("start"))
    end = _coerce_to_yyyymmdd(dates.get("end"))
    if start is None or end is None:
        raise ValueError(
            "META['dates'].start / .end must be ISO date strings or YYYYMMDD "
            f"ints; got {dates!r}"
        )
    if start >= end:
        raise ValueError(f"META['dates'].start {start} >= end {end}")
    return start, end


def _primary_symbol(meta: dict) -> str:
    universe = meta.get("universe") or []
    if isinstance(universe, str):
        return universe
    if isinstance(universe, (list, tuple)) and universe:
        return str(universe[0])
    raise ValueError(
        "META['universe'] must be a non-empty list (or string); the first "
        "entry is treated as the canonical instrument for compute_signal"
    )


def _meta_asset_class(meta: dict) -> str:
    """Default asset_class is INDEX (covers SPX, NDX, ...). Override via META."""
    return str(meta.get("asset_class") or "INDEX").upper()


def _build_sizing(meta: dict) -> SizingConfig:
    cfg = meta.get("sizing") or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"META['sizing'] must be a dict, got {type(cfg).__name__}")
    method = cfg.get("method") or cfg.get("kind") or "fixed_fraction"
    return SizingConfig(
        method=method,
        fraction=float(cfg.get("fraction", 1.0)),
        vol_target_annual=cfg.get("vol_target_annual"),
        kelly_cap=cfg.get("kelly_cap"),
    )


def _build_execution(meta: dict) -> ExecutionConfig:
    cfg = meta.get("execution") or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"META['execution'] must be a dict, got {type(cfg).__name__}")
    fees = cfg.get("fees_bps")
    if fees is None:
        fees = cfg.get("fee_bps", 5.0)
    return ExecutionConfig(
        fees_bps=float(fees),
        slippage_bps=float(cfg.get("slippage_bps", 5.0)),
        fill_timing=cfg.get("fill_timing", "next_open"),
        look_ahead_shift=int(cfg.get("look_ahead_shift", 1)),
        risk_free_rate=float(cfg.get("risk_free_rate", 0.0)),
    )


# --------------------------------------------------------------------------- facades


def _load_bars_facade(
    instrument_id: str,
    *,
    asset_class: str = "INDEX",
    provider: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> PriceSeries:
    """Single-entry ``ctx.load_bars`` facade.

    Opens the read-only sync Mongo handle and dispatches via
    :func:`lib.data_load.load_bars` keyed by ``asset_class``. Strategies that
    need multi-instrument loading call this once per leg.
    """
    from . import data_load as _data_load
    from . import mongo as _mongo
    db = _mongo.sync_db()
    return _data_load.load_bars(
        db,
        asset_class=asset_class,
        instrument_id=instrument_id,
        provider=provider,
        start=start,
        end=end,
    )


def _load_option_chain_facade(*args, **kwargs):
    """``ctx.load_option_chain`` facade — proxy to :func:`lib.options.load_chain`.

    The signature matches :func:`lib.options.load_chain` (which opens the
    Mongo handle internally for the read-only path). Callers typically:

        chain_history = ctx.load_option_chain(root="SPX", start=20200101, end=20241231)
    """
    from . import options as _options
    from . import mongo as _mongo
    db = kwargs.pop("db", None) or _mongo.sync_db()
    return _options.load_chain(db, *args, **kwargs)


def _build_ctx(
    *,
    workspace_path: Path,
    meta: dict,
    bars: PriceSeries | None,
    slug: str,
) -> StrategyContext:
    from . import options as _options
    return StrategyContext(
        workspace_path=workspace_path,
        meta=meta,
        bars=bars,
        logger=logging.getLogger(f"tcg_backtester.strategy.{slug}"),
        load_bars=_load_bars_facade,
        load_option_chain=_load_option_chain_facade,
        run_backtest=run_backtest,
        indicators=_indicators,
        options=_options,
    )


def _load_bars_from_meta(meta: dict) -> PriceSeries:
    """Load primary-instrument bars from META."""
    asset_class = _meta_asset_class(meta)
    symbol = _primary_symbol(meta)
    start, end = _meta_dates_yyyymmdd(meta)
    return _load_bars_facade(symbol, asset_class=asset_class, start=start, end=end)


def _build_benchmark_bars(meta: dict) -> PriceSeries | None:
    bench = meta.get("benchmark")
    if not bench:
        return None
    default_ac = _meta_asset_class(meta)
    start, end = _meta_dates_yyyymmdd(meta)
    # Normalize via the public helper so strategy code, this facade, and
    # ad-hoc analysis snippets all agree on the canonical shape.
    from . import data as _data
    try:
        norm = _data.normalize_benchmark(bench, default_asset_class=default_ac)
    except ValueError as exc:
        log.warning("benchmark shape rejected (non-fatal): %s", exc)
        return None
    try:
        return _load_bars_facade(
            norm["symbol"], asset_class=norm["asset_class"], start=start, end=end,
        )
    except Exception as exc:  # noqa: BLE001 — benchmark is best-effort
        log.warning("benchmark load failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- main


def run_strategy(
    strategy_module: ModuleType,
    *,
    workspace_path: Path | str,
) -> BacktestResult:
    """Drive a code-first strategy end-to-end.

    Detects whether the module exposes ``run`` (escape hatch) or
    ``compute_signal`` (canonical). If both, ``run`` wins (a warning is
    logged). Returns a ``BacktestResult`` either way.

    Canonical path:
      1. Read ``META``.
      2. Load primary-instrument bars (and benchmark bars, if META declares one).
      3. Build ``StrategyContext`` (with ``bars`` populated).
      4. Call ``compute_signal(bars, ctx) -> NDArray[float64]``.
      5. Build a ``BacktestSpec`` from META + signal.
      6. Run ``run_backtest`` and return the result.

    Escape-hatch path:
      ``run(ctx)`` returns a ``BacktestResult`` directly. Useful when the
      strategy needs to load multi-leg bars, build options legs, fit a model,
      etc., before calling ``ctx.run_backtest`` itself.
    """
    if not hasattr(strategy_module, "META"):
        raise ValueError(
            "strategy module is missing top-level META dict; "
            "every code-first strategy must define META"
        )
    meta = getattr(strategy_module, "META")
    if not isinstance(meta, dict):
        raise ValueError(f"strategy.META must be a dict, got {type(meta).__name__}")

    workspace_path = Path(workspace_path).resolve()
    slug = str(meta.get("slug") or workspace_path.name)

    has_run = callable(getattr(strategy_module, "run", None))
    has_compute = callable(getattr(strategy_module, "compute_signal", None))

    if has_run and has_compute:
        log.warning(
            "strategy module exposes both 'run' and 'compute_signal'; "
            "using 'run' (escape hatch wins)"
        )

    if has_run:
        ctx = _build_ctx(
            workspace_path=workspace_path, meta=meta, bars=None, slug=slug,
        )
        result = strategy_module.run(ctx)
        if not isinstance(result, BacktestResult):
            raise TypeError(
                f"strategy.run must return a BacktestResult, got "
                f"{type(result).__name__}"
            )
        return result

    if not has_compute:
        raise ValueError(
            "strategy module must define either 'compute_signal(bars, ctx)' "
            "or 'run(ctx)'; neither was found"
        )

    bars = _load_bars_from_meta(meta)
    benchmark_bars = _build_benchmark_bars(meta)
    ctx = _build_ctx(
        workspace_path=workspace_path, meta=meta, bars=bars, slug=slug,
    )

    signal = strategy_module.compute_signal(bars, ctx)
    sig_arr = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig_arr.shape[0] != len(bars.dates):
        raise ValueError(
            f"compute_signal returned length {sig_arr.shape[0]} but bars has "
            f"length {len(bars.dates)}; the engine expects equal-length signal"
        )

    sizing = _build_sizing(meta)
    execution = _build_execution(meta)
    spec = BacktestSpec(
        bars=bars,
        signal=sig_arr,
        benchmark=benchmark_bars,
        execution=execution,
        sizing=sizing,
        capital_base=float(meta.get("capital_base", 100_000.0)),
        label=slug,
    )
    return run_backtest(spec)


__all__ = ["StrategyContext", "run_strategy"]
