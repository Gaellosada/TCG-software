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
