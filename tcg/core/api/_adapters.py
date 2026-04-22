"""Input-adapter helpers for API routers.

Small, stateless builders that translate frontend string params into
typed domain objects. Centralized so that the four routers that
construct a ``ContinuousRollConfig`` can't diverge on default values or
validation order.
"""

from __future__ import annotations

from tcg.core.api.common import ADJUSTMENT_MAP
from tcg.types.market import ContinuousRollConfig, RollStrategy


def build_roll_config(
    adjustment: str,
    cycle: str | None,
    roll_offset: int,
) -> ContinuousRollConfig:
    """Build a front-month ``ContinuousRollConfig`` from frontend params.

    Raises ``ValueError`` with a canonical ``"unknown adjustment
    method ..."`` message so each caller can forward it through its
    preferred error channel.
    """
    adj = ADJUSTMENT_MAP.get(adjustment)
    if adj is None:
        raise ValueError(f"unknown adjustment method {adjustment!r}")
    return ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH,
        adjustment=adj,
        cycle=cycle or None,
        roll_offset_days=int(roll_offset),
    )
