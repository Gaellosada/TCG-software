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
    strategy: str = "front_month",
) -> ContinuousRollConfig:
    """Build a ``ContinuousRollConfig`` from frontend params.

    ``strategy`` selects the roll strategy (``"front_month"`` /
    ``"end_of_month"``); it defaults to ``"front_month"`` so existing positional
    callers are unchanged.  Issue #3: this MUST be threaded (not hardcoded) or
    END_OF_MONTH silently no-ops for the signals + indicators paths that route
    through this adapter.

    Raises ``ValueError`` with a canonical ``"unknown adjustment method ..."``
    or ``"invalid roll strategy ..."`` message so each caller can forward it
    through its preferred error channel.
    """
    adj = ADJUSTMENT_MAP.get(adjustment)
    if adj is None:
        raise ValueError(f"unknown adjustment method {adjustment!r}")
    try:
        roll_strategy = RollStrategy(strategy)
    except ValueError:
        raise ValueError(
            f"invalid roll strategy {strategy!r}. Must be one of: "
            f"{', '.join(e.value for e in RollStrategy)}"
        ) from None
    return ContinuousRollConfig(
        strategy=roll_strategy,
        adjustment=adj,
        cycle=cycle or None,
        roll_offset_days=int(roll_offset),
    )
