"""Option helpers: BS pricer, chain selection, expiry helpers, multi-leg builders.

Multi-leg spread builders (`vertical`, `calendar`, `iron_condor`, `straddle`,
`strangle`, plus the dispatcher `build_spread`) emit `OptionLegSpec` tuples that
`lib.engine.run_backtest` consumes. The legacy `OptionLeg` (pre-priced array
exposure) is preserved for the short-put strategy snippet.
"""
from __future__ import annotations

import math
import pickle
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from . import data_load as _data_load
from .data_load import (
    OptionChainSnapshot,
    OptionContractSeries,
    PriceSeries,
    _OPTIONS_DOC_PROJECTION,
    load_option_chain_sync,
    load_option_contract_series_sync,
)


# ----------------------------------------------------------------------------- progress

class _LoadProgress:
    """Stdout progress emitter for long Mongo loads (load_chain, load_option_chain).

    Python's stdout is line-buffered when attached to a TTY but block-buffered
    when piped; `flush=True` forces visibility for both. `load_chain` over a
    multi-year SP_500 chain can take 20+ minutes server-side and emit no stdout
    today — agents have no signal whether the load is alive.

    Cap output at ~20 lines for typical loads so we don't spam: emit the first
    `min_records` records, then exponentially back off, then settle on
    `max_lines` evenly-spaced milestones thereafter, with a soft time-based
    floor (`min_seconds_between_emits`) to keep cadence visible on slow loads.

    Tracks counter via `tick(n=1)`; emits via `_should_emit()`.
    """

    def __init__(
        self,
        label: str,
        *,
        enabled: bool = True,
        min_seconds_between_emits: float = 5.0,
        max_lines: int = 20,
        stream: Any = None,
    ) -> None:
        self.label = label
        self.enabled = bool(enabled)
        self.min_seconds_between_emits = float(min_seconds_between_emits)
        self.max_lines = int(max_lines)
        self.stream = stream  # None -> sys.stdout (resolved at write time)
        self.count = 0
        self.lines_emitted = 0
        self._t_start = time.perf_counter()
        self._t_last_emit = self._t_start
        self._next_threshold = 1  # emit at 1, 2, 4, 8, 16, 32, ... initially

    def tick(self, n: int = 1) -> None:
        """Increment counter and emit if a milestone is hit. No-op when disabled."""
        if not self.enabled:
            return
        self.count += int(n)
        if self._should_emit():
            self._emit()

    def _should_emit(self) -> bool:
        if self.lines_emitted >= self.max_lines:
            # Past the line cap: only emit on a slow time-based heartbeat so
            # very-long loads still show signs of life without spamming.
            return (time.perf_counter() - self._t_last_emit) >= max(
                self.min_seconds_between_emits, 30.0
            )
        # Geometric thresholds (1, 2, 4, 8, ...): cheap, visible on small loads,
        # bounded on large ones (20 lines = ~1M records before the cap kicks in).
        if self.count >= self._next_threshold:
            return True
        # Heartbeat: even if the count threshold isn't hit, surface every N seconds.
        if (time.perf_counter() - self._t_last_emit) >= self.min_seconds_between_emits:
            return True
        return False

    def _emit(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._t_start
        rate = (self.count / elapsed) if elapsed > 0 else 0.0
        msg = f"[{self.label}] {self.count} records ({elapsed:.1f}s, {rate:.0f}/s)"
        out = self.stream if self.stream is not None else sys.stdout
        print(msg, file=out, flush=True)
        self.lines_emitted += 1
        self._t_last_emit = now
        # Step the geometric threshold forward.
        while self._next_threshold <= self.count:
            self._next_threshold *= 2

    def done(self) -> None:
        """Emit a final summary line. Always fires when enabled."""
        if not self.enabled:
            return
        elapsed = time.perf_counter() - self._t_start
        rate = (self.count / elapsed) if elapsed > 0 else 0.0
        msg = f"[{self.label}] done: {self.count} records in {elapsed:.1f}s ({rate:.0f}/s)"
        out = self.stream if self.stream is not None else sys.stdout
        print(msg, file=out, flush=True)
        self.lines_emitted += 1

_BASIS_DENOM = {"365": 365.0, "365.25": 365.25, "252": 252.0}


def _to_dt(yyyymmdd: int) -> date:
    n = int(yyyymmdd)
    return date(n // 10000, (n // 100) % 100, n % 100)


def _yyyymmdd(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def t_years(today: int, expiration: int, *, basis: Literal["365", "252", "365.25"] = "365") -> float:
    """Compute year fraction between two YYYYMMDD ints under the given day-count basis."""
    if basis not in _BASIS_DENOM:
        raise ValueError(f"unsupported basis: {basis!r}")
    days = (_to_dt(expiration) - _to_dt(today)).days
    return days / _BASIS_DENOM[basis]


def dte_in_days(today: int, expiration: int, *, calendar: str = "NYSE") -> int:
    """Business-day distance to expiration via the named exchange calendar (negative if expired).

    Falls back to calendar days only when the optional `pandas_market_calendars`
    dependency is missing or the named calendar / schedule lookup fails. Any
    other exception (programmer error in the calendar API, unexpected schedule
    shape) propagates so the caller sees the real failure rather than a silent
    semantic shift from business-days to calendar-days.
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        import pandas_market_calendars as mcal
    except ImportError:
        logger.debug(
            "pandas_market_calendars unavailable; falling back to calendar days for dte (today=%r exp=%r)",
            today, expiration,
        )
        return (_to_dt(int(expiration)) - _to_dt(int(today))).days
    try:
        cal = mcal.get_calendar(calendar)
        d_today = _to_dt(int(today))
        d_exp = _to_dt(int(expiration))
        if d_exp < d_today:
            sched = cal.schedule(start_date=d_exp.isoformat(), end_date=d_today.isoformat())
            return -int(len(sched))
        sched = cal.schedule(start_date=d_today.isoformat(), end_date=d_exp.isoformat())
        return int(len(sched)) - 1
    except (KeyError, ValueError) as e:
        logger.debug(
            "calendar=%r lookup failed (%s); falling back to calendar days (today=%r exp=%r)",
            calendar, e, today, expiration,
        )
        return (_to_dt(int(expiration)) - _to_dt(int(today))).days


def compute_greeks(
    spot: float,
    strike: float,
    t_years: float,
    r: float,
    sigma: float,
    option_type: Literal["C", "P"],
    *,
    q: float = 0.0,
    model: Literal["bs", "bsm"] = "bs",
) -> dict[str, float]:
    """Black-Scholes price + greeks; intrinsic-value collapse when DTE<=0."""
    if option_type not in ("C", "P"):
        raise ValueError(f"option_type must be 'C' or 'P': {option_type!r}")
    flag = "c" if option_type == "C" else "p"
    if t_years is None or t_years <= 0.0:
        intrinsic = max(spot - strike, 0.0) if option_type == "C" else max(strike - spot, 0.0)
        delta = 0.0
        if intrinsic > 0.0:
            delta = 1.0 if option_type == "C" else -1.0
        warnings.warn("compute_greeks called with t_years<=0; returning intrinsic value", stacklevel=2)
        return {
            "price": float(intrinsic),
            "delta": float(delta),
            "gamma": 0.0,
            "vega": 0.0,
            "theta": 0.0,
            "rho": 0.0,
            "iv": float(sigma),
        }
    if model == "bsm":
        from py_vollib.black_scholes_merton import black_scholes_merton as bsm_price
        from py_vollib.black_scholes_merton.greeks.analytical import delta as bsm_delta
        from py_vollib.black_scholes_merton.greeks.analytical import gamma as bsm_gamma
        from py_vollib.black_scholes_merton.greeks.analytical import rho as bsm_rho
        from py_vollib.black_scholes_merton.greeks.analytical import theta as bsm_theta
        from py_vollib.black_scholes_merton.greeks.analytical import vega as bsm_vega

        return {
            "price": float(bsm_price(flag, spot, strike, t_years, r, q, sigma)),
            "delta": float(bsm_delta(flag, spot, strike, t_years, r, q, sigma)),
            "gamma": float(bsm_gamma(flag, spot, strike, t_years, r, q, sigma)),
            "vega": float(bsm_vega(flag, spot, strike, t_years, r, q, sigma)),
            "theta": float(bsm_theta(flag, spot, strike, t_years, r, q, sigma)),
            "rho": float(bsm_rho(flag, spot, strike, t_years, r, q, sigma)),
            "iv": float(sigma),
        }
    from py_vollib.black_scholes import black_scholes as bs_price
    from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
    from py_vollib.black_scholes.greeks.analytical import gamma as bs_gamma
    from py_vollib.black_scholes.greeks.analytical import rho as bs_rho
    from py_vollib.black_scholes.greeks.analytical import theta as bs_theta
    from py_vollib.black_scholes.greeks.analytical import vega as bs_vega

    return {
        "price": float(bs_price(flag, spot, strike, t_years, r, sigma)),
        "delta": float(bs_delta(flag, spot, strike, t_years, r, sigma)),
        "gamma": float(bs_gamma(flag, spot, strike, t_years, r, sigma)),
        "vega": float(bs_vega(flag, spot, strike, t_years, r, sigma)),
        "theta": float(bs_theta(flag, spot, strike, t_years, r, sigma)),
        "rho": float(bs_rho(flag, spot, strike, t_years, r, sigma)),
        "iv": float(sigma),
    }


def implied_vol(
    spot: float,
    strike: float,
    t_years: float,
    r: float,
    price: float,
    option_type: Literal["C", "P"],
    *,
    q: float = 0.0,
) -> float | None:
    """Solve for IV; returns None if the solver fails (e.g. arbitrage-violating quote)."""
    if option_type not in ("C", "P"):
        raise ValueError(f"option_type must be 'C' or 'P': {option_type!r}")
    if t_years < 0:
        raise ValueError("t_years must be non-negative")
    flag = "c" if option_type == "C" else "p"
    import logging
    logger = logging.getLogger(__name__)
    try:
        from py_vollib.black_scholes.implied_volatility import implied_volatility as iv
        # py_vollib re-raises py_lets_be_rational's arbitrage exceptions
        # (BelowIntrinsicException / AboveMaximumException) verbatim. Their
        # common base is VolatilityValueException.
        from py_lets_be_rational.exceptions import VolatilityValueException
    except ImportError as exc:
        logger.debug("py_vollib unavailable for implied_vol_bs: %r", exc)
        return None
    try:
        return float(iv(price, spot, strike, t_years, r, flag))
    except VolatilityValueException as exc:
        # Quote violates BS arbitrage bounds (below intrinsic or above max);
        # no valid IV exists.
        logger.debug(
            "implied_vol_bs: arbitrage-violating quote: price=%r spot=%r strike=%r t=%r r=%r flag=%s err=%r",
            price, spot, strike, t_years, r, flag, exc,
        )
        return None
    except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError) as exc:
        # Numerical solver failure (degenerate inputs, root-finder didn't converge).
        logger.debug(
            "implied_vol_bs solver failed numerically: price=%r spot=%r strike=%r t=%r r=%r flag=%s err=%r",
            price, spot, strike, t_years, r, flag, exc,
        )
        return None


def select_atm_strike(
    chain: OptionChainSnapshot,
    spot: float,
    *,
    option_type: Literal["C", "P"] = "C",
) -> OptionContractSeries | None:
    """Pick the contract with strike nearest to spot (filtered by option_type)."""
    candidates = [c for c in chain.contracts if c.option_type == option_type]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.strike - spot))


def select_delta_target(
    chain: OptionChainSnapshot,
    spot: float,
    target_delta: float,
    *,
    option_type: Literal["C", "P"] = "C",
    r: float = 0.0,
    sigma_fallback: float = 0.20,
) -> OptionContractSeries | None:
    """Pick contract whose |delta - target_delta| is minimal; uses BS fallback when greeks absent."""
    candidates = [c for c in chain.contracts if c.option_type == option_type]
    if not candidates:
        return None
    asof = chain.asof_date
    best: tuple[float, OptionContractSeries] | None = None
    for c in candidates:
        row = c.rows[0] if c.rows else None
        d: float | None = None
        if row is not None and row.delta is not None and not math.isnan(row.delta):
            d = float(row.delta)
        else:
            tt = t_years(asof, c.expiration)
            sigma = sigma_fallback
            if row is not None and row.iv is not None and not math.isnan(row.iv) and row.iv > 0:
                sigma = float(row.iv)
            try:
                g = compute_greeks(spot, c.strike, tt, r, sigma, c.option_type)
                d = g["delta"]
            except ValueError:
                # Bad option_type or unsupported BS branch — skip this contract.
                # Other exceptions (e.g., math errors in py_vollib) propagate
                # so the caller sees the real failure with a stack trace.
                continue
        if d is None:
            continue
        score = abs(d - float(target_delta))
        if best is None or score < best[0]:
            best = (score, c)
    return best[1] if best is not None else None


def monthly_expirations(chain: OptionChainSnapshot) -> list[int]:
    """Return ascending unique expirations in the chain (one per month)."""
    seen: dict[tuple[int, int], int] = {}
    for c in chain.contracts:
        ym = (c.expiration // 10000, (c.expiration // 100) % 100)
        if ym not in seen or c.expiration < seen[ym]:
            seen[ym] = c.expiration
    return sorted(seen.values())


# ----------------------------------------------------------------------------- multi-leg

from .engine import (
    DeltaSelector,
    DteSelector,
    ExitRule,
    ExpirySelector,
    FixedExpirySelector,
    HoldToExpiration,
    OptionLegSpec,
    StrikeOffsetPctSelector,
)


def _strike_selector(strike: float, spot_hint: float):
    """Build a StrikeOffsetPctSelector from an absolute strike + a mandatory spot hint.

    The selector is matched against the chain at backtest time, so the hint only
    needs to be in the right ballpark — typically `chain.snapshots[0].spot` or
    the user's expected spot at intake. The hint is required (no default) because
    a wrong-magnitude default (e.g. 100 when SPX is 4500) silently picks a far
    OTM strike on thin chains. Pass `spot_hint = expected_spot` explicitly.
    """
    pct = (float(strike) - float(spot_hint)) / float(spot_hint)
    return StrikeOffsetPctSelector(pct_offset=pct)


def vertical(
    side: Literal["long", "short"],
    option_type: Literal["C", "P"],
    near_strike: float,
    far_strike: float,
    expiry: int,
    *,
    spot_hint: float,
    qty_units: int = 1,
    exit_rule: ExitRule | None = None,
) -> tuple[OptionLegSpec, ...]:
    """Build a 2-leg vertical spread.

    For a `long` call vertical: long the lower strike, short the higher strike.
    For a `long` put vertical: long the higher strike, short the lower strike.
    `short` reverses both sides.
    """
    rule: ExitRule = exit_rule or HoldToExpiration()
    expiry_sel: ExpirySelector = FixedExpirySelector(expiration=int(expiry))
    lower, higher = sorted([float(near_strike), float(far_strike)])
    if option_type == "C":
        long_strike, short_strike = (lower, higher) if side == "long" else (higher, lower)
    else:
        long_strike, short_strike = (higher, lower) if side == "long" else (lower, higher)
    long_leg = OptionLegSpec(
        leg_id=f"long_{option_type}_{int(long_strike)}",
        side="long",
        qty_units=qty_units,
        option_type=option_type,
        contract_selector=_strike_selector(long_strike, spot_hint),
        expiry_selector=expiry_sel,
        exit_rule=rule,
    )
    short_leg = OptionLegSpec(
        leg_id=f"short_{option_type}_{int(short_strike)}",
        side="short",
        qty_units=qty_units,
        option_type=option_type,
        contract_selector=_strike_selector(short_strike, spot_hint),
        expiry_selector=expiry_sel,
        exit_rule=rule,
    )
    return (long_leg, short_leg)


def calendar(
    side: Literal["long", "short"],
    option_type: Literal["C", "P"],
    strike: float,
    near_expiry: int,
    far_expiry: int,
    *,
    spot_hint: float,
    qty_units: int = 1,
    exit_rule: ExitRule | None = None,
) -> tuple[OptionLegSpec, ...]:
    """Build a 2-leg calendar spread (same strike, two expiries)."""
    rule: ExitRule = exit_rule or HoldToExpiration()
    near = OptionLegSpec(
        leg_id=f"{'short' if side == 'long' else 'long'}_{option_type}_near",
        side="short" if side == "long" else "long",
        qty_units=qty_units,
        option_type=option_type,
        contract_selector=_strike_selector(strike, spot_hint),
        expiry_selector=FixedExpirySelector(expiration=int(near_expiry)),
        exit_rule=rule,
    )
    far = OptionLegSpec(
        leg_id=f"{'long' if side == 'long' else 'short'}_{option_type}_far",
        side="long" if side == "long" else "short",
        qty_units=qty_units,
        option_type=option_type,
        contract_selector=_strike_selector(strike, spot_hint),
        expiry_selector=FixedExpirySelector(expiration=int(far_expiry)),
        exit_rule=rule,
    )
    return (near, far)


def iron_condor(
    short_call_strike: float,
    long_call_strike: float,
    short_put_strike: float,
    long_put_strike: float,
    expiry: int,
    *,
    spot_hint: float,
    qty_units: int = 1,
    exit_rule: ExitRule | None = None,
) -> tuple[OptionLegSpec, ...]:
    """Build a 4-leg iron condor (short call spread + short put spread)."""
    rule: ExitRule = exit_rule or HoldToExpiration()
    sel_exp = FixedExpirySelector(expiration=int(expiry))
    return (
        OptionLegSpec(
            leg_id="short_call",
            side="short",
            qty_units=qty_units,
            option_type="C",
            contract_selector=_strike_selector(short_call_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
        OptionLegSpec(
            leg_id="long_call",
            side="long",
            qty_units=qty_units,
            option_type="C",
            contract_selector=_strike_selector(long_call_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
        OptionLegSpec(
            leg_id="short_put",
            side="short",
            qty_units=qty_units,
            option_type="P",
            contract_selector=_strike_selector(short_put_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
        OptionLegSpec(
            leg_id="long_put",
            side="long",
            qty_units=qty_units,
            option_type="P",
            contract_selector=_strike_selector(long_put_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
    )


def straddle(
    strike: float,
    expiry: int,
    *,
    spot_hint: float,
    side: Literal["long", "short"] = "long",
    qty_units: int = 1,
    exit_rule: ExitRule | None = None,
) -> tuple[OptionLegSpec, ...]:
    """Build a long/short straddle (call + put at same strike)."""
    rule: ExitRule = exit_rule or HoldToExpiration()
    sel_exp = FixedExpirySelector(expiration=int(expiry))
    return (
        OptionLegSpec(
            leg_id=f"{side}_call",
            side=side,
            qty_units=qty_units,
            option_type="C",
            contract_selector=_strike_selector(strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
        OptionLegSpec(
            leg_id=f"{side}_put",
            side=side,
            qty_units=qty_units,
            option_type="P",
            contract_selector=_strike_selector(strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
    )


def strangle(
    call_strike: float,
    put_strike: float,
    expiry: int,
    *,
    spot_hint: float,
    side: Literal["long", "short"] = "long",
    qty_units: int = 1,
    exit_rule: ExitRule | None = None,
) -> tuple[OptionLegSpec, ...]:
    """Build a strangle (OTM call + OTM put at different strikes)."""
    rule: ExitRule = exit_rule or HoldToExpiration()
    sel_exp = FixedExpirySelector(expiration=int(expiry))
    return (
        OptionLegSpec(
            leg_id=f"{side}_call",
            side=side,
            qty_units=qty_units,
            option_type="C",
            contract_selector=_strike_selector(call_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
        OptionLegSpec(
            leg_id=f"{side}_put",
            side=side,
            qty_units=qty_units,
            option_type="P",
            contract_selector=_strike_selector(put_strike, spot_hint),
            expiry_selector=sel_exp,
            exit_rule=rule,
        ),
    )


_SPREAD_BUILDERS = {
    "vertical": vertical,
    "calendar": calendar,
    "iron_condor": iron_condor,
    "straddle": straddle,
    "strangle": strangle,
}


def build_spread(spread_type: str, **kwargs) -> tuple[OptionLegSpec, ...]:
    """Dispatch on `spread_type` to the correct builder; returns OptionLegSpec tuple."""
    fn = _SPREAD_BUILDERS.get(str(spread_type))
    if fn is None:
        raise ValueError(
            f"unknown spread_type {spread_type!r}; supported: {sorted(_SPREAD_BUILDERS)}"
        )
    return fn(**kwargs)


# --------------------------------------------------------------------------- data fetch + npz IO


@dataclass(frozen=True)
class OptionChainHistory:
    """Multi-day chain view: list of single-day snapshots over [start, end]."""

    root: str
    start: int
    end: int
    snapshots: tuple[OptionChainSnapshot, ...]

    @property
    def n_observations(self) -> int:
        """Total contract-day rows across all snapshots."""
        return int(sum(len(s.contracts) for s in self.snapshots))

    @property
    def n_contracts(self) -> int:
        """Distinct contract_ids across the history."""
        ids: set[str] = set()
        for s in self.snapshots:
            for c in s.contracts:
                ids.add(c.contract_id)
        return len(ids)


def list_expirations(
    db: Any,
    root: str,
    *,
    provider: str | None = None,
) -> list[int]:
    """Return sorted unique YYYYMMDD expirations available in the OPT_<root> collection."""
    from .data_load import _coll_names
    from . import mongo as _mongo

    coll_name = f"OPT_{root.upper()}"
    coll_names = _mongo.sync_run(_coll_names(db))
    if coll_name not in coll_names:
        raise ValueError(f"unknown option root: {root!r}")
    coll = db[coll_name]
    filter_q: dict[str, Any] | None = None
    if provider is not None:
        filter_q = {f"eodDatasStart.{provider}": {"$exists": True}}
    raw = _mongo.sync_run(coll.distinct("expiration", filter=filter_q))
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


_DEFAULT_INSTRUMENT_TO_OPTION_ROOT: dict[str, str] = {
    # Equity-index spot tickers -> OPT_* collection roots used by the IVOL feed.
    "SPX": "SP_500",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "VIX": "VIX",
    "RUT": "RUSSELL_2000",
    "NDX": "NASDAQ_100",
}


def chain_args_from_spec(
    spec: dict,
    *,
    root_alias: dict[str, str] | None = None,
    spot_hint: float | None = None,
) -> dict:
    """Translate a STRATEGY.yaml-shaped spec to ``load_chain`` kwargs.

    Returns ``{root, start, end, dte_min, dte_max, right, use_aggregation: True,
    strike_min, strike_max, expiration_cycle}`` suitable for
    ``load_chain(db, **chain_args_from_spec(spec, spot_hint=...))``.

    Translation rules:
    - ``start`` / ``end`` come from ``spec['date_range']``.
    - ``signals.type`` MUST be ``'option_strategy'`` (else ``ValueError``).
    - For each leg in ``spec['signals']['legs']``, ``expiry_selector.kind``
      MUST be one of ``'dte'``, ``'weekly'``, or ``'monthly'``. ``'weekly'``
      expands to DTE band ``[3, 10]`` and ``'monthly'`` expands to ``[25, 45]``
      to match the ``WeeklySelector`` / ``MonthlySelector`` engine semantics.
      Other kinds (``'absolute'``, ``'next_expiry'``, ``'fixed'``, etc.) raise
      ``ValueError`` rather than being silently ignored — those need a chain
      load with explicitly-computed DTE bounds.
    - ``dte_min = max(0, min(target_dte - tolerance_days))`` across legs.
      ``dte_max = max(target_dte + tolerance_days)`` across legs. (Union of
      per-leg DTE windows; the chain must cover every leg.)
    - ``right``: union of ``leg['option_type']`` — all ``'P'`` -> ``'P'``,
      all ``'C'`` -> ``'C'``, mixed -> ``'BOTH'``.
    - ``root``: takes the single tradable in ``spec['universe']`` (multiple
      tradables -> ``ValueError``). Resolves via ``root_alias`` if supplied,
      otherwise via the built-in equity-index map
      (``_DEFAULT_INSTRUMENT_TO_OPTION_ROOT``: SPX -> SP_500, etc.). When the
      ``instrument_id`` is unknown to both maps, the value is passed verbatim
      and a ``UserWarning`` is emitted via ``warnings.warn``.
    - ``use_aggregation=True`` is hardcoded — production callers should never
      think about this flag (Wave 2 perf fix; see ``load_chain`` docstring).

    Focused-query derivation (the "don't load all strikes" principle):
    - ``expiration_cycle``: derived from the legs' ``expiry_selector.kind``.
      All-weekly legs → ``"W"``; all-monthly legs → ``"M"``; otherwise
      ``None`` (no cycle filter, e.g. mixed/dte-band legs).
    - ``strike_min`` / ``strike_max``: derived from ``spot_hint`` per the
      union of leg ``contract_selector`` kinds:
        * ``delta`` (any |target_delta|): ±30% band around spot — generous
          enough to cover deep OTM through deep ITM rebalances.
        * ``atm``: ±15% band around spot.
        * ``pct_offset``: ``spot * (1 + pct_offset) ± 15%`` per leg, then
          union.
        * ``moneyness``: ``spot * moneyness ± 15%`` per leg, then union.
      When ``spot_hint is None`` the strike band is left ``None``/``None``
      (no push-down) and a ``UserWarning`` is emitted naming the missing
      hint — fresh agents see exactly why their load is slow. The engine
      still works without spot_hint; callers who care about the 21-min ->
      sub-10-min speedup must pass ``spot_hint``.

    Args:
        spec: STRATEGY.yaml-shaped dict.
        root_alias: optional ``instrument_id -> OPT_<root>`` override.
        spot_hint: representative spot price for strike-band derivation. The
            value need only be in the ballpark (within ~30% of mid-window
            spot) since the band is generous; for SPX 2021-2025 a value
            like ``4500.0`` works for the entire 4-year window.

    Raises:
        ValueError: ``signals.type != 'option_strategy'``; empty legs;
            unsupported ``expiry_selector.kind``; multiple tradables.
    """
    if not isinstance(spec, dict):
        raise ValueError(f"spec must be a dict, got {type(spec).__name__}")
    signals = spec.get("signals")
    if not isinstance(signals, dict):
        raise ValueError("spec['signals'] missing or not a dict")
    if signals.get("type") != "option_strategy":
        raise ValueError(
            f"chain_args_from_spec only supports signals.type='option_strategy', "
            f"got {signals.get('type')!r}"
        )
    legs = signals.get("legs") or []
    if not legs:
        raise ValueError("spec['signals']['legs'] is empty; nothing to translate")

    # Date range
    date_range = spec.get("date_range") or {}
    if "start" not in date_range or "end" not in date_range:
        raise ValueError("spec['date_range'] must contain 'start' and 'end'")
    start = int(date_range["start"])
    end = int(date_range["end"])

    # DTE union across legs
    lows: list[int] = []
    highs: list[int] = []
    rights: set[str] = set()
    expiry_kinds: list[str] = []
    contract_selectors: list[dict] = []
    # Bands matching engine.WeeklySelector / engine.MonthlySelector.
    _BAND_BY_KIND: dict[str, tuple[int, int]] = {
        "weekly": (3, 10),
        "monthly": (25, 45),
    }
    for i, leg in enumerate(legs):
        sel = leg.get("expiry_selector")
        if not isinstance(sel, dict):
            raise ValueError(f"leg[{i}].expiry_selector missing or not a dict")
        kind = sel.get("kind")
        if kind == "dte":
            if "target_dte" not in sel or "tolerance_days" not in sel:
                raise ValueError(
                    f"leg[{i}].expiry_selector requires 'target_dte' and 'tolerance_days'"
                )
            target = int(sel["target_dte"])
            tol = int(sel["tolerance_days"])
            lows.append(target - tol)
            highs.append(target + tol)
        elif kind in _BAND_BY_KIND:
            band_lo, band_hi = _BAND_BY_KIND[kind]
            lows.append(band_lo)
            highs.append(band_hi)
        else:
            raise ValueError(
                f"leg[{i}].expiry_selector.kind={kind!r} is unsupported by "
                f"chain_args_from_spec; supported kinds: 'dte', 'weekly', "
                f"'monthly'. For 'fixed'/'absolute'/'next_expiry' selectors, "
                f"compute DTE bounds manually before calling load_chain."
            )
        expiry_kinds.append(kind)
        otype = leg.get("option_type")
        if otype not in ("C", "P"):
            raise ValueError(
                f"leg[{i}].option_type must be 'C' or 'P', got {otype!r}"
            )
        rights.add(otype)
        # Capture contract_selector for strike-band derivation. Default to
        # delta-style {} when missing — that triggers the most-generous band
        # which is the safer fallback for ambiguous specs.
        cs = leg.get("contract_selector")
        contract_selectors.append(cs if isinstance(cs, dict) else {})
    dte_min = max(0, min(lows))
    dte_max = max(highs)

    # Right aggregation
    if rights == {"P"}:
        right: Literal["C", "P", "BOTH"] = "P"
    elif rights == {"C"}:
        right = "C"
    else:
        right = "BOTH"

    # Root resolution from universe
    universe = spec.get("universe") or []
    tradables = [
        u for u in universe
        if isinstance(u, dict) and (u.get("role") == "tradable" or "role" not in u)
    ]
    # If 'role' is unset across the universe, treat all entries as tradables (legacy specs).
    if not tradables:
        tradables = list(universe)
    if len(tradables) != 1:
        raise ValueError(
            f"chain_args_from_spec requires exactly one tradable in spec['universe']; "
            f"found {len(tradables)}. Single-root pipeline only — multi-root needs a "
            f"future relaxation."
        )
    instrument_id = tradables[0].get("instrument_id")
    if not instrument_id:
        raise ValueError("tradable universe entry missing 'instrument_id'")

    # Alias precedence: caller-supplied > built-in equity-index map > verbatim+warn.
    if root_alias is not None and instrument_id in root_alias:
        root = root_alias[instrument_id]
    elif instrument_id in _DEFAULT_INSTRUMENT_TO_OPTION_ROOT:
        root = _DEFAULT_INSTRUMENT_TO_OPTION_ROOT[instrument_id]
    else:
        warnings.warn(
            f"chain_args_from_spec: no option-root alias for instrument_id="
            f"{instrument_id!r}; passing verbatim to load_chain (will fail if "
            f"OPT_{instrument_id} is not a real collection). Pass `root_alias=` "
            f"to override.",
            stacklevel=2,
        )
        root = instrument_id

    # ------------------------------------------------------------------
    # Focused-query derivation: expiration_cycle + strike_min/strike_max.
    # ------------------------------------------------------------------
    # Cycle: only push when ALL legs agree on a single cycle ("W" or "M").
    # Mixed/dte legs leave it None so the chain still covers every cycle.
    if all(k == "weekly" for k in expiry_kinds):
        expiration_cycle = "W"
    elif all(k == "monthly" for k in expiry_kinds):
        expiration_cycle = "M"
    else:
        expiration_cycle = None

    # Strike band: per leg, derive a per-leg [lo, hi] using contract_selector
    # kind + spot_hint, then take the union (min lo, max hi).
    strike_min = None
    strike_max = None
    if spot_hint is not None:
        spot = float(spot_hint)
        if not (spot > 0.0):
            raise ValueError(
                f"spot_hint must be > 0; got {spot_hint!r}"
            )
        # Per-selector-kind bands. The principle: generous on delta (the
        # delta-target moves with vol) so a single band covers a 4-year
        # SPX backtest; tight on pct_offset / moneyness because those are
        # explicitly anchored.
        DELTA_BAND = 0.30   # ±30% drift-tolerant band (matches sibling-stack STRIKE_DRIFT_BAND)
        ATM_BAND = 0.15     # ±15% around spot
        ANCHOR_BAND = 0.15  # ±15% around the explicitly-anchored target
        per_leg: list[tuple[float, float]] = []
        for cs in contract_selectors:
            kind = cs.get("kind") if cs else None
            if kind == "delta" or kind is None:
                # Default: delta-style (most generous). `kind=None` happens
                # when the spec doesn't specify a contract_selector — be
                # conservative and assume the strategy may rebalance widely.
                per_leg.append((spot * (1.0 - DELTA_BAND), spot * (1.0 + DELTA_BAND)))
            elif kind == "atm":
                per_leg.append((spot * (1.0 - ATM_BAND), spot * (1.0 + ATM_BAND)))
            elif kind == "pct_offset":
                pct = float(cs.get("pct_offset", 0.0))
                anchor = spot * (1.0 + pct)
                per_leg.append((anchor * (1.0 - ANCHOR_BAND), anchor * (1.0 + ANCHOR_BAND)))
            elif kind == "moneyness":
                m = float(cs.get("moneyness", 1.0))
                anchor = spot * m
                per_leg.append((anchor * (1.0 - ANCHOR_BAND), anchor * (1.0 + ANCHOR_BAND)))
            else:
                # Unknown selector kind — fall back to the generous delta
                # band rather than skipping. Better a slightly-wider band
                # than silently dropping the push-down.
                per_leg.append((spot * (1.0 - DELTA_BAND), spot * (1.0 + DELTA_BAND)))
        strike_min = float(min(lo for lo, _ in per_leg))
        strike_max = float(max(hi for _, hi in per_leg))
    else:
        # No spot_hint — emit a one-shot warning so a fresh agent sees why
        # the multi-year load is slow. The engine still works without this;
        # the warning is the documented escape hatch.
        warnings.warn(
            "chain_args_from_spec: spot_hint not supplied — strike_min/max "
            "left unbounded. Multi-year option chain loads will scan all "
            "strikes (~21min for 4yr SPX). Pass `spot_hint=<approx_spot>` "
            "to push a server-side strike band and cut load time ~10x.",
            stacklevel=2,
        )

    return {
        "root": root,
        "start": start,
        "end": end,
        "dte_min": dte_min,
        "dte_max": dte_max,
        "right": right,
        "strike_min": strike_min,
        "strike_max": strike_max,
        "expiration_cycle": expiration_cycle,
        "use_aggregation": True,
    }


def load_chain(
    db: Any,
    *,
    root: str,
    start: int,
    end: int,
    dte_min: int = 0,
    dte_max: int = 365,
    right: Literal["C", "P", "BOTH"] = "BOTH",
    strike_min: float | None = None,
    strike_max: float | None = None,
    expiration_cycle: Literal["M", "W"] | None = None,
    provider: str | None = None,
    underlying_id: str | None = None,
    underlying_collection: str | None = None,
    use_aggregation: bool = True,
    progress: bool = True,
) -> OptionChainHistory:
    """Build an OptionChainHistory over `[start, end]` in a single Mongo batch.

    One collection scan keyed by `eodDatas{Start,End}.<provider>` window overlap;
    each matched contract has its eodDatas rows walked once and bucketed by
    as-of date. Spot per as-of date is loaded in one underlying bar query when
    `underlying_id` is supplied.

    When to set `use_aggregation=True`: always for production / agent use.
    Post-Wave-2, aggregation trims eodDatas/eodGreeks server-side via
    `$project + $filter` and is the only path that meets sub-30s budgets on
    the OPT_SP_500 collection. The find+projection path is kept for narrow
    correctness-equivalence tests on small fixtures only.

    DTE bound semantics: calendar days (not trading days). The lower bound is
    pushed server-side as `expiration >= start + dte_min` via `timedelta`
    calendar-day arithmetic — NOT YYYYMMDD subtraction (which drops valid
    expirations at month boundaries). Upper bound: `expiration <= end + dte_max`.
    Sentinel pair `(0, 365)` means "no push-down".

    Right semantics: `'P'` / `'C'` are case-insensitive at the regex level
    (covers the OPT_VIX lowercase `type` quirk). `'BOTH'` returns the union
    and skips the type filter entirely.

    Provider auto-resolution: `provider=None` (default) lets `_pick_provider`
    walk the priority list (IVOLATILITY for OPT_* equity / index roots,
    CBOE first for OPT_VIX). Pass an explicit string only to force a
    non-default provider.

    Performance characteristics (post-Wave-2 fix): Q4 2024 SP_500 puts at
    DTE [20, 40] returns in ~3-5s on a warm Mongo working set; three-year
    SP_500 puts at DTE [20, 40] returns in ~100-150s.

    Cold-from-Mongo-restart caveat: if the Mongo daemon was just restarted
    and the OPT collection's working set is fully evicted, the first call
    may briefly land in the 90s+ range until pages are faulted in. The
    structural fix is a compound index on `(type, expiration,
    eodDatasStart.<provider>, eodDatasEnd.<provider>)`, but `lib/mongo.py`'s
    read-only proxy intentionally blocks `create_index` — the index must be
    created out-of-band by ops.

    Args:
        db: Mongo handle (sync or async-wrapped via `lib.mongo`).
        root: Option root, e.g. ``"SP_500"``, ``"VIX"`` (case-insensitive).
        start, end: Inclusive YYYYMMDD bounds of the as-of window.
        dte_min: Lower DTE bound (calendar days). 0 = no lower bound.
        dte_max: Upper DTE bound (calendar days). 365 = no upper bound.
        right: ``'C'`` | ``'P'`` | ``'BOTH'``.
        strike_min: Optional inclusive lower strike bound. When supplied,
            pushes ``{strike: {$gte}}`` server-side. Use a generous band around
            spot (e.g. ±30%) to cut OPT_SP_500's ~4,000 strikes to a few hundred.
        strike_max: Optional inclusive upper strike bound. Pushes
            ``{strike: {$lte}}`` server-side.
        expiration_cycle: Optional ``"M"`` (monthly) or ``"W"`` (weekly).
            Pushes ``{_id.expirationCycle: <cycle>}`` server-side, which on
            the OPT_SP_500 composite ``_id`` index cuts contracts ~4-5x for
            single-cycle strategies.
        provider: Force a specific eodDatas provider key; ``None`` auto-picks.
        underlying_id: Optional spot loader hint (single bar query).
        underlying_collection: Override for the underlying collection name.
        use_aggregation: ``True`` = server-side row trim (production default).

    Returns:
        ``OptionChainHistory`` with one snapshot per as-of date (sorted).

    Raises:
        ValueError: ``right`` not in {C, P, BOTH}, or unknown ``root``.
    """
    from .data_load import (
        _coll_names,
        _greeks_for_date,
        _option_type_from_doc,
        _parse_expiration,
        _pick_provider,
        _row_from_doc,
        _serialize_id,
        load_index_bars_sync,
    )
    from . import mongo as _mongo

    if right not in ("C", "P", "BOTH"):
        raise ValueError(f"right must be 'C'|'P'|'BOTH', got {right!r}")
    otype_filter: Literal["C", "P"] | None = None if right == "BOTH" else right  # type: ignore[assignment]

    coll_name = f"OPT_{root.upper()}"
    coll_names = _mongo.sync_run(_coll_names(db))
    if coll_name not in coll_names:
        raise ValueError(f"unknown option root: {root!r}")

    coll = db[coll_name]

    # Resolve provider via a single sample-doc peek.
    sample = _mongo.sync_run(coll.find_one({}, projection=_OPTIONS_DOC_PROJECTION))
    if sample is None:
        return OptionChainHistory(root=root.upper(), start=int(start), end=int(end), snapshots=())
    try:
        actual_provider = _pick_provider(sample, provider, collection=coll_name)
    except LookupError:
        return OptionChainHistory(root=root.upper(), start=int(start), end=int(end), snapshots=())

    # Server-side window-overlap query. Fall back to {} when the collection
    # does not carry eodDatasStart/End fields (legacy / synthetic fixtures).
    query: dict[str, Any] = {
        f"eodDatasStart.{actual_provider}": {"$lte": int(end)},
        f"eodDatasEnd.{actual_provider}": {"$gte": int(start)},
    }
    # A1: server-side type regex (case-insensitive; covers OPT_VIX lowercase).
    if otype_filter is not None:
        c = otype_filter
        query["type"] = {"$regex": f"^[{c.upper()}{c.lower()}]"}
    # A2: server-side expiration range when DTE bounds materially narrow.
    # We compute a real calendar-day upper bound so YYYYMMDD month-boundary
    # arithmetic does not silently drop valid expirations (day 32 is invalid).
    if dte_min > 0 or dte_max < 365:
        # Tightened lower bound: an expiration exp can only yield a row whose
        # DTE >= dte_min if exp >= start + dte_min (calendar days). Mirroring
        # the upper-bound construction with timedelta avoids YYYYMMDD month-
        # boundary arithmetic dropping valid expirations. Saves ~20% of
        # matched docs on tight DTE windows.
        start_dt = _to_dt(int(start))
        lo_dt = start_dt + timedelta(days=int(dte_min))
        exp_lo = _yyyymmdd(lo_dt)
        end_dt = _to_dt(int(end))
        hi_dt = end_dt + timedelta(days=int(dte_max))
        exp_hi = _yyyymmdd(hi_dt)
        query["expiration"] = {"$gte": exp_lo, "$lte": exp_hi}
    # Server-side strike-band filter. Mirror `load_option_chain`'s A5 push-down:
    # for delta/moneyness/ATM strategies the caller (or `chain_args_from_spec`)
    # supplies a generous spot-relative band, cutting OPT_SP_500's ~4,000
    # strikes to ~200-400 candidates before the cursor reads anything.
    if strike_min is not None or strike_max is not None:
        strike_pred: dict[str, Any] = {}
        if strike_min is not None:
            strike_pred["$gte"] = float(strike_min)
        if strike_max is not None:
            strike_pred["$lte"] = float(strike_max)
        query["strike"] = strike_pred
    # Server-side expiration-cycle filter. The OPT_SP_500 `_id` is a composite
    # `{internalSymbol, expirationCycle}` document; sub-field dot-notation
    # against the auto compound `_id` index makes this filter near-free.
    # "M" = standard monthly (third-Friday), "W" = weekly. SPX has both
    # cycles + EOM/quarterlies; scoping to "W" or "M" alone cuts the doc
    # count ~4-5x for single-cycle strategies.
    if expiration_cycle is not None:
        if expiration_cycle not in ("M", "W"):
            raise ValueError(
                f"expiration_cycle must be 'M', 'W', or None; got {expiration_cycle!r}"
            )
        query["_id.expirationCycle"] = expiration_cycle

    found = _mongo.sync_run(coll.count_documents(query))
    if found == 0:
        # Strip narrowing filters and retry (legacy / synthetic fixtures path).
        query = {}

    # Spot map per as-of date. Single bar query. Built BEFORE iterating the
    # contract cursor so the streaming bucket loop below has the spot map
    # available to attach to snapshots after iteration.
    # The caller passing `underlying_id=X` clearly wants spot data — surfacing
    # a loader failure here is correct (was previously swallowed silently,
    # producing OptionChainSnapshot with spot=None for every date and silently
    # degrading downstream delta-target selection to BS-fallback IV → strike-only
    # matching). Per P0-B, errors propagate.
    spot_by_date: dict[int, float] = {}
    if underlying_id is not None:
        und_coll = underlying_collection or "INDEX"
        from .data_load import _make_sync, load_bars  # noqa: F401
        from .data_load import (
            load_index_bars,
            load_etf_bars,
            load_fund_bars,
        )
        loader_map = {
            "INDEX": load_index_bars_sync,
        }
        loader = loader_map.get(und_coll.upper(), load_index_bars_sync)
        bars = loader(db, underlying_id, start=int(start), end=int(end))
        for d, c in zip(bars.dates, bars.close):
            spot_by_date[int(d)] = float(c)

    # Bucket per as-of-date. Built incrementally as docs stream from Mongo so
    # the full doc list never needs to live in memory at once on multi-year
    # queries (50K+ docs). Each doc is GC-eligible after _bucket_doc returns.
    per_date: dict[int, list[OptionContractSeries]] = {}

    def _bucket_doc(doc: dict) -> None:
        """Extract in-window rows from a single OPT doc and append to per_date.

        Mutates `per_date` in the enclosing scope. Skips docs with malformed
        expiration/option-type/provider, and rows whose date falls outside
        [start, end] or violates the DTE bounds.
        """
        try:
            doc_exp = _parse_expiration(doc.get("expiration", 0))
        except ValueError:
            return
        try:
            otype = _option_type_from_doc(doc)
        except ValueError:
            return
        if otype_filter is not None and otype != otype_filter:
            return
        try:
            doc_provider = _pick_provider(doc, provider or actual_provider, collection=coll_name)
        except LookupError:
            return
        eod = doc.get("eodDatas") or {}
        if isinstance(eod, dict):
            eod_rows = eod.get(doc_provider) or []
        else:
            eod_rows = list(eod)
        strike = float(doc.get("strike", 0.0) or 0.0)
        contract_id = str(doc.get("contractId") or _serialize_id(doc.get("_id")))
        for raw_row in eod_rows:
            try:
                d = int(raw_row.get("date", 0))
            except (TypeError, ValueError):
                continue
            if d < int(start) or d > int(end):
                continue
            if doc_exp < d:
                continue
            try:
                dte = (_to_dt(int(doc_exp)) - _to_dt(d)).days
            except (TypeError, ValueError) as exc:
                # Malformed YYYYMMDD ints (out-of-range month/day, non-numeric)
                # — skip the row rather than abort the whole chain load.
                import logging
                logging.getLogger(__name__).debug(
                    "load_chain: skipping row with bad date(s) doc_exp=%r d=%r: %r",
                    doc_exp, d, exc,
                )
                continue
            if dte < dte_min or dte > dte_max:
                continue
            greeks = _greeks_for_date(doc, d, provider=doc_provider, collection=coll_name)
            row = _row_from_doc(d, raw_row, greeks)
            cs = OptionContractSeries(
                root=root.upper(),
                contract_id=contract_id,
                strike=strike,
                expiration=int(doc_exp),
                option_type=otype,
                rows=(row,),
            )
            per_date.setdefault(d, []).append(cs)

    # Progress emitter: visible heartbeat on long Mongo loads. Off-by-default
    # is wrong here because the silent-21-min load is the actual friction;
    # callers can set `progress=False` for unit tests that capture stdout.
    _prog = _LoadProgress(
        label=f"load_chain {root.upper()}",
        enabled=bool(progress),
    )

    if use_aggregation and query:
        # A3: server-side row trim via $project + $filter on eodDatas/eodGreeks
        # provider arrays. Cuts wire bytes 4-8x on multi-year backtests.
        proj: dict[str, Any] = {
            "_id": 1,
            "contractId": 1,
            "expiration": 1,
            "strike": 1,
            "type": 1,
            "optionType": 1,
            "rootUnderlying": 1,
            "underlying": 1,
            "underlyingSymbol": 1,
            "contractSize": 1,
            "currency": 1,
            "eodDatasStart": 1,
            "eodDatasEnd": 1,
            "eodDatas": {
                actual_provider: {
                    "$filter": {
                        "input": {"$ifNull": [f"$eodDatas.{actual_provider}", []]},
                        "as": "r",
                        "cond": {"$and": [
                            {"$gte": ["$$r.date", int(start)]},
                            {"$lte": ["$$r.date", int(end)]},
                        ]},
                    }
                }
            },
            "eodGreeks": {
                actual_provider: {
                    "$filter": {
                        "input": {"$ifNull": [f"$eodGreeks.{actual_provider}", []]},
                        "as": "g",
                        "cond": {"$and": [
                            {"$gte": ["$$g.date", int(start)]},
                            {"$lte": ["$$g.date", int(end)]},
                        ]},
                    }
                }
            },
        }
        # Post-$project $match: drop docs whose trimmed eodDatas array is
        # empty. The window-overlap pre-match already guarantees at least one
        # in-window date for non-edge cases, but this is cheap hygiene that
        # prevents zero-row docs leaking through edge-window scenarios (and
        # keeps the cursor payload tighter on the 3-year query).
        pipeline = [
            {"$match": query},
            {"$project": proj},
            {"$match": {f"eodDatas.{actual_provider}.0": {"$exists": True}}},
        ]

        async def _stream_agg_bucket() -> None:
            cursor = coll.aggregate(pipeline, allowDiskUse=True)
            async for d in cursor:
                _bucket_doc(d)
                _prog.tick()

        _mongo.sync_run(_stream_agg_bucket())
    else:
        async def _stream_find_bucket() -> None:
            # A4: inclusion projection drops intradayDatas + unused fields.
            async for d in coll.find(query, projection=_OPTIONS_DOC_PROJECTION):
                _bucket_doc(d)
                _prog.tick()

        _mongo.sync_run(_stream_find_bucket())

    _prog.done()

    snapshots: list[OptionChainSnapshot] = []
    for d in sorted(per_date.keys()):
        snapshots.append(
            OptionChainSnapshot(
                root=root.upper(),
                asof_date=d,
                spot=spot_by_date.get(d),
                contracts=tuple(per_date[d]),
            )
        )
    return OptionChainHistory(
        root=root.upper(), start=int(start), end=int(end), snapshots=tuple(snapshots)
    )


def save_chain_pkl(history: OptionChainHistory, path: str | Path) -> None:
    """Persist an OptionChainHistory to a pickle file (use a `.pkl` extension)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(history, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_chain_pkl(path: str | Path) -> OptionChainHistory:
    """Reverse of `save_chain_pkl` — returns the OptionChainHistory."""
    with open(Path(path), "rb") as f:
        obj = pickle.load(f)
    if not isinstance(obj, OptionChainHistory):
        raise ValueError(f"file at {path} is not an OptionChainHistory pickle")
    return obj


def save_contract_pkl(series: OptionContractSeries, path: str | Path) -> None:
    """Persist an OptionContractSeries to a pickle file (use a `.pkl` extension)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(series, f, protocol=pickle.HIGHEST_PROTOCOL)


# --------------------------------------------------------------------------- short-put strategy


@dataclass(frozen=True)
class OptionPositionSeries:
    """Per-trade short-put position log."""

    dates: NDArray[np.int64]
    pnl: NDArray[np.float64]
    trades: tuple[dict, ...]
    meta: dict = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        """Number of open-and-closed positions in the log."""
        return len(self.trades)

    @property
    def avg_dte(self) -> float:
        """Mean DTE-at-entry across all logged trades; NaN when log is empty.

        Returning NaN (not 0.0) so callers can distinguish "no trades" from
        "trades with same-day entry/exit". 0 is a valid DTE; the prior 0.0
        sentinel was ambiguous (P3-2 in the W9 audit).
        """
        if not self.trades:
            return float("nan")
        valid = [t["dte_at_entry"] for t in self.trades if t.get("dte_at_entry") is not None]
        if not valid:
            return float("nan")
        return float(np.mean(valid))


def save_position_pkl(position: OptionPositionSeries, path: str | Path) -> None:
    """Persist an OptionPositionSeries to a pickle file (use a `.pkl` extension)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(position, f, protocol=pickle.HIGHEST_PROTOCOL)


def short_put_series(
    *,
    chain: OptionChainHistory,
    spot: PriceSeries,
    dte_min: int,
    dte_max: int,
    delta_target: float,
    exit_dte: int,
    contracts_per_trade: int = 1,
    r: float = 0.0,
    sigma_fallback: float = 0.20,
) -> OptionPositionSeries:
    """Build a short-put position log: pick OTM strike at delta_target, hold to exit_dte."""
    if not chain.snapshots:
        return OptionPositionSeries(
            dates=np.zeros(0, dtype=np.int64),
            pnl=np.zeros(0, dtype=np.float64),
            trades=(),
            meta={"reason": "empty chain"},
        )
    spot_by_date: dict[int, float] = {int(d): float(p) for d, p in zip(spot.dates, spot.close)}

    trades: list[dict] = []
    daily_pnl_by_date: dict[int, float] = {}
    open_pos: dict | None = None
    for snap in chain.snapshots:
        d = int(snap.asof_date)
        s = spot_by_date.get(d)
        if s is None:
            continue
        if open_pos is None:
            # search for new entry within DTE window
            candidates = [c for c in snap.contracts if c.option_type == "P"]
            in_window = [c for c in candidates
                         if dte_min <= (_to_dt(int(c.expiration)) - _to_dt(d)).days <= dte_max]
            if not in_window:
                continue
            asof_chain = OptionChainSnapshot(root=snap.root, asof_date=d, spot=s, contracts=tuple(in_window))
            sel = select_delta_target(asof_chain, s, float(delta_target),
                                      option_type="P", r=r, sigma_fallback=sigma_fallback)
            if sel is None or not sel.rows:
                continue
            row0 = sel.rows[0]
            entry_premium = float(getattr(row0, "mark", row0.close))
            open_pos = {
                "entry_date": d,
                "expiration": int(sel.expiration),
                "strike": float(sel.strike),
                "contract_id": sel.contract_id,
                "entry_premium": entry_premium,
                "qty": int(contracts_per_trade),
                "dte_at_entry": (_to_dt(int(sel.expiration)) - _to_dt(d)).days,
            }
            continue
        # holding: mark-to-model; exit when dte <= exit_dte
        dte_remaining = (_to_dt(open_pos["expiration"]) - _to_dt(d)).days
        if dte_remaining > exit_dte:
            continue
        # Exit at intrinsic value (cash-settled approximation).
        intrinsic = max(open_pos["strike"] - s, 0.0)
        # short put PnL per contract = (entry_premium - exit_premium) * 100
        pnl = (open_pos["entry_premium"] - intrinsic) * 100.0 * float(open_pos["qty"])
        daily_pnl_by_date[d] = daily_pnl_by_date.get(d, 0.0) + pnl
        trades.append({
            "entry_date": int(open_pos["entry_date"]),
            "exit_date": d,
            "expiration": int(open_pos["expiration"]),
            "strike": float(open_pos["strike"]),
            "contract_id": open_pos["contract_id"],
            "entry_premium": float(open_pos["entry_premium"]),
            "exit_premium": float(intrinsic),
            "qty": int(open_pos["qty"]),
            "pnl": float(pnl),
            "dte_at_entry": int(open_pos["dte_at_entry"]),
        })
        open_pos = None

    dates_arr = np.array(sorted(daily_pnl_by_date.keys()), dtype=np.int64)
    pnl_arr = np.array([daily_pnl_by_date[int(d)] for d in dates_arr], dtype=np.float64)
    return OptionPositionSeries(
        dates=dates_arr,
        pnl=pnl_arr,
        trades=tuple(trades),
        meta={
            "dte_min": int(dte_min),
            "dte_max": int(dte_max),
            "delta_target": float(delta_target),
            "exit_dte": int(exit_dte),
            "contracts_per_trade": int(contracts_per_trade),
        },
    )


