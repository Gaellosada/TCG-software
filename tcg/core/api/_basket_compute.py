"""Standalone basket → composite-series orchestration.

Thin orchestrator behind the Data-page ``POST /api/data/basket/series``
endpoint.  It materialises a basket (saved or inline) into a typed
:class:`~tcg.types.signal.InstrumentBasket`, derives an explicit date
window when any leg is an option-stream (option-stream resolution needs a
concrete ``(start, end)``), and runs the SHARED ``make_signal_fetcher``
over it — so the standalone series is byte-for-byte the same weighted-sum
the in-signal basket branch produces (parity is tested).

``data.py`` imports only this module + ``_series_fetch`` — never the
signals router — so there is no router→router import.  The leg
materialisers and the fetcher live in :mod:`tcg.core.api._series_fetch`
(shared with ``signals.py``).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from tcg.core.api._models import BasketLeg
from tcg.core.api._series_fetch import (
    _has_option_stream_dependency,
    _saved_basket_leg_to_typed,
    basket_leg_date_intersection,
    make_signal_fetcher,
)
from tcg.data.protocols import MarketDataService
from tcg.engine.signal_exec import SignalValidationError
from tcg.types.persistence import BasketDoc, DocType
from tcg.types.signal import (
    InstrumentBasket,
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
)

if TYPE_CHECKING:
    from tcg.persistence import WriteRepository

_TypedLeg = tuple[
    "InstrumentSpot | InstrumentContinuous | InstrumentOptionStream", float
]


async def _build_basket(
    *,
    svc: MarketDataService,
    repo: "WriteRepository",
    basket_id: str | None,
    asset_class: str | None,
    legs: list[BasketLeg] | list[dict] | None,
) -> InstrumentBasket:
    """Resolve a saved/inline basket request into a typed ``InstrumentBasket``.

    * Saved (``basket_id`` set): load the :class:`BasketDoc` via ``repo``
      and materialise each persisted leg dict — mirrors
      ``signals.py``'s ``_resolve_basket_inputs`` saved branch.
    * Inline (``asset_class`` + ``legs`` set): materialise each
      :class:`BasketLeg` directly (no DB read) — mirrors the inline branch.

    Raises :class:`SignalValidationError` for an unknown / empty saved
    basket or a malformed inline request.
    """
    if basket_id is not None:
        doc = await repo.get_by_id(DocType.BASKET.value, basket_id)
        if doc is None or not isinstance(doc, BasketDoc):
            raise SignalValidationError(f"basket {basket_id!r} not found")
        if not doc.legs:
            raise SignalValidationError(f"basket {basket_id!r} has no legs")
        typed_legs: tuple[_TypedLeg, ...] = tuple(
            _saved_basket_leg_to_typed(
                leg,
                basket_id=basket_id,
                leg_index=i,
                asset_class=doc.asset_class,
            )
            for i, leg in enumerate(doc.legs)
        )
        return InstrumentBasket(
            legs=typed_legs, basket_id=basket_id, asset_class=doc.asset_class
        )

    # Inline path.
    if asset_class is None:
        raise SignalValidationError(
            "inline basket requires 'asset_class' (or supply 'basket_id')"
        )
    if not legs:
        raise SignalValidationError("basket has no legs")
    # Lazy import to avoid a cycle: the materialiser lives in
    # _series_fetch and consumes the typed Pydantic leg's instrument ref.
    from tcg.core.api._series_fetch import _materialise_leg_instrument

    typed: list[_TypedLeg] = []
    for i, leg in enumerate(legs):
        # Accept both validated ``BasketLeg`` instances (endpoint path)
        # and raw leg dicts (programmatic callers) — coerce dicts through
        # the same Pydantic model the wire uses so leg validation is
        # identical to the saved/signal paths.
        leg_model = leg if isinstance(leg, BasketLeg) else BasketLeg.model_validate(leg)
        typed.append(
            (
                _materialise_leg_instrument(
                    leg_model.instrument, input_id="basket", leg_index=i
                ),
                float(leg_model.weight),
            )
        )
    return InstrumentBasket(legs=tuple(typed), basket_id=None, asset_class=asset_class)


async def _resolve_window(
    *,
    svc: MarketDataService,
    basket: InstrumentBasket,
    start: date | None,
    end: date | None,
) -> tuple[date | None, date | None]:
    """Derive the ``(start, end)`` window the fetcher runs over.

    Spot/continuous-only baskets borrow their date axis from the price
    series, so the requested ``(start, end)`` (possibly ``None``) pass
    through untouched.  When ANY leg is an option-stream we MUST hand the
    fetcher a concrete window — otherwise the option_stream branch raises
    "option_stream requires explicit start/end dates".  We derive it from
    the intersection of the legs' date arrays, mirroring the
    ``InstrumentBasket`` branch of ``compute_input_overlap``.
    """
    if not _has_option_stream_dependency(basket):
        return start, end

    # Shared with ``compute_input_overlap``'s basket branch — identical
    # per-leg intersection (proven by the option-leg parity test).
    basket_dates = await basket_leg_date_intersection(
        basket, svc, start=start, end=end, err_prefix="basket"
    )
    lo = int(basket_dates[0])
    hi = int(basket_dates[-1])
    win_start = date(lo // 10000, (lo % 10000) // 100, lo % 100)
    win_end = date(hi // 10000, (hi % 10000) // 100, hi % 100)
    return win_start, win_end


# Resolver ``error_code`` prefixes that are SUCCESS-side annotations, NOT
# failures: ``snapped_to:<iso>`` (a non-NearestToTarget arithmetic target was
# snapped to the nearest listed expiration) and ``coverage_skipped:<iso>`` (a
# strictly-nearer expiration was skipped for a covered one).  Both coexist with
# a REAL value — the value array is the source of truth for NaN-ness — so they
# must NOT be tallied as holes (see stream_resolver's error_codes contract, and
# portfolio.py ``_diagnostic_hint`` which excludes them the same way).
_SUCCESS_SIDE_CODE_PREFIXES: tuple[str, ...] = ("snapped_to:", "coverage_skipped:")


def _is_hole_code(code: str | None) -> bool:
    """True iff ``code`` marks a genuine coverage hole (a missing/NaN value),
    False for ``None`` and for the success-side annotation prefixes."""
    return code is not None and not code.startswith(_SUCCESS_SIDE_CODE_PREFIXES)


def _leg_coverage(record: dict) -> dict:
    """Summarise one option leg's per-date diagnostics into a coverage block.

    ``{descriptor, n, n_holes, counts, dominant_code, first_gap, last_gap}`` —
    ``counts`` maps each hole ``error_code`` to its occurrence count; the
    dominant code is the most frequent; ``first_gap``/``last_gap`` bound the
    affected trade-date range (ISO).  A hole is any date whose error_code names
    a genuine failure; success-side notes (``snapped_to:``/``coverage_skipped:``)
    coexist with a real value and are NOT counted (see ``_is_hole_code``).
    """
    codes: list[str | None] = record.get("error_codes") or []
    dates_arr = record.get("dates")
    n = len(codes)
    counts: dict[str, int] = {}
    gap_ints: list[int] = []
    for i, c in enumerate(codes):
        if c is not None and _is_hole_code(c):
            counts[c] = counts.get(c, 0) + 1
            if dates_arr is not None and i < len(dates_arr):
                gap_ints.append(int(dates_arr[i]))
    n_holes = sum(counts.values())
    dominant = max(counts, key=lambda k: counts[k]) if counts else None

    def _iso(v: int) -> str:
        return f"{v // 10000:04d}-{(v % 10000) // 100:02d}-{v % 100:02d}"

    return {
        "descriptor": record.get("descriptor", "option leg"),
        "n": n,
        "n_holes": n_holes,
        "counts": counts,
        "dominant_code": dominant,
        "first_gap": _iso(min(gap_ints)) if gap_ints else None,
        "last_gap": _iso(max(gap_ints)) if gap_ints else None,
    }


async def compute_basket_series(
    *,
    svc: MarketDataService,
    repo: "WriteRepository",
    basket_id: str | None,
    asset_class: str | None,
    legs: list[BasketLeg] | list[dict] | None,
    start: date | None,
    end: date | None,
    field: str = "close",
    coverage_out: dict | None = None,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Compute a basket's composite series as ``(dates, values)``.

    ``dates`` are int ``YYYYMMDD``; ``values`` is the signed
    weighted-sum of the legs' ``field`` series over the intersection of
    leg date axes.  Reuses the same materialisers + fetcher the in-signal
    basket path uses, so the result is identical (parity-tested).

    When ``coverage_out`` is a dict, it is populated with a coverage summary
    (``{composite:{n,n_holes}, legs:[per-option-leg blocks]}``) so the Data page
    can explain WHY points are missing instead of drawing a silently broken
    line.  The ``(dates, values)`` return is unchanged (parity-preserving).
    """
    basket = await _build_basket(
        svc=svc,
        repo=repo,
        basket_id=basket_id,
        asset_class=asset_class,
        legs=legs,
    )
    win_start, win_end = await _resolve_window(
        svc=svc, basket=basket, start=start, end=end
    )
    diag_sink: list[dict] | None = [] if coverage_out is not None else None
    fetcher = make_signal_fetcher(svc, win_start, win_end, diag_sink=diag_sink)
    dates, values = await fetcher(basket, field)
    if coverage_out is not None:
        n = int(values.size)
        n_holes = int(np.count_nonzero(np.isnan(values)))
        coverage_out["composite"] = {"n": n, "n_holes": n_holes}
        coverage_out["legs"] = [_leg_coverage(rec) for rec in (diag_sink or [])]
    return dates, values
