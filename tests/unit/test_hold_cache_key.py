"""Guardrail Sign 4: the hold-mode cache key discriminates sizing_mode /
futures_reference so a premium_notional cached result is NEVER served for a
futures_notional leg with otherwise-identical axes (which would silently
mis-size — the futures leg attaches a roll_future_ref the premium one lacks)."""

from __future__ import annotations

from tcg.core.api._series_fetch import _hold_cache_key
from tcg.types.options import ByDelta, NearestToTarget
from tcg.types.signal import InstrumentOptionStream


def _opt(*, sizing_mode: str, futures_reference: str = "nearest_on_or_after"):
    return InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=-0.10, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
        nav_times=1.0,
        sizing_mode=sizing_mode,
        futures_reference=futures_reference,
    )


def test_premium_and_futures_keys_differ() -> None:
    k_prem = _hold_cache_key(_opt(sizing_mode="premium_notional"))
    k_fut = _hold_cache_key(_opt(sizing_mode="futures_notional"))
    assert k_prem != k_fut, "premium vs futures cache keys MUST NOT collide"


def test_futures_reference_variants_differ() -> None:
    k_a = _hold_cache_key(
        _opt(sizing_mode="futures_notional", futures_reference="nearest_on_or_after")
    )
    k_b = _hold_cache_key(
        _opt(sizing_mode="futures_notional", futures_reference="nearest_abs")
    )
    assert k_a != k_b


def test_nav_times_excluded_identical_key() -> None:
    # nav_times is a downstream sizing multiple (same series) → NOT in the key.
    a = _opt(sizing_mode="premium_notional")
    b = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=-0.10, tolerance=0.20),
        stream="mid",
        hold_between_rolls=True,
        nav_times=7.5,  # different
        sizing_mode="premium_notional",
    )
    assert _hold_cache_key(a) == _hold_cache_key(b)
