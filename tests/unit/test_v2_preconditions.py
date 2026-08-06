"""Pure-checker unit tests for the v2 API-boundary preconditions.

The boundary checker (:func:`check_v2_preconditions`) must accept EXACTLY the
option cycles the reader can serve — no more, no less. A boundary that is
stricter than the reader rejects a serviceable run (the "generic weekly ``W``"
regression); one that is looser lets an unserviceable run reach the engine,
where the error is swallowed into an all-NaN curve.

These tests encode that contract directly on the pure checker (no DB), keyed to
:func:`tcg.data._v2_compat.options_reader._route_objects`, the ground truth for
what v2 serves.
"""

from __future__ import annotations

import pytest

from tcg.core.api._v2_preconditions import check_v2_preconditions
from tcg.data._v2_compat.options_reader import _route_objects
from tcg.types.errors import ValidationError
from tcg.types.options import WEEKLY_CYCLE_TAGS


def _option_payload(cycle):
    return {
        "legs": {
            "P mid": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "cycle": cycle,
                "stream": "close",
            }
        }
    }


@pytest.mark.parametrize("cycle", WEEKLY_CYCLE_TAGS)
def test_every_weekly_cycle_the_reader_serves_passes_the_boundary(cycle):
    """The generic ``"W"`` umbrella AND each concrete ``"W# Friday"`` are
    serviceable on v2 — the reader routes them to real EW objects — so the
    boundary must let them through unchanged."""
    # Ground truth: the reader resolves this cycle to at least one EW object.
    assert _route_objects(cycle, require_filter=True)
    # Therefore the boundary must NOT reject it.
    check_v2_preconditions(_option_payload(cycle), data_source="v2")


def test_generic_weekly_W_is_not_rejected():
    """Regression: ``"W"`` (the UI's "Weekly" choice) was rejected as if it were
    the absent monthly series, even though the reader serves it as all four
    weeklies."""
    check_v2_preconditions(_option_payload("W"), data_source="v2")


@pytest.mark.parametrize("cycle", ["M", "Q", ""])
def test_non_weekly_cycles_are_rejected(cycle):
    """v2 has no monthly/quarterly S&P options — these must still 400."""
    with pytest.raises(ValidationError):
        check_v2_preconditions(_option_payload(cycle), data_source="v2")


def test_missing_cycle_is_rejected():
    with pytest.raises(ValidationError):
        check_v2_preconditions(_option_payload(None), data_source="v2")


def test_v1_run_is_a_total_noop():
    """The frozen v1 reference path must gain no boundary branch (Sign 1)."""
    check_v2_preconditions(_option_payload("M"), data_source="v1")
    check_v2_preconditions(_option_payload(None), data_source="v1")


# --------------------------------------------------------------------------- #
# Per-instrument: the gate is PER NODE on its EFFECTIVE source, not per run.    #
# --------------------------------------------------------------------------- #

from tcg.core.api._v2_preconditions import collect_v2_option_roots  # noqa: E402


def _mixed_payload():
    """A v1-DEFAULT body with one v1 option leg (v2-hostile shape, must be
    ignored) and one explicitly-v2 option leg with a v2-unserviceable cycle."""
    return {
        "data_source": "v1",
        "legs": {
            "v1leg": {
                "type": "option_stream",
                "collection": "OPT_VIX",
                "cycle": "M",
                "stream": "mid",
                # no data_source → inherits the v1 default → NOT gated
            },
            "v2leg": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "cycle": "M",  # monthly is unserviceable on v2
                "stream": "close",
                "data_source": "v2",
            },
        },
    }


def test_mixed_payload_gates_only_the_v2_node():
    """The v1 leg's v2-hostile shape must be ignored; only the v2 leg raises,
    and the message names THAT leg."""
    with pytest.raises(ValidationError) as exc:
        check_v2_preconditions(_mixed_payload(), data_source="v1")
    msg = str(exc.value)
    assert "v2leg" in msg
    assert "monthly" in msg


def test_all_v1_payload_never_gated_even_with_v2_hostile_leg():
    """Sign 1: an all-v1 body no-ops entirely, even when a leg's shape would be
    unserviceable IF it were v2."""
    payload = {
        "data_source": "v1",
        "legs": {
            "a": {"type": "option_stream", "collection": "OPT_VIX", "cycle": "M", "stream": "mid"}
        },
    }
    check_v2_preconditions(payload, data_source="v1")  # must not raise


def test_leg_inherits_v2_body_default_and_is_gated():
    """A leg with no own source under a v2 body inherits v2 and IS checked."""
    payload = {
        "data_source": "v2",
        "legs": {
            "a": {"type": "option_stream", "collection": "OPT_SP_500", "cycle": "M", "stream": "close"}
        },
    }
    with pytest.raises(ValidationError):
        check_v2_preconditions(payload, data_source="v2")


def test_v1_leaf_overrides_v2_body_default_and_is_not_gated():
    """A leg that explicitly sets ``data_source='v1'`` under a v2 body is NOT
    checked (its own source wins over the inherited default)."""
    payload = {
        "data_source": "v2",
        "legs": {
            "a": {
                "type": "option_stream",
                "collection": "OPT_VIX",
                "cycle": "M",
                "stream": "mid",
                "data_source": "v1",
            }
        },
    }
    check_v2_preconditions(payload, data_source="v2")  # must not raise


def test_collect_v2_option_roots_returns_only_v2_effective_roots():
    """Only option roots whose EFFECTIVE source is v2 are collected — the v1 leg
    (inherited) is excluded so its coverage floor is never queried."""
    payload = {
        "data_source": "v1",
        "legs": {
            "v2opt": {
                "type": "option_stream",
                "collection": "OPT_VIX",
                "cycle": "W3 Friday",
                "stream": "close",
                "data_source": "v2",
            },
            "v1opt": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "cycle": "W3 Friday",
                "stream": "close",
            },
        },
    }
    assert collect_v2_option_roots(payload, data_source="v1") == {"OPT_VIX"}


def test_composed_child_body_re_roots_inheritance():
    """A composed child body carries its own ``data_source`` in the dump, which
    re-roots inheritance for its legs: a v2 child under a v1 parent gates the
    child's legs even though they carry no own source."""
    payload = {
        "data_source": "v1",
        "legs": {
            "c": {
                "type": "portfolio",
                "portfolio": {
                    "data_source": "v2",
                    "legs": {
                        "inner": {
                            "type": "option_stream",
                            "collection": "OPT_SP_500",
                            "cycle": "M",
                            "stream": "close",
                        }
                    },
                },
            }
        },
    }
    with pytest.raises(ValidationError):
        check_v2_preconditions(payload, data_source="v1")


# --- A1: basket-nested v2 option legs (invisible to the wire-JSON walk) --------


def _v2_basket_input(*, cycle, stream):
    """A resolved basket input carrying ONE v2 option leg (data_source inherited).

    Mirrors what ``_resolve_basket_inputs`` produces for a SAVED basket — the leg
    is a materialised ``InstrumentOptionStream``, NOT anything on the request wire.
    """
    from tcg.core.api.signals import _ResolvedBasketInput
    from tcg.types.options import ByMoneyness, NextThirdFriday
    from tcg.types.signal import InstrumentOptionStream

    leg = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=cycle,
        maturity=NextThirdFriday(offset_months=1),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.01),
        stream=stream,
        data_source=None,  # inherits the run default
    )
    return _ResolvedBasketInput(id="opt_in", legs=((leg, 1.0),), basket_id="b1")


def test_wire_precondition_cannot_see_basket_legs_documents_the_gap():
    """The wire-JSON checker walks only the request body; a saved basket is just
    an id, so its option legs are invisible — this is the A1 gap the new helper
    closes. A bad stream/cycle on a basket leg passes the wire walk here.
    """
    payload = {"spec": {"inputs": [{"instrument": {"type": "basket", "basket_id": "b1"}}]}}
    # No option_stream node on the wire → nothing to reject, even though the
    # resolved basket below carries an unserviceable v2 'mid' leg.
    check_v2_preconditions(payload, data_source="v2")  # does not raise (the gap)


def test_basket_nested_v2_mid_stream_is_rejected():
    """A v2 option leg reached through a basket with the default ``stream="mid"``
    (v2 cannot serve) must 400 at the boundary, not degrade to an all-NaN curve.
    """
    from tcg.core.api.signals import check_v2_basket_option_legs

    inputs = [_v2_basket_input(cycle="W", stream="mid")]
    with pytest.raises(ValidationError) as ei:
        check_v2_basket_option_legs(inputs, default_source="v2")
    assert "opt_in" in str(ei.value)  # labelled with the basket input id


def test_basket_nested_v2_bad_cycle_is_rejected():
    from tcg.core.api.signals import check_v2_basket_option_legs

    inputs = [_v2_basket_input(cycle=None, stream="close")]
    with pytest.raises(ValidationError):
        check_v2_basket_option_legs(inputs, default_source="v2")


def test_basket_nested_v1_leg_no_ops():
    """An all-v1 run (or v1 default) must not touch the v2 gate (Sign 1)."""
    from tcg.core.api.signals import check_v2_basket_option_legs

    inputs = [_v2_basket_input(cycle=None, stream="mid")]
    # default_source v1 and the leg inherits it → effective v1 → no raise.
    check_v2_basket_option_legs(inputs, default_source="v1")


def test_basket_nested_serviceable_v2_leg_passes():
    from tcg.core.api.signals import check_v2_basket_option_legs

    inputs = [_v2_basket_input(cycle="W", stream="close")]
    check_v2_basket_option_legs(inputs, default_source="v2")  # serviceable → OK
