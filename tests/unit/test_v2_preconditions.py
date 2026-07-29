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
