"""GAP A + GAP C wire→engine mapping (PR#92 §5.5/§5.6 hedged-VIX-call reproduction).

DB-free unit coverage that the signals/basket/portfolio wire model
:class:`OptionStreamRef` now carries BOTH:

  * GAP A — a ``delta_hedge`` overlay (mirroring the portfolio ``LegSpec``), so the
    F2 hedge is no longer DROPPED between the wire and the engine
    :class:`tcg.types.signal.InstrumentOptionStream`;
  * GAP C — an ``apply_contract_multiplier`` flag that selects per-INDEX-POINT
    sizing (m_opt := m_fut) for a futures_notional option leg, matching legacy VIX
    option sizing (SPEC §6 multiplier note).

The mapping goes through the ONE shared converter ``option_stream_ref_to_instrument``
so the signals path, basket-leg path and portfolio hold path can't drift.
"""

from __future__ import annotations

import pytest

from tcg.core.api._models import DeltaHedgeConfig, OptionStreamRef
from tcg.core.api.options import option_stream_ref_to_instrument
from tcg.types.multipliers import collapse_index_point
from tcg.types.signal import DeltaHedgeSpec, InstrumentOptionStream


def _ref(**kw) -> OptionStreamRef:
    base = dict(
        type="option_stream",
        collection="OPT_VIX",
        option_type="C",
        maturity={"kind": "next_third_friday", "offset_months": 0},
        selection={"kind": "by_delta", "target_delta": 0.30, "tolerance": 0.20},
        stream="close",
        hold_between_rolls=True,
    )
    base.update(kw)
    return OptionStreamRef(**base)


# ── GAP A: delta_hedge reaches the engine instrument (no longer dropped) ─────
def test_delta_hedge_maps_into_instrument() -> None:
    ref = _ref(
        delta_hedge=DeltaHedgeConfig(
            factor=1.0 / 3.0,
            hedge_collection="FUT_VIX",
            gate_symbol="IND_VVIX",
            gate_threshold=150.0,
            gate_op="gt",
        )
    )
    inst = option_stream_ref_to_instrument(ref)
    assert isinstance(inst, InstrumentOptionStream)
    assert isinstance(inst.delta_hedge, DeltaHedgeSpec)
    assert inst.delta_hedge.factor == pytest.approx(1.0 / 3.0)
    assert inst.delta_hedge.hedge_collection == "FUT_VIX"
    assert inst.delta_hedge.gate_symbol == "IND_VVIX"
    assert inst.delta_hedge.gate_threshold == pytest.approx(150.0)
    assert inst.delta_hedge.gate_op == "gt"


# ── P2a: rebalance / qty-cap / pause-on-roll config → spec threading ─────────
def test_hedge_p2a_defaults_byte_identical() -> None:
    # Absent P2a keys (old JSONB) load with the byte-identical shipped defaults.
    spec = DeltaHedgeConfig().to_spec()
    assert spec.rebalance_interval_days == 1
    assert spec.qty_cap_mult == pytest.approx(10.0)
    assert spec.pause_on_roll is True


def test_hedge_p2a_fields_thread_to_spec() -> None:
    spec = DeltaHedgeConfig(
        rebalance_interval_days=5, qty_cap_mult=3.5, pause_on_roll=False
    ).to_spec()
    assert spec.rebalance_interval_days == 5
    assert spec.qty_cap_mult == pytest.approx(3.5)
    assert spec.pause_on_roll is False


def test_hedge_p2a_reaches_instrument() -> None:
    inst = option_stream_ref_to_instrument(
        _ref(delta_hedge=DeltaHedgeConfig(rebalance_interval_days=3, pause_on_roll=False))
    )
    assert inst.delta_hedge is not None
    assert inst.delta_hedge.rebalance_interval_days == 3
    assert inst.delta_hedge.pause_on_roll is False


def test_hedge_p2a_back_compat_old_jsonb_loads() -> None:
    # A persisted payload WITHOUT the P2a keys still validates → shipped defaults.
    cfg = DeltaHedgeConfig.model_validate(
        {"factor": 1.0 / 3.0, "hedge_collection": "FUT_VIX", "gate_threshold": 150.0}
    )
    assert cfg.rebalance_interval_days == 1
    assert cfg.qty_cap_mult == pytest.approx(10.0)
    assert cfg.pause_on_roll is True


@pytest.mark.parametrize("bad,expect", [(0, 1), (-4, 1), (1, 1), (7, 7)])
def test_hedge_rebalance_interval_clamped_to_one(bad, expect) -> None:
    # rebalance_interval_days ≤ 1 is treated as 1 (never rejected).
    assert DeltaHedgeConfig(rebalance_interval_days=bad).rebalance_interval_days == expect


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_hedge_qty_cap_mult_rejects_nonpositive_or_nonfinite(bad) -> None:
    with pytest.raises(Exception):  # pydantic ValidationError wraps the ValueError
        DeltaHedgeConfig(qty_cap_mult=bad)


def test_delta_hedge_disabled_maps_to_none() -> None:
    ref = _ref(delta_hedge=DeltaHedgeConfig(enabled=False))
    inst = option_stream_ref_to_instrument(ref)
    assert inst.delta_hedge is None


def test_no_delta_hedge_maps_to_none_and_is_omitted() -> None:
    ref = _ref()
    inst = option_stream_ref_to_instrument(ref)
    assert inst.delta_hedge is None
    # byte-identity: an unset overlay is OMITTED from the JSON dump so an ordinary
    # option leg's payload / result-cache key is unperturbed.
    dumped = ref.model_dump(mode="json")
    assert "delta_hedge" not in dumped
    assert "apply_contract_multiplier" not in dumped


# ── GAP C: apply_contract_multiplier flag maps + is omitted when default ─────
def test_apply_contract_multiplier_default_true() -> None:
    inst = option_stream_ref_to_instrument(_ref())
    assert inst.apply_contract_multiplier is True


def test_apply_contract_multiplier_false_maps() -> None:
    inst = option_stream_ref_to_instrument(_ref(apply_contract_multiplier=False))
    assert inst.apply_contract_multiplier is False


def test_apply_contract_multiplier_false_is_serialised() -> None:
    # A non-default (per-index-point) leg DOES serialise the flag so its cache key
    # changes (it is a different compute), while a default leg omits it.
    dumped = _ref(apply_contract_multiplier=False).model_dump(mode="json")
    assert dumped["apply_contract_multiplier"] is False


# ── GAP C: the per-index-point multiplier collapse ──────────────────────────
def test_collapse_index_point_vix() -> None:
    # VIX: m_fut=1000, m_opt=100. apply=True → unchanged; apply=False → m_opt:=m_fut
    # so the m_opt/m_fut ratio becomes exactly 1.0 (per-index-point, legacy).
    assert collapse_index_point(1000.0, 100.0, True) == (1000.0, 100.0)
    mf, mo = collapse_index_point(1000.0, 100.0, False)
    assert mf == 1000.0
    assert mo == 1000.0
    # the daily-$ ratio m_opt/m_fut goes 0.1 → 1.0 (uniform 10x higher magnitude).
    assert (mo / mf) / (100.0 / 1000.0) == pytest.approx(10.0)


def test_collapse_index_point_spx_unchanged_either_way() -> None:
    # SP_500: m_fut == m_opt == 50 → the ratio is already 1.0; the flag is a no-op.
    assert collapse_index_point(50.0, 50.0, True) == (50.0, 50.0)
    assert collapse_index_point(50.0, 50.0, False) == (50.0, 50.0)


def test_collapse_index_point_none_means_apply() -> None:
    # None (unset wire default) is treated as "apply the multiplier" (byte-identical).
    assert collapse_index_point(1000.0, 100.0, None) == (1000.0, 100.0)
