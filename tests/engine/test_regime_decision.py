"""Pure engine tests for the F2.2 regime->side DECISION layer.

Deterministic, NO dwh. Covers:
* ``decide_regime_side`` — the pure (signals, thresholds) -> {long,short,flat}
  rule: HVOL-ON strict + tolerance boundary -> long; HVOL-OFF -> short;
  extremely-low precedence -> flat; VVIX gate veto/adjust; missing signals ->
  documented safe default (fall back to the static run-level side).
* ``resolve_regime_decisions`` — the NO-LOOK-AHEAD as-of picker: day D uses the
  latest signal date STRICTLY before D (<= D-1), guarded by an injected divergent
  D-value that must NOT change D's side.
"""

from __future__ import annotations

from tcg.engine.regime import (
    LevelGateSpec,
    decide_regime_side,
    resolve_regime_decisions,
)

RV_KEYS = ("h20", "h30", "h100")


# --------------------------------------------------------------------------- #
# decide_regime_side — core cascade
# --------------------------------------------------------------------------- #
def test_hvol_on_strict_backwardation_is_long() -> None:
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15}
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS)
    assert d.side == "long"
    assert d.state == "hvol_on"
    assert d.gate is None


def test_not_backwardated_is_hvol_off_short() -> None:
    # h20 < h30 -> NOT the strict H20>H30>H100 ladder -> complement -> short.
    sig = {"h20": 0.14, "h30": 0.16, "h100": 0.15}
    d = decide_regime_side(sig, static_side="long", rv_keys=RV_KEYS)
    assert d.side == "short"
    assert d.state == "hvol_off"


def test_hvol_on_strict_boundary_equal_is_not_on() -> None:
    # Exact ties fail the STRICT inequality at tolerance 0 (matches the brief's
    # strict H20>H30>H100 default).
    sig = {"h20": 0.18, "h30": 0.18, "h100": 0.15}
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS)
    assert d.side == "short"
    assert d.state == "hvol_off"


def test_tolerance_relaxes_near_ties_into_hvol_on() -> None:
    # h20 slightly BELOW h30: strict(tol=0) -> off; a relaxing tolerance lets the
    # near-tie still count as backwardation -> on.
    sig = {"h20": 0.176, "h30": 0.18, "h100": 0.15}
    strict = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS)
    assert strict.side == "short"  # 0.176 > 0.18 is False
    relaxed = decide_regime_side(
        sig, static_side="short", rv_keys=RV_KEYS, hvol_tolerance=0.05
    )
    # 0.176 > 0.18*0.95=0.171 AND 0.18 > 0.15*0.95=0.1425 -> ON.
    assert relaxed.side == "long"
    assert relaxed.state == "hvol_on"


def test_extremely_low_floor_takes_precedence_over_short() -> None:
    # A calm, non-backwardated tape would be SHORT, but H20 below the floor -> flat.
    sig = {"h20": 0.04, "h30": 0.05, "h100": 0.06}
    d = decide_regime_side(
        sig, static_side="long", rv_keys=RV_KEYS, extremely_low_h20=0.05
    )
    assert d.side == "flat"
    assert d.state == "extremely_low"


def test_extremely_low_floor_precedes_even_backwardation() -> None:
    # Even a backwardated ladder is vetoed when the absolute short-window level is
    # below the floor (documented: the floor is an absolute veto).
    sig = {"h20": 0.045, "h30": 0.04, "h100": 0.03}
    d = decide_regime_side(
        sig, static_side="long", rv_keys=RV_KEYS, extremely_low_h20=0.05
    )
    assert d.side == "flat"
    assert d.state == "extremely_low"


def test_extremely_low_floor_zero_is_inert() -> None:
    # floor 0.0 disables the gate (RV is always >= 0).
    sig = {"h20": 0.0001, "h30": 0.05, "h100": 0.06}
    d = decide_regime_side(
        sig, static_side="short", rv_keys=RV_KEYS, extremely_low_h20=0.0
    )
    assert d.side == "short"
    assert d.state == "hvol_off"


def test_missing_signal_falls_back_to_static_side() -> None:
    # Any of the 3 RV inputs None -> cannot classify -> documented safe default:
    # trade the static run-level side, state "fallback" (never a silent skip).
    for missing in ("h20", "h30", "h100"):
        sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15}
        sig[missing] = None
        d_long = decide_regime_side(sig, static_side="long", rv_keys=RV_KEYS)
        d_short = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS)
        assert d_long.side == "long" and d_long.state == "fallback"
        assert d_short.side == "short" and d_short.state == "fallback"


# --------------------------------------------------------------------------- #
# decide_regime_side — VVIX gate (veto / adjust), VIX1D-ready
# --------------------------------------------------------------------------- #
def test_vvix_gate_above_vetoes_to_flat() -> None:
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": 130.0}
    gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="flat")
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS, gates=(gate,))
    # Base decision was long (backwardation); the gate vetoes to flat.
    assert d.side == "flat"
    assert d.gate == "vvix"
    assert d.state == "hvol_on"  # state records the underlying regime, gate the veto


def test_vvix_gate_below_threshold_does_not_fire() -> None:
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": 90.0}
    gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="flat")
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS, gates=(gate,))
    assert d.side == "long"
    assert d.gate is None


def test_vvix_gate_disabled_is_inert() -> None:
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": 200.0}
    gate = LevelGateSpec(enabled=False, signal="vvix", above=120.0, action="flat")
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS, gates=(gate,))
    assert d.side == "long"
    assert d.gate is None


def test_vvix_gate_missing_value_cannot_fire() -> None:
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": None}
    gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="flat")
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS, gates=(gate,))
    assert d.side == "long"  # None value never vetoes
    assert d.gate is None


def test_vvix_gate_can_force_a_side_not_only_flat() -> None:
    # "adjust" (not only veto): a gate action of long/short overrides the base.
    sig = {"h20": 0.14, "h30": 0.16, "h100": 0.15, "vvix": 130.0}  # base short
    gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="long")
    d = decide_regime_side(sig, static_side="short", rv_keys=RV_KEYS, gates=(gate,))
    assert d.side == "long"
    assert d.gate == "vvix"


def test_second_gate_slots_in_with_no_rework_vix1d_ready() -> None:
    # F2.3: adding a VIX1D bucket is a NEW gate in the list — same evaluator,
    # no signature change. Two gates evaluate in order (later overrides earlier).
    sig = {"h20": 0.20, "h30": 0.18, "h100": 0.15, "vvix": 90.0, "vix1d": 25.0}
    vvix_gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="flat")
    vix1d_gate = LevelGateSpec(enabled=True, signal="vix1d", above=20.0, action="short")
    d = decide_regime_side(
        sig, static_side="long", rv_keys=RV_KEYS, gates=(vvix_gate, vix1d_gate)
    )
    # vvix does not fire (90<120); vix1d fires (25>20) -> short.
    assert d.side == "short"
    assert d.gate == "vix1d"


# --------------------------------------------------------------------------- #
# resolve_regime_decisions — NO LOOK-AHEAD (the crux)
# --------------------------------------------------------------------------- #
def _sig(h20: float, h30: float, h100: float, vvix: float | None = None) -> dict:
    return {"h20": h20, "h30": h30, "h100": h100, "vvix": vvix}


def test_decision_uses_prior_close_not_day_d() -> None:
    # Trade day D=20250205. D-1=20250204 signals are backwardated (would be long).
    # We ALSO inject a divergent signal DATED D itself (strongly hvol-off): if the
    # resolver leaked day D's own close into D's decision, the side would flip to
    # short. It must stay LONG (D uses <= D-1 only).
    signals_by_date = {
        20250204: _sig(0.20, 0.18, 0.15),  # D-1 -> long
        20250205: _sig(0.10, 0.16, 0.20),  # D itself -> would be short (look-ahead)
    }
    out = resolve_regime_decisions(
        [20250205], signals_by_date, static_side="short", rv_keys=RV_KEYS,
        signal_names=("h20", "h30", "h100", "vvix"),
    )
    assert out[20250205].side == "long"
    assert out[20250205].asof == 20250204  # decided as-of the PRIOR close
    assert out[20250205].state == "hvol_on"


def test_asof_picks_latest_strictly_before_d() -> None:
    signals_by_date = {
        20250203: _sig(0.10, 0.16, 0.20),  # older -> short
        20250204: _sig(0.20, 0.18, 0.15),  # D-1 -> long (the one that must win)
    }
    out = resolve_regime_decisions(
        [20250205], signals_by_date, static_side="short", rv_keys=RV_KEYS,
        signal_names=("h20", "h30", "h100", "vvix"),
    )
    assert out[20250205].asof == 20250204
    assert out[20250205].side == "long"


def test_no_prior_signal_date_falls_back_to_static() -> None:
    # First trade day with NO signal strictly before it -> null signals ->
    # fallback to static side (never a fabricated regime).
    signals_by_date = {20250205: _sig(0.20, 0.18, 0.15)}
    out = resolve_regime_decisions(
        [20250205], signals_by_date, static_side="short", rv_keys=RV_KEYS,
        signal_names=("h20", "h30", "h100", "vvix"),
    )
    assert out[20250205].asof is None
    assert out[20250205].side == "short"
    assert out[20250205].state == "fallback"
    assert out[20250205].signals == {
        "h20": None, "h30": None, "h100": None, "vvix": None
    }


def test_multi_day_mixed_regimes_each_resolved_independently() -> None:
    signals_by_date = {
        20250203: _sig(0.20, 0.18, 0.15),  # -> long as-of for the 4th
        20250204: _sig(0.12, 0.16, 0.18),  # -> short as-of for the 5th
        20250205: _sig(0.05, 0.05, 0.05),  # low, but only used as-of for the 6th
    }
    out = resolve_regime_decisions(
        [20250204, 20250205, 20250206],
        signals_by_date,
        static_side="long",
        rv_keys=RV_KEYS,
        extremely_low_h20=0.06,
        signal_names=("h20", "h30", "h100", "vvix"),
    )
    assert out[20250204].asof == 20250203 and out[20250204].side == "long"
    assert out[20250205].asof == 20250204 and out[20250205].side == "short"
    # 20250206 uses 20250205 (all 0.05 < floor 0.06) -> flat.
    assert out[20250206].asof == 20250205 and out[20250206].side == "flat"
    assert out[20250206].state == "extremely_low"


def test_resolver_threads_gates_asof() -> None:
    # The gate reads the as-of VVIX, not day D's.
    signals_by_date = {
        20250204: _sig(0.20, 0.18, 0.15, vvix=130.0),  # D-1 vvix high -> veto
        20250205: _sig(0.20, 0.18, 0.15, vvix=90.0),   # D vvix low (must be ignored)
    }
    gate = LevelGateSpec(enabled=True, signal="vvix", above=120.0, action="flat")
    out = resolve_regime_decisions(
        [20250205], signals_by_date, static_side="short", rv_keys=RV_KEYS,
        gates=(gate,), signal_names=("h20", "h30", "h100", "vvix"),
    )
    assert out[20250205].side == "flat"
    assert out[20250205].gate == "vvix"
