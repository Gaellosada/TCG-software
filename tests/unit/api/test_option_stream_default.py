"""ASK B: ``OptionStreamRef.stream`` defaults to ``"mid"`` when omitted.

The roll/stream UX should not force the user to choose which series to
extract; ``mid`` (the option premium mark) is the sensible default.  The
request model must therefore accept a payload with NO ``stream`` key and
resolve it to ``"mid"`` — while still honouring an explicit override and
rejecting an out-of-enum value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tcg.core.api._models import OptionStreamRef


def _base_payload(**overrides):
    payload = {
        "type": "option_stream",
        "collection": "OPT_SP_500",
        "option_type": "C",
        "cycle": "M",
        "maturity": {"kind": "nearest_to_target", "target_days": 30},
        "selection": {"kind": "by_delta", "target": 0.10, "tolerance": 0.05},
    }
    payload.update(overrides)
    return payload


def test_stream_defaults_to_mid_when_omitted() -> None:
    ref = OptionStreamRef.model_validate(_base_payload())
    assert ref.stream == "mid"


def test_explicit_stream_is_preserved() -> None:
    ref = OptionStreamRef.model_validate(_base_payload(stream="delta"))
    assert ref.stream == "delta"


def test_out_of_enum_stream_still_rejected() -> None:
    with pytest.raises(ValidationError):
        OptionStreamRef.model_validate(_base_payload(stream="not_a_stream"))


def test_close_is_an_accepted_stream() -> None:
    ref = OptionStreamRef.model_validate(_base_payload(stream="close"))
    assert ref.stream == "close"


# ── close is the DEFAULT for HOLD legs (conditional; explicit + persisted win) ──


def test_hold_leg_defaults_to_close_when_stream_omitted() -> None:
    """A NEW hold leg with no explicit stream defaults to ``close`` (the faithful
    settlement mark for a held-to-roll option), NOT ``mid``."""
    ref = OptionStreamRef.model_validate(_base_payload(hold_between_rolls=True))
    assert ref.stream == "close"


def test_non_hold_leg_still_defaults_to_mid() -> None:
    """A NON-hold / daily-reselected leg with no explicit stream keeps defaulting
    to ``mid`` (unchanged display-series behaviour)."""
    ref = OptionStreamRef.model_validate(_base_payload(hold_between_rolls=False))
    assert ref.stream == "mid"
    # Omitting the flag entirely is also non-hold (default False) → mid.
    assert OptionStreamRef.model_validate(_base_payload()).stream == "mid"


def test_explicit_stream_on_hold_leg_is_honored_not_coerced() -> None:
    """A hold leg that explicitly sets its stream is honoured verbatim — an
    explicit ``mid`` is NOT flipped to ``close``, and neither is ``bs_mid``."""
    ref_mid = OptionStreamRef.model_validate(
        _base_payload(hold_between_rolls=True, stream="mid")
    )
    assert ref_mid.stream == "mid"
    ref_bs = OptionStreamRef.model_validate(
        _base_payload(hold_between_rolls=True, stream="bs_mid")
    )
    assert ref_bs.stream == "bs_mid"


def test_persisted_hold_leg_carrying_mid_stays_mid_after_load() -> None:
    """Backward-compat: an already-saved hold leg persisted its stream EXPLICITLY
    (persistence always serialises ``stream``), so re-validating that payload
    keeps ``mid`` — saved research is reproducible, never silently flipped to
    ``close``."""
    persisted = _base_payload(hold_between_rolls=True, stream="mid", nav_times=1.0)
    ref = OptionStreamRef.model_validate(persisted)
    assert ref.stream == "mid"
