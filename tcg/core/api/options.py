"""Options router — five GET endpoints under ``/api/options``.

Wave B4 (Phase 1B) wiring layer.  Each handler:

1. Parses query parameters into Pydantic request models.
2. Builds the engine objects via ``_options_wiring`` (per request).
3. Calls the engine, converts the result to a Pydantic response model
   (per Decision A, the engine returns frozen dataclasses; the API uses
   Pydantic mirrors).
4. Returns ``response.model_dump()`` (Decision F — plain dict on wire).

Per Decision B, ``ChainSnapshot.underlying_price: float | None`` is
wrapped to ``ComputeResult`` at this boundary.

Errors are raised as one of the 4 ``OptionsXxxError`` types defined in
``tcg.types.errors`` (Phase 0).  ``tcg_error_handler`` (registered in
``tcg.core.app``) translates them through ``STATUS_MAP`` to HTTP
status codes (400/404/422/502).

Spec reference: §5 (API surface).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError as PydanticValidationError

from tcg.core.api._models_options import (
    ChainResponse,
    ChainSnapshotQuery,
    ChainSnapshotResponse,
    ChainQuery,
    ContractQuery,
    ContractResponse,
    ListRootsResponse,
    SelectQuery,
    SelectResponse,
    SmilePoint,
    SmileSeries,
)
from tcg.core.api._options_wiring import (
    build_options_chain,
    build_options_pricer,
    build_options_selector,
)
from tcg.core.api.common import get_market_data
from tcg.data.protocols import MarketDataService
from tcg.types.errors import (
    OptionsContractNotFound,
    OptionsSelectionError,
    OptionsValidationError,
)
from tcg.types.options import (
    ChainSnapshot,
    ComputeResult,
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
)


router = APIRouter(prefix="/api/options", tags=["options"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_underlying_price(value: float | None) -> dict[str, Any]:
    """Wrap ``ChainSnapshot.underlying_price`` as a ComputeResult dict.

    Per Decision B: Module 6 keeps ``float | None``; the API router
    wraps to ``ComputeResult`` at this boundary.  The actual wrap is
    delegated to ``tcg.engine.options.chain._widen.wrap_underlying_price``
    so the cardinal invariant — ``source="stored"`` is emitted ONLY by
    Module 6's ``_widen.py`` — is preserved (verified by grep in Wave B2).
    """
    # Local import: keeps the API->engine dependency function-scoped, in
    # line with the rest of this module's policy of pulling engine helpers
    # at call sites (see ``_build_contract_row_with_greeks``).
    from tcg.engine.options.chain._widen import wrap_underlying_price

    return dataclasses.asdict(wrap_underlying_price(value))


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert a frozen dataclass tree into JSON-friendly dicts.

    Used for converting engine outputs (``ChainSnapshot``,
    ``OptionContractSeries``, etc.) into shapes the Pydantic response
    models can validate / dump.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_dataclass_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj


def _build_contract_row_with_greeks(
    *,
    row: OptionDailyRow,
    contract: OptionContractDoc,
    underlying_price: float | None,
    pricer: Any,
    compute_missing: bool,
) -> dict[str, Any]:
    """Construct a ``ContractRowWithGreeks``-shaped dict for one row.

    Decision D: include both the ``*_stored`` raw fields AND the
    ComputeResult-wrapped Greeks.  Stored takes precedence; if
    ``compute_missing=True`` and stored is None for a Greek, fill via
    Module 2.
    """
    # Locally import the widening helper — the cardinal invariant is
    # that source="stored" is emitted ONLY by Module 6's _widen.py
    # (verified in Wave B2 by grep).  The router pulls the helper from
    # there to keep the invariant intact.
    from tcg.engine.options.chain._widen import merge_stored_with_computed

    computed = None
    needs_compute = compute_missing and (
        row.iv_stored is None
        or row.delta_stored is None
        or row.gamma_stored is None
        or row.theta_stored is None
        or row.vega_stored is None
    )
    if needs_compute:
        computed = pricer.compute(contract, row, underlying_price)

    iv_cr = merge_stored_with_computed(
        stored_value=row.iv_stored,
        greek_name="iv",
        computed=computed.iv if computed is not None else None,
    )
    delta_cr = merge_stored_with_computed(
        stored_value=row.delta_stored,
        greek_name="delta",
        computed=computed.delta if computed is not None else None,
    )
    gamma_cr = merge_stored_with_computed(
        stored_value=row.gamma_stored,
        greek_name="gamma",
        computed=computed.gamma if computed is not None else None,
    )
    theta_cr = merge_stored_with_computed(
        stored_value=row.theta_stored,
        greek_name="theta",
        computed=computed.theta if computed is not None else None,
    )
    vega_cr = merge_stored_with_computed(
        stored_value=row.vega_stored,
        greek_name="vega",
        computed=computed.vega if computed is not None else None,
    )

    return {
        # Quote fields
        "date": row.date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "bid": row.bid,
        "ask": row.ask,
        "bid_size": row.bid_size,
        "ask_size": row.ask_size,
        "volume": row.volume,
        "open_interest": row.open_interest,
        "mid": row.mid,
        # Raw stored Greek scalars (Decision D)
        "iv_stored": row.iv_stored,
        "delta_stored": row.delta_stored,
        "gamma_stored": row.gamma_stored,
        "theta_stored": row.theta_stored,
        "vega_stored": row.vega_stored,
        "underlying_price_stored": row.underlying_price_stored,
        # ComputeResult wrappers
        "iv": dataclasses.asdict(iv_cr),
        "delta": dataclasses.asdict(delta_cr),
        "gamma": dataclasses.asdict(gamma_cr),
        "theta": dataclasses.asdict(theta_cr),
        "vega": dataclasses.asdict(vega_cr),
    }


# ---------------------------------------------------------------------------
# Endpoint 1 — GET /api/options/roots
# ---------------------------------------------------------------------------


@router.get("/roots")
async def list_roots(
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """List every OPT_* collection with display metadata.

    Errors:
        ``OptionsDataAccessError`` from the reader → 502 via the global
        TCG error handler.
    """
    roots = await svc.list_option_roots()
    payload = ListRootsResponse.model_validate(
        {"roots": [dataclasses.asdict(r) for r in roots]}
    )
    return payload.model_dump()


# ---------------------------------------------------------------------------
# Endpoint 1b — GET /api/options/expirations
# ---------------------------------------------------------------------------


@router.get("/expirations")
async def list_expirations(
    root: str = Query(..., description="OPT_* collection name"),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Distinct expirations available on *root*, sorted ascending.

    Backs the chain / smile date pickers so users can only choose dates
    that actually have contracts.

    Errors:
        ``OptionsDataAccessError`` from the reader → 502.
    """
    dates_ = await svc.list_option_expirations(root)
    return {"root": root, "expirations": [d.isoformat() for d in dates_]}


# ---------------------------------------------------------------------------
# Endpoint 2 — GET /api/options/chain
# ---------------------------------------------------------------------------


@router.get("/chain")
async def get_chain(
    root: str = Query(..., description="OPT_* collection name"),
    date: date = Query(..., description="Trade date (YYYY-MM-DD)"),
    type: Literal["C", "P", "both"] = Query(
        "both", description="Option type filter"
    ),
    expiration_min: date = Query(
        ..., description="Lower bound for expiration window (inclusive)"
    ),
    expiration_max: date = Query(
        ..., description="Upper bound for expiration window (inclusive)"
    ),
    strike_min: float | None = Query(
        None, description="Optional strike lower bound"
    ),
    strike_max: float | None = Query(
        None, description="Optional strike upper bound"
    ),
    compute_missing: bool = Query(
        False,
        description=(
            "Opt in to computing missing Greeks via Module 2; defaults to "
            "stored-only per guardrail #2."
        ),
    ),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Return the chain snapshot for ``(root, date, type, ...)``.

    ``ChainSnapshot.underlying_price: float | None`` is wrapped here as
    a ``ComputeResult`` per Decision B before populating
    ``ChainResponse``.

    Errors:
        ``OptionsValidationError`` (400) when ``expiration_min >
        expiration_max`` (also caught by Module 6 itself).
        ``OptionsDataAccessError`` (502) on Mongo failure.
    """
    # Validate the request shape via Pydantic — for parity with other
    # routers and to surface a structured 400 if anything is off-shape.
    try:
        query = ChainQuery.model_validate(
            {
                "root": root,
                "date": date,
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "compute_missing": compute_missing,
            }
        )
    except PydanticValidationError as exc:
        raise OptionsValidationError(str(exc)) from exc

    if query.expiration_min > query.expiration_max:
        raise OptionsValidationError(
            f"expiration_min={query.expiration_min.isoformat()} > "
            f"expiration_max={query.expiration_max.isoformat()}."
        )

    chain = build_options_chain(svc)
    snapshot: ChainSnapshot = await chain.snapshot(
        root=query.root,
        date=query.date,
        type=query.type,
        expiration_min=query.expiration_min,
        expiration_max=query.expiration_max,
        compute_missing=query.compute_missing,
        strike_min=query.strike_min,
        strike_max=query.strike_max,
    )

    # Wrap underlying_price (Decision B) and serialize rows.
    underlying_dict = _wrap_underlying_price(snapshot.underlying_price)
    rows_payload = [dataclasses.asdict(r) for r in snapshot.rows]

    response = ChainResponse.model_validate(
        {
            "root": snapshot.root,
            "date": snapshot.date,
            "underlying_price": underlying_dict,
            "rows": rows_payload,
            "notes": list(snapshot.notes),
        }
    )
    return response.model_dump()


# ---------------------------------------------------------------------------
# Endpoint 3 — GET /api/options/contract/{coll}/{id}
# ---------------------------------------------------------------------------


@router.get("/contract/{coll}/{contract_id:path}")
async def get_contract(
    coll: str,
    contract_id: str,
    compute_missing: bool = Query(False),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Return one contract's full daily series with ComputeResult-wrapped Greeks.

    Errors:
        ``OptionsContractNotFound`` (404) when the contract id is missing.
        ``OptionsDataAccessError`` (502) on Mongo failure.

    Per Decision D, every row carries both the raw ``*_stored`` fields
    and the ComputeResult-wrapped Greeks.
    """
    try:
        validated = ContractQuery.model_validate(
            {
                "collection": coll,
                "contract_id": contract_id,
                "compute_missing": compute_missing,
                "date_from": date_from,
                "date_to": date_to,
            }
        )
    except PydanticValidationError as exc:
        raise OptionsValidationError(str(exc)) from exc

    series: OptionContractSeries = await svc.get_option_contract(
        validated.collection, validated.contract_id
    )

    # Reuse Module 6's _join.resolve_underlying_price per row.  We
    # construct the ports by hand here (same wiring as
    # ``build_options_chain`` minus the chain object itself).
    from tcg.core.api._options_wiring import (
        _FuturesDataPortAdapter,
        _IndexDataPortAdapter,
    )
    from tcg.engine.options.chain._join import resolve_underlying_price

    index_port = _IndexDataPortAdapter(svc)
    futures_port = _FuturesDataPortAdapter(svc)
    pricer = build_options_pricer() if validated.compute_missing else None

    rows_payload: list[dict[str, Any]] = []
    for row in series.rows:
        if validated.date_from is not None and row.date < validated.date_from:
            continue
        if validated.date_to is not None and row.date > validated.date_to:
            continue

        underlying_price: float | None = None
        if pricer is not None:
            underlying_price = await resolve_underlying_price(
                contract=series.contract,
                row=row,
                target_date=row.date,
                index_port=index_port,
                futures_port=futures_port,
            )

        rows_payload.append(
            _build_contract_row_with_greeks(
                row=row,
                contract=series.contract,
                underlying_price=underlying_price,
                pricer=pricer,
                compute_missing=validated.compute_missing,
            )
        )

    response = ContractResponse.model_validate(
        {
            "contract": dataclasses.asdict(series.contract),
            "rows": rows_payload,
        }
    )
    return response.model_dump()


# ---------------------------------------------------------------------------
# Endpoint 4 — GET /api/options/select
# ---------------------------------------------------------------------------


@router.get("/select")
async def select_contract(
    q: str = Query(
        ...,
        description=(
            "JSON-encoded SelectQuery payload.  Phase 1 ergonomic "
            "compromise: nested discriminated unions (criterion / "
            "maturity) do not serialize cleanly as flat query params, "
            "so the entire SelectQuery is sent as a single JSON-encoded "
            "string."
        ),
    ),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Resolve a selection criterion + maturity rule into a contract.

    ``q`` is a JSON-encoded ``SelectQuery`` object.  This avoids
    FastAPI's known limitation with nested discriminated unions in
    query strings.

    Errors:
        ``OptionsValidationError`` (400) on JSON parse / shape errors.
        ``OptionsSelectionError`` (422) when no chain exists for the
        date OR the criterion cannot be resolved with stored data
        without compute opt-in.
    """
    try:
        payload = SelectQuery.model_validate_json(q)
    except PydanticValidationError as exc:
        raise OptionsValidationError(
            f"Invalid 'q' SelectQuery payload: {exc}"
        ) from exc
    except ValueError as exc:
        # Bare-JSON parse error path
        raise OptionsValidationError(
            f"Could not decode 'q' as JSON: {exc}"
        ) from exc

    selector = build_options_selector(
        svc, with_pricer=payload.compute_missing_for_delta_selection
    )

    # Convert Pydantic discriminated unions back to the dataclass
    # variants the engine expects.  ``model_dump()`` produces a dict
    # we can replay through dataclass constructors keyed on ``kind``.
    criterion = _criterion_pydantic_to_dataclass(payload.criterion)
    maturity = _maturity_pydantic_to_dataclass(payload.maturity)

    result = await selector.select(
        root=payload.root,
        date=payload.date,
        type=payload.type,
        criterion=criterion,
        maturity=maturity,
        compute_missing_for_delta=payload.compute_missing_for_delta_selection,
    )

    # Spec §5.3 reserves OptionsSelectionError for "criterion
    # unresolvable (no chain on date, all delta_stored missing without
    # compute)".  We map only the unambiguous error_codes that match
    # this definition; other errors (e.g. ``no_match_within_tolerance``)
    # are returned as a 200 with structured ``error_code``.
    if (
        result.contract is None
        and result.error_code in {"missing_delta_no_compute", "no_chain_for_date"}
    ):
        raise OptionsSelectionError(
            f"Selection unresolvable: error_code={result.error_code!r}, "
            f"diagnostic={result.diagnostic!r}"
        )

    response = SelectResponse.model_validate(
        {
            "contract": (
                dataclasses.asdict(result.contract)
                if result.contract is not None
                else None
            ),
            "matched_value": result.matched_value,
            "error_code": result.error_code,
            "diagnostic": result.diagnostic,
        }
    )
    return response.model_dump()


def _criterion_pydantic_to_dataclass(criterion: Any) -> Any:
    """Convert a Pydantic SelectionCriterion to its frozen-dataclass twin."""
    from tcg.types.options import ByDelta, ByMoneyness, ByStrike

    kind = criterion.kind
    if kind == "by_delta":
        return ByDelta(
            target_delta=criterion.target_delta,
            tolerance=criterion.tolerance,
            strict=criterion.strict,
        )
    if kind == "by_moneyness":
        return ByMoneyness(
            target_K_over_S=criterion.target_K_over_S,
            tolerance=criterion.tolerance,
        )
    if kind == "by_strike":
        return ByStrike(strike=criterion.strike)
    raise OptionsValidationError(f"Unknown criterion kind {kind!r}")


def _maturity_pydantic_to_dataclass(maturity: Any) -> Any:
    """Convert a Pydantic MaturityRule to its frozen-dataclass twin."""
    from tcg.types.options import (
        EndOfMonth,
        FixedDate,
        NearestToTarget,
        NextThirdFriday,
        PlusNDays,
    )

    kind = maturity.kind
    if kind == "next_third_friday":
        return NextThirdFriday(offset_months=maturity.offset_months)
    if kind == "end_of_month":
        return EndOfMonth(offset_months=maturity.offset_months)
    if kind == "plus_n_days":
        return PlusNDays(n=maturity.n)
    if kind == "fixed":
        return FixedDate(date=maturity.date)
    if kind == "nearest_to_target":
        return NearestToTarget(target_dte_days=maturity.target_dte_days)
    raise OptionsValidationError(f"Unknown maturity kind {kind!r}")


# ---------------------------------------------------------------------------
# Endpoint 5 — GET /api/options/chain-snapshot
# ---------------------------------------------------------------------------


@router.get("/chain-snapshot")
async def get_chain_snapshot(
    root: str = Query(...),
    date: date = Query(...),
    type: Literal["C", "P"] = Query("C"),
    expirations: list[date] = Query(
        ..., description="Expiration dates (max 8 per request)"
    ),
    field: Literal["iv", "delta"] = Query("iv"),
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Return per-expiration smile series for a Tier-2 multi-expiration view.

    Errors:
        ``OptionsValidationError`` (400) when more than 8 expirations
        are passed (also enforced by ``ChainSnapshotQuery`` validator).
        ``OptionsDataAccessError`` (502) on Mongo failure.
    """
    try:
        query = ChainSnapshotQuery.model_validate(
            {
                "root": root,
                "date": date,
                "type": type,
                "expirations": expirations,
                "field": field,
            }
        )
    except PydanticValidationError as exc:
        raise OptionsValidationError(str(exc)) from exc

    chain = build_options_chain(svc)

    series_payload: list[dict[str, Any]] = []
    underlying_value: float | None = None

    for expiration in query.expirations:
        snapshot = await chain.snapshot(
            root=query.root,
            date=query.date,
            type=query.type,
            expiration_min=expiration,
            expiration_max=expiration,
            compute_missing=False,
        )
        # underlying_price is the same across expirations (same root +
        # date); take the first non-None we observe.
        if underlying_value is None and snapshot.underlying_price is not None:
            underlying_value = snapshot.underlying_price

        points: list[dict[str, Any]] = []
        for row in snapshot.rows:
            cr = row.iv if query.field == "iv" else row.delta
            point = SmilePoint.model_validate(
                {
                    "strike": row.strike,
                    "K_over_S": row.K_over_S,
                    "value": dataclasses.asdict(cr),
                }
            )
            points.append(point.model_dump())
        smile = SmileSeries.model_validate(
            {"expiration": expiration, "points": points}
        )
        series_payload.append(smile.model_dump())

    response = ChainSnapshotResponse.model_validate(
        {
            "root": query.root,
            "date": query.date,
            "underlying_price": _wrap_underlying_price(underlying_value),
            "series": series_payload,
        }
    )
    return response.model_dump()


__all__ = ["router"]


# Suppress unused-import lint for the module-level helpers used only
# inside endpoint bodies.
_ = (json,)
