"""Cartesian-matrix equivalence proof for ``_choose_path`` (Wave 25).

The routing refactor collapsed the previously-scattered eligibility predicates
in ``stream_resolver`` (``_hold_two_phase`` / ``_delta_pushdown`` /
``_fast_path_eligible`` + the four legacy per-date rejects) into the single pure
function ``_choose_path``.  Byte-identity of ROUTING is the governing constraint:
for EVERY input, ``_choose_path`` must select the exact same path + params the
old scattered expressions did.

This test proves it directly.  ``_oracle`` below re-implements the OLD scattered
expressions VERBATIM (copied from the pre-refactor source) as an independent
reference.  Over the full cartesian grid of inputs, every field of
``_choose_path``'s decision is asserted equal to the oracle's.  If any combo
differs, the transcription changed routing — a byte-identity bug.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

from tcg.engine.options.series.stream_resolver import (
    _BS_MID,
    _PUSHDOWN_K,
    PathDecision,
    ResolvePath,
    _choose_path,
)
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    EndOfMonth,
    FixedDate,
    NearestToTarget,
    NextThirdFriday,
    PlusNDays,
)


@dataclass(frozen=True)
class _UnknownSel:
    """A selection type the resolver does not special-case (none exists today).

    Exercises the ``else`` of every ``isinstance(selection, (ByStrike, ByDelta,
    ByMoneyness))`` gate — the shape that stays on the legacy per-expiration path.
    """

    marker: int = 0


@dataclass(frozen=True)
class _HoldButNoRollMath:
    """Synthetic maturity that claims hold cadence (``is_hold_cadence`` True).

    Exercises the hold-cadence branch of the legacy reject for a type that is NOT
    ``EndOfMonth`` (the D1 F3 footgun class) — the reject message interpolates
    ``type(maturity).__name__``, so this proves the interpolation is preserved.
    """

    @property
    def is_hold_cadence(self) -> bool:
        return True


# ── Reference oracle: the OLD scattered expressions, copied VERBATIM ──────────
def _oracle(
    *,
    has_bulk_reader: bool,
    selection: object,
    maturity: object,
    stream: str,
    hold_between_rolls: bool,
    roll_offset_nonzero: bool,
    supports_bulk_multi: bool,
    supports_held_rows: bool,
    compute_missing_delta: bool,
) -> tuple[str | None, bool, bool, bool]:
    """Return ``(legacy_reject, hold_two_phase, delta_pushdown, fast_path)``.

    Transcribed from the pre-refactor ``resolve_option_stream`` legacy guards and
    ``_resolve_bulk`` eligibility locals — NOT from ``_choose_path``.
    """
    # resolve_option_stream legacy per-date rejects (in original order).
    legacy_reject: str | None = None
    if roll_offset_nonzero:
        legacy_reject = (
            "roll_offset requires the bulk chain reader; the legacy per-date "
            "path does not support it"
        )
    elif hold_between_rolls:
        legacy_reject = (
            "hold_between_rolls requires the bulk chain reader; the legacy "
            "per-date path does not support select-and-hold"
        )
    elif bool(getattr(maturity, "is_hold_cadence", False)):
        legacy_reject = (
            f"{type(maturity).__name__} maturity requires the bulk chain reader; "
            "the legacy per-date path does not support the monthly-hold roll"
        )
    elif stream == "bs_mid":
        legacy_reject = (
            "bs_mid stream requires the bulk chain reader; the legacy per-date "
            "path does not support the computed Black-76 price stream"
        )

    # _resolve_bulk._hold_two_phase
    hold_two_phase = (
        hold_between_rolls
        and isinstance(selection, (ByStrike, ByDelta, ByMoneyness))
        and bool(supports_bulk_multi)
        and bool(supports_held_rows)
    )
    # _resolve_bulk._delta_pushdown (truthiness — old value was a tuple-or-None)
    delta_pushdown = (
        isinstance(selection, ByDelta)
        and not compute_missing_delta
        and (not hold_between_rolls or hold_two_phase)
    )
    # _resolve_bulk._fast_path_eligible
    fast_path_eligible = (
        isinstance(selection, (ByStrike, ByDelta, ByMoneyness)) and supports_bulk_multi
    )
    return (
        legacy_reject,
        bool(hold_two_phase),
        bool(delta_pushdown),
        bool(fast_path_eligible),
    )


_SELECTIONS = [
    ByDelta(-0.10),
    ByStrike(4000.0),
    ByMoneyness(1.02),
    _UnknownSel(),
]
_MATURITIES = [
    NearestToTarget(30),
    EndOfMonth(),
    PlusNDays(30),
    FixedDate(date(2024, 1, 19)),
    NextThirdFriday(),
    _HoldButNoRollMath(),
]
_STREAMS = [_BS_MID, "mid"]
_BOOLS = [False, True]


def _grid():
    for (
        selection,
        maturity,
        stream,
        has_bulk_reader,
        hold_between_rolls,
        roll_offset_nonzero,
        supports_bulk_multi,
        supports_held_rows,
        compute_missing_delta,
    ) in itertools.product(
        _SELECTIONS,
        _MATURITIES,
        _STREAMS,
        _BOOLS,  # has_bulk_reader
        _BOOLS,  # hold_between_rolls
        _BOOLS,  # roll_offset_nonzero
        _BOOLS,  # supports_bulk_multi
        _BOOLS,  # supports_held_rows
        _BOOLS,  # compute_missing_delta
    ):
        yield dict(
            selection=selection,
            maturity=maturity,
            stream=stream,
            has_bulk_reader=has_bulk_reader,
            hold_between_rolls=hold_between_rolls,
            roll_offset_nonzero=roll_offset_nonzero,
            supports_bulk_multi=supports_bulk_multi,
            supports_held_rows=supports_held_rows,
            compute_missing_delta=compute_missing_delta,
        )


def test_choose_path_matches_old_scattered_predicates_for_every_combo():
    """Every field of every decision equals the verbatim old-expression oracle."""
    n = 0
    for kw in _grid():
        n += 1
        decision: PathDecision = _choose_path(**kw)
        exp_reject, exp_htp, exp_dp, exp_fp = _oracle(**kw)

        assert decision.legacy_reject == exp_reject, kw
        assert decision.hold_two_phase == exp_htp, kw
        assert decision.delta_pushdown == exp_dp, kw
        assert decision.fast_path_eligible == exp_fp, kw

    # 4 sel × 6 mat × 2 stream × 2^6 bools = 3072 combinations.
    assert n == 4 * 6 * 2 * (2**6) == 3072


def test_derived_path_summary_is_consistent_with_the_booleans():
    """The ``path`` summary matches the boolean fields it summarises (all combos)."""
    for kw in _grid():
        d = _choose_path(**kw)
        if not kw["has_bulk_reader"] or not d.fast_path_eligible:
            assert d.path is ResolvePath.LEGACY_PER_DATE, kw
        elif d.delta_pushdown:
            assert d.path is ResolvePath.BULK_PUSHDOWN, kw
        else:
            assert d.path is ResolvePath.BULK_FULLCHAIN, kw


def test_delta_pushdown_only_fires_for_bydelta_so_engine_tuple_is_safe():
    """When ``delta_pushdown`` is True the selection is always ByDelta.

    The engine re-materialises ``(target_delta, _PUSHDOWN_K)`` guarded on this
    invariant; prove no non-ByDelta input ever sets the flag.
    """
    fired = 0
    for kw in _grid():
        d = _choose_path(**kw)
        if d.delta_pushdown:
            fired += 1
            assert isinstance(kw["selection"], ByDelta), kw
            # The tuple the engine would build is well-formed.
            assert (float(kw["selection"].target_delta), _PUSHDOWN_K) == (-0.10, 8)
    assert fired > 0  # the pushdown path IS exercised by the grid


# --------------------------------------------------------------------------- #
# Runtime safeguard: _require_stored_delta_pushdown (audit_d1/d2 F1)
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402

from tcg.engine.options.series.stream_resolver import (  # noqa: E402
    _require_stored_delta_pushdown,
)


def test_pushdown_guard_is_a_noop_across_the_whole_grid():
    """The guard NEVER fires for any routing decision the resolver produces:
    ``_choose_path`` only sets ``delta_pushdown`` when ``compute_missing_delta``
    is False, so ``pushdown_engaged and compute_missing`` is unreachable today.
    Feeding the guard the SAME two flags the resolver would feed it must not raise
    for any of the 3072 combos.
    """
    for kw in _grid():
        d = _choose_path(**kw)
        # No exception == no-op.  (delta_pushdown True ⟹ compute_missing False.)
        _require_stored_delta_pushdown(
            pushdown_engaged=d.delta_pushdown,
            compute_missing_delta=kw["compute_missing_delta"],
        )
        if d.delta_pushdown:
            assert kw["compute_missing_delta"] is False, kw


def test_pushdown_guard_fires_when_precondition_violated():
    """If a future edit ever engages the pushdown while compute-missing is on,
    the guard raises LOUD (never a silent under-inclusive pick)."""
    with pytest.raises(ValueError, match="compute-missing"):
        _require_stored_delta_pushdown(
            pushdown_engaged=True, compute_missing_delta=True
        )


def test_pushdown_guard_permits_every_valid_combination():
    """The three reachable combinations are all no-ops."""
    for engaged, cm in ((True, False), (False, True), (False, False)):
        _require_stored_delta_pushdown(
            pushdown_engaged=engaged, compute_missing_delta=cm
        )
