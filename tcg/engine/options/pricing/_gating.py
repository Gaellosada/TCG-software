"""Internal helpers for Module 2 (pricing).

Pure functions; no DTO awareness beyond ``date`` arithmetic and primitives.
Kept separate from ``pricer.py`` so the failure-routing logic is unit-testable
without spinning up a kernel.
"""

from __future__ import annotations

from datetime import date
from typing import Literal


# Roots whose Greeks are structurally blocked in Phase 1 (guardrail #6).
_BLOCKED_ROOTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "OPT_VIX": ("missing_forward_vix_curve", ("forward_vix_curve",)),
    "OPT_ETH": ("missing_deribit_feed", ("underlying_price",)),
}

# Roots whose strike-factor must be verified before Black-76 may run.
# Mirrors `tcg.data.options._strike_factor.STRIKE_FACTOR_VERIFIED` but lives
# inside Module 2 so the engine never imports from `tcg.data.*` (guardrail #2,
# import-linter `engine-data-isolation`). The flag is also carried per-contract
# in `OptionContractDoc.strike_factor_verified`; this set just enumerates which
# roots the gate applies to.
_STRIKE_FACTOR_GATED_ROOTS: frozenset[str] = frozenset(
    {"OPT_T_NOTE_10_Y", "OPT_T_BOND", "OPT_EURUSD", "OPT_JPYUSD"}
)


def is_blocked_root(collection: str) -> tuple[bool, str | None, tuple[str, ...]]:
    """Return ``(blocked, error_code, missing_inputs)`` for the root collection.

    Examples:
        >>> is_blocked_root("OPT_VIX")
        (True, 'missing_forward_vix_curve', ('forward_vix_curve',))
        >>> is_blocked_root("OPT_SP_500")
        (False, None, ())
    """
    if collection in _BLOCKED_ROOTS:
        code, missing = _BLOCKED_ROOTS[collection]
        return True, code, missing
    return False, None, ()


def needs_strike_factor_verification(collection: str) -> bool:
    """Whether the given root requires `strike_factor_verified=True` to compute."""
    return collection in _STRIKE_FACTOR_GATED_ROOTS


def time_to_expiry_years(expiration: date, ref: date) -> float:
    """TTM in years using the documented `(expiration - ref).days / 365.0` convention.

    Phase 1 uses calendar-day TTM (not business days). py_vollib expects years.
    Returns 0.0 or negative when expired — caller surfaces ``expired_contract``.
    """
    return (expiration - ref).days / 365.0


def sign_for_type(option_type: str) -> Literal["c", "p"]:
    """Map ``OptionContractDoc.type`` ('C'|'P') to the py_vollib flag ('c'|'p')."""
    if option_type == "C":
        return "c"
    if option_type == "P":
        return "p"
    raise ValueError(f"Unknown option type {option_type!r}; expected 'C' or 'P'.")
