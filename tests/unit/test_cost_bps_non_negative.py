"""FIX round 4: the API boundary rejects a NEGATIVE slippage/fees bps.

``slippage_bps`` / ``fees_bps`` on both ``SignalComputeRequest`` and
``PortfolioRequest`` were bare ``float = 0.0`` with no lower bound, so a negative
value posted directly yielded NEGATIVE drag (inflated equity / negative reported
cost). A ``Field(ge=0.0)`` constraint makes Pydantic reject it -> FastAPI returns
422. These tests assert the model-level validation that drives that 422.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tcg.core.api.portfolio import PortfolioRequest
from tcg.core.api.signals import SignalComputeRequest, SignalIn


@pytest.mark.parametrize("field", ["slippage_bps", "fees_bps"])
def test_signal_compute_request_rejects_negative_bps(field):
    with pytest.raises(ValidationError) as exc:
        SignalComputeRequest(spec=SignalIn(), **{field: -10.0})
    assert field in str(exc.value)


@pytest.mark.parametrize("field", ["slippage_bps", "fees_bps"])
def test_portfolio_request_rejects_negative_bps(field):
    with pytest.raises(ValidationError) as exc:
        PortfolioRequest(legs={}, weights={}, **{field: -10.0})
    assert field in str(exc.value)


def test_zero_and_positive_bps_still_accepted():
    """The default (0.0) and any positive rate remain valid (feature unchanged)."""
    SignalComputeRequest(spec=SignalIn())  # defaults 0.0
    SignalComputeRequest(spec=SignalIn(), slippage_bps=10.0, fees_bps=5.0)
    PortfolioRequest(legs={}, weights={})
    PortfolioRequest(legs={}, weights={}, slippage_bps=0.0, fees_bps=2.5)
