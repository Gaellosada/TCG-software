"""Signed-off multiplier config + live-first resolution (tcg.types.multipliers)."""

from __future__ import annotations

import math

from tcg.types.multipliers import (
    FUTURES_NOTIONAL_MULTIPLIERS,
    futures_collection_for_option,
    resolve_multipliers,
    root_from_collection,
)


def test_root_and_futures_name_mapping() -> None:
    assert root_from_collection("OPT_SP_500") == "SP_500"
    assert root_from_collection("FUT_VIX") == "VIX"
    assert root_from_collection("SP_500") == "SP_500"
    # Guardrail Sign 3: OPT_ -> FUT_ by NAME.
    assert futures_collection_for_option("OPT_VIX") == "FUT_VIX"
    assert futures_collection_for_option("OPT_SP_500") == "FUT_SP_500"


def test_signed_off_values_present() -> None:
    sp = FUTURES_NOTIONAL_MULTIPLIERS["SP_500"]
    assert (sp.m_fut, sp.m_opt, sp.verified) == (50.0, 50.0, True)
    vix = FUTURES_NOTIONAL_MULTIPLIERS["VIX"]
    # The ONLY differing root: fut 1000, opt 100.
    assert (vix.m_fut, vix.m_opt) == (1000.0, 100.0)
    assert not FUTURES_NOTIONAL_MULTIPLIERS["BTC"].verified  # provisional


def test_config_fallback_when_no_live() -> None:
    r = resolve_multipliers("VIX")
    assert r.m_fut == 1000.0 and r.m_opt == 100.0
    assert r.m_fut_source == "config" and r.m_opt_source == "config"
    assert r.is_complete


def test_live_wins_over_config() -> None:
    # Cash-index SPX option (contract_size 100) would be caught live — live wins.
    r = resolve_multipliers("SP_500", live_m_fut=50.0, live_m_opt=100.0)
    assert r.m_opt == 100.0 and r.m_opt_source == "live"
    # Disagreement is surfaced, not silently overridden.
    assert r.diagnostic is not None and "disagrees" in r.diagnostic


def test_missing_root_yields_nan_and_diagnostic_never_one() -> None:
    r = resolve_multipliers("UNKNOWN_ROOT")
    assert math.isnan(r.m_fut) and math.isnan(r.m_opt)
    assert r.m_fut_source == "missing" and not r.is_complete
    assert r.diagnostic is not None
    # NEVER a silent 1.0.
    assert r.m_fut != 1.0 and r.m_opt != 1.0


def test_provisional_root_flagged() -> None:
    r = resolve_multipliers("BTC")
    assert not r.verified
    assert r.diagnostic is not None and "PROVISIONAL" in r.diagnostic


def test_live_m_fut_overrides_config() -> None:
    # A live FUT contract_size that disagrees with config → live wins + diagnostic.
    r = resolve_multipliers("SP_500", live_m_fut=25.0)
    assert r.m_fut == 25.0 and r.m_fut_source == "live"
    assert (
        r.diagnostic is not None
        and "m_fut" in r.diagnostic
        and "disagrees" in r.diagnostic
    )
    # A live value equal to config → live source, NO disagreement noise.
    r2 = resolve_multipliers("VIX", live_m_fut=1000.0)
    assert r2.m_fut == 1000.0 and r2.m_fut_source == "live"
    assert r2.diagnostic is None


def test_m_fut_null_live_falls_back_to_config() -> None:
    r = resolve_multipliers("VIX", live_m_fut=None)
    assert r.m_fut == 1000.0 and r.m_fut_source == "config"


def test_m_fut_neither_live_nor_config_is_nan() -> None:
    r = resolve_multipliers("UNKNOWN", live_m_fut=None)
    assert math.isnan(r.m_fut) and r.m_fut_source == "missing"
    assert r.m_fut != 1.0  # never a silent 1.0
