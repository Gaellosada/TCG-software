"""Stored-source widening helpers — Module 6 exclusive (spec §3.6 / §4.4).

This module is the **only** place in the system where
``ComputeResult.source="stored"`` is emitted (per Appendix C.3).
Module 1 returns raw values (None or float); Module 2 emits only
``"computed"`` or ``"missing"``; Module 6 — through these helpers —
wraps stored values as ``"stored"`` and merges with Module 2's output.

Invariants
----------
- ``widen_stored(value, greek_name)`` with a non-None value returns a
  ``ComputeResult`` with ``source="stored"``, ``model=None``,
  ``inputs_used=None``, ``missing_inputs=None``, ``error_code=None``,
  ``error_detail=None``.  This shape mirrors the Phase 0 dataclass
  contract exactly so ``ChainResponse`` Pydantic mirrors do not break.
- ``widen_stored(None, greek_name)`` returns
  ``source="missing"`` with ``error_code="not_stored"`` and
  ``missing_inputs=(greek_name,)``.
- ``merge_stored_with_computed(stored_value, greek_name, computed)``
  prefers stored when present; otherwise pass-through computed; if
  ``computed is None`` and stored is None, returns the same
  ``not_stored`` envelope as ``widen_stored(None, ...)``.
"""

from __future__ import annotations

from tcg.types.options import ComputeResult


def widen_stored(value: float | None, *, greek_name: str) -> ComputeResult:
    """Wrap a Module 1-supplied stored value as a ``ComputeResult``.

    A non-None value becomes ``source="stored"``; None becomes
    ``source="missing"`` with ``error_code="not_stored"`` —
    indicating the caller can opt in to compute by passing
    ``compute_missing=True`` to ``DefaultOptionsChain.snapshot``.
    """
    if value is None:
        return ComputeResult(
            value=None,
            source="missing",
            model=None,
            inputs_used=None,
            missing_inputs=(greek_name,),
            error_code="not_stored",
            error_detail=(
                f"Stored {greek_name} unavailable; "
                "pass compute_missing=true to compute via Black-76."
            ),
        )
    return ComputeResult(
        value=float(value),
        source="stored",
        model=None,
        inputs_used=None,
        missing_inputs=None,
        error_code=None,
        error_detail=None,
    )


def merge_stored_with_computed(
    *,
    stored_value: float | None,
    greek_name: str,
    computed: ComputeResult | None,
) -> ComputeResult:
    """Combine a stored value with an optional computed envelope.

    Precedence:
    1. ``stored_value`` is not None → return ``widen_stored(stored_value, ...)``
       (i.e. ``source="stored"``).  The computed envelope, if any, is
       discarded — stored always wins (spec §3.6 inline note).
    2. ``stored_value`` is None and ``computed`` is provided → pass
       ``computed`` through unchanged.  Module 6 NEVER alters Module 2's
       ``source`` label; Module 2 is the only emitter of
       ``"computed"``.
    3. Both None → ``widen_stored(None, ...)`` =
       ``source="missing"`` / ``error_code="not_stored"``.
    """
    if stored_value is not None:
        return widen_stored(stored_value, greek_name=greek_name)
    if computed is not None:
        return computed
    return widen_stored(None, greek_name=greek_name)
