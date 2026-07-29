"""Validate a ``data_source="v2"`` run at the API boundary, before the engine.

Why this exists — and why it cannot live lower down
---------------------------------------------------
The v2 adapter and options reader DO raise typed, actionable errors
(``tcg.data._v2_compat.errors``). For a **price** leg those reach the user
intact: the adapter is called directly from the route's fetch path.

For an **option** leg they do not. The engine's Phase-B year-chunk resolver
(``tcg/engine/options/series/stream_resolver.py``) catches every per-chunk
exception and degrades to the per-expiration path, which then yields an
all-NaN stream. Whatever the reader said is lost; the user gets
``"all option stream values are NaN … no_chain_for_date"`` — a 400 that names
neither the cause nor the fix.

The engine must NOT learn that v2 exists (guardrail Sign 3 — ``tcg.engine`` ⊥
``tcg.data``, import-linter enforced), and widening its exception handling would
also put the v1 byte-identity gate at risk. So the fix is architectural rather
than local: check every v2 precondition HERE, up front, so that no v2-specific
error ever has to escape from inside a resolve. A request that reaches the
engine is one v2 can serve.

Scope
-----
:func:`check_v2_preconditions` and everything it calls is PURE (no DB). The one
precondition that needs a query — the option coverage floor (spec §11 E7) —
lives here too, as :func:`check_v2_option_coverage_floor`, taking the service as
a plain argument. It used to sit inline in ``portfolio.py``; that left the
signals route without a floor check at all, so a v2 signal starting before the
EW3 floor silently returned a short curve that read as a strategy difference
against the v1 run. Both routes now call the same helper.

Everything in this module no-ops for ``data_source == "v1"``.

What is deliberately NOT checked here: futures/index coverage floors (no
``price_trade_date_coverage`` on the service protocol — a documented gap, not an
omission), and the E6 theta/vega unit divergence, which the spec makes a
non-blocking warning rather than an error.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Iterator, Mapping

from tcg.data.protocols import MarketDataService
from tcg.data._v2_compat import (
    V2_OPTIONS_COLLECTION,
    V2_SUPPORTED_COLLECTIONS,
    V2_UNAVAILABLE_OPTION_STREAMS,
    V2CollectionUnavailable,
    V2DataUnavailable,
    V2MissingCycleFilter,
    V2UnsupportedCycle,
    V2UnsupportedField,
)
from tcg.types.errors import ValidationError
from tcg.types.options import WEEKLY_CYCLE_TAGS

#: Leg/instrument ``type`` discriminators that carry an option contract. The
#: portfolio router and the signal instrument refs happen to use the same
#: literal, which is what lets one walk cover both request shapes.
_OPTION_TYPES = frozenset({"option_stream"})

#: Discriminators that name a plain price series. ``"instrument"`` is the
#: portfolio leg's word for it, ``"spot"`` the signal instrument ref's.
_PRICE_TYPES = frozenset({"instrument", "continuous", "spot"})


def _iter_typed_nodes(node: Any, path: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield ``(path, node)`` for every dict in the tree carrying a ``type``.

    Walking the JSON dump rather than the Pydantic models is deliberate: a v2
    run nests arbitrarily (composed portfolio legs inline a whole child body,
    signal legs inline a whole signal spec whose inputs inline instrument refs,
    a basket leg inlines yet another), and ``SignalComputeRequest.instruments``
    is typed ``dict[str, Any]`` — untyped by construction. One generic walk
    keyed on the ``type`` discriminator reaches every level uniformly, and a
    future nesting depth needs no wiring step anyone can forget.
    """
    if isinstance(node, Mapping):
        if isinstance(node.get("type"), str):
            yield path, node
        for key, value in node.items():
            yield from _iter_typed_nodes(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _iter_typed_nodes(value, f"{path}[{index}]")


def _check_option_node(node: Mapping[str, Any]) -> None:
    """Raise the typed v2 error for the first unmet precondition on an option leg."""
    collection = node.get("collection")
    if isinstance(collection, str) and collection != V2_OPTIONS_COLLECTION:
        raise V2CollectionUnavailable(collection)

    # Cycle. ``None`` and ``""`` are DIFFERENT user errors: the first is "you
    # gave no filter", the second is an explicitly empty one. Both are fatal on
    # v2 (v1 would answer with monthlies AND weeklies), but they get distinct
    # messages so the remedy is unambiguous.
    cycle = node.get("cycle")
    if cycle is None:
        raise V2MissingCycleFilter()
    # v2 serves every WEEKLY cycle: the four concrete ``"W# Friday"`` tags AND
    # the UI's generic "Weekly" umbrella ``"W"`` — the reader routes ``"W"`` to
    # all four EW objects (see ``options_reader._route_objects``). Accept exactly
    # what the reader serves (``WEEKLY_CYCLE_TAGS``); the monthly ``"M"`` /
    # quarterly ``"Q"`` / empty ``""`` cases still fall through and raise. Using
    # ``EW_OBJECT_BY_CYCLE`` here (concrete tags only) was stricter than the
    # reader and rejected the serviceable "all weeklies" run.
    if not isinstance(cycle, str) or cycle not in WEEKLY_CYCLE_TAGS:
        raise V2UnsupportedCycle(str(cycle))

    # Stream. ``mid`` is the model DEFAULT and by far the most common stream in
    # saved signals, and v2 cannot serve it — so without this check the single
    # most likely v2 option run is exactly the one that dies as an
    # unattributable all-NaN curve. Only streams ``OptionStreamLabel`` actually
    # admits are listed in ``V2_UNAVAILABLE_OPTION_STREAMS``; there is no point
    # guarding a spelling the request model already rejects.
    stream = node.get("stream")
    if isinstance(stream, str) and stream in V2_UNAVAILABLE_OPTION_STREAMS:
        raise V2UnsupportedField(stream)


def _check_price_node(node: Mapping[str, Any]) -> None:
    """Raise when a price leg names a collection v2 does not serve.

    Deliberately NOT checked here: the E2 "INDEX but not ``IND_SP_500``" case.
    The adapter raises it from ``get_prices``/``get_aligned_prices``, which the
    route calls directly — that error is NOT swallowed, so duplicating it buys
    nothing, while a boundary copy would reject on a symbol spelling the
    adapter might yet accept. Only add a boundary check where the error would
    otherwise be lost.
    """
    collection = node.get("collection")
    if isinstance(collection, str) and collection not in V2_SUPPORTED_COLLECTIONS:
        raise V2CollectionUnavailable(collection)


def check_v2_preconditions(payload: Mapping[str, Any], *, data_source: str) -> None:
    """Reject a v2 request the warehouse cannot serve, naming the offending leg.

    *payload* is the request body as JSON (``model_dump(mode="json")``).
    No-ops entirely for ``data_source == "v1"`` — the frozen reference path must
    not gain a single new branch (guardrail Sign 1).

    The typed ``V2DataUnavailable`` messages are the SINGLE source of truth for
    the wording (spec §11 E1-E5); this re-raises them as ``ValidationError`` with
    the leg label prepended. Both carry ``error_type="validation_error"`` and so
    render through the identical HTTP 400 envelope — the only difference is that
    the user now learns WHICH leg to fix.
    """
    if data_source == "v1":
        return

    for path, node in _iter_typed_nodes(payload, ""):
        node_type = node.get("type")
        if node_type in _OPTION_TYPES:
            check = _check_option_node
        elif node_type in _PRICE_TYPES:
            check = _check_price_node
        else:
            continue
        try:
            check(node)
        except V2DataUnavailable as exc:
            raise ValidationError(f"{_label(path)}: {exc}") from exc


def collect_v2_option_roots(
    payload: Mapping[str, Any], *, data_source: str
) -> set[str]:
    """Option collections named anywhere in *payload*, at any nesting depth.

    Reuses the same walk as :func:`check_v2_preconditions`, so a shape the
    precondition checker reaches is a shape the coverage floor reaches too —
    the two cannot drift apart. Returns an empty set for ``data_source == "v1"``
    so the caller's loop is a no-op on the frozen path (Sign 1).

    Basket legs resolved from the DB are NOT in the payload (a saved basket ref
    is just an id on the wire); the signals route unions those in separately.
    """
    if data_source == "v1":
        return set()
    roots: set[str] = set()
    for _path, node in _iter_typed_nodes(payload, ""):
        if node.get("type") in _OPTION_TYPES:
            collection = node.get("collection")
            if isinstance(collection, str) and collection:
                roots.add(collection)
    return roots


async def check_v2_option_coverage_floor(
    roots: Iterable[str],
    svc: MarketDataService,
    start_date: date | None,
    *,
    data_source: str,
) -> None:
    """Reject a v2 run that starts before the warehouse has option data (E7).

    v2's option series begin years after v1's (EW1/EW2/EW4 ≈ 2011, EW3 from
    2016-02-22). An earlier start fails nowhere below — it silently returns a
    SHORTER curve, which read against a v1 run of the same spec looks like a
    strategy difference rather than a data gap. Fail loudly and NAME the floor.

    This is the ONE precondition that needs a query, which is why it takes *svc*
    rather than living in the pure checker. It no-ops for ``data_source == "v1"``
    (Sign 1), for an open-ended start, and for a run with no option legs — so
    the common case costs zero round-trips.
    """
    if data_source == "v1" or start_date is None:
        return
    for root in sorted(set(roots)):
        first, _last = await svc.option_trade_date_coverage(root)
        if first is not None and start_date < first:
            raise ValidationError(
                f'Data source "v2" has no {root} option data before '
                f"{first.isoformat()}, but this run starts "
                f"{start_date.isoformat()}. Move the start date to "
                f"{first.isoformat()} or later, or switch this run to data "
                f'source "v1".'
            )


def _label(path: str) -> str:
    """Turn a walk path into something a user can find in their portfolio.

    ``legs.short_put`` → ``Leg 'short_put'``; anything deeper keeps the full
    path, because in a composed portfolio the same leg name can occur at several
    depths and the bare name would be ambiguous.
    """
    parts = path.split(".")
    if len(parts) == 2 and parts[0] == "legs":
        return f"Leg '{parts[1]}'"
    return f"Leg '{path}'" if path else "This request"
