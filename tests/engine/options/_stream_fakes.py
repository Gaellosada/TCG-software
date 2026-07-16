"""Shared synthetic fakes for ``resolve_option_stream`` bulk-path tests.

Lifted verbatim (as a superset) from the per-file copies that previously
lived in the resolver test suites (``test_stream_roll_offset.py`` and the
former mid-adjustment suite) so they build chains the same way.

* :func:`_contract` / :func:`_row` — build an ``OptionContractDoc`` /
  ``OptionDailyRow`` with sensible defaults; ``_row`` accepts an optional
  ``mid`` (``None`` ⇒ no quoted bid/ask/mid) plus ``iv`` / ``delta``.
* :class:`FakeBulkChainReader` — three-phase bulk reader: filters each date's
  full chain by type / expiration window / cycle and returns the matching rows
  for every requested date in one call.  Records every call's kwargs in
  ``bulk_calls`` for optional assertion.
* :class:`FakeChainReader` — per-date reader satisfying the resolver signature
  (and the ``NearestToTarget`` probe query).  Records calls in ``calls``.

NOTE: ``test_stream_resolver.py`` keeps its OWN copies — they diverge from
these (richer ``_row`` params, richer recorded-call dicts) and are left
untouched on purpose.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

from tcg.types.options import OptionContractDoc, OptionDailyRow


def _cycle_matches(row_cycle: str, cycle_filter: object) -> bool:
    """Mirror the real SQL ``_cycle_predicate`` semantics for the fakes.

    ``None`` = no filter (match all); a scalar str = exact match; a
    (non-str) sequence = membership (the monthly 3rd-Friday series expands to
    ``('M','W3 Friday')`` at the wiring layer, so the engine may pass a tuple).
    """
    if cycle_filter is None:
        return True
    if isinstance(cycle_filter, str):
        return row_cycle == cycle_filter
    return row_cycle in tuple(cycle_filter)


def _contract(
    *,
    strike: float,
    expiration: date,
    type_: Literal["C", "P"] = "C",
    cycle: str = "M",
    collection: str = "OPT_SP_500",
) -> OptionContractDoc:
    cid = f"{collection}_K{int(strike)}_{type_}_{expiration.isoformat()}_{cycle}"
    return OptionContractDoc(
        collection=collection,
        contract_id=cid,
        root_underlying="IND_SP_500",
        underlying_ref="FUT_SP_500_EMINI",
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle=cycle,
        strike=float(strike),
        type=type_,
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _row(
    *,
    row_date: date,
    mid: float | None = 1.05,
    iv: float | None = 0.20,
    delta: float | None = 0.50,
    close: float | None = None,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=close,
        bid=mid - 0.05 if mid is not None else None,
        ask=mid + 0.05 if mid is not None else None,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=mid,
        iv_stored=iv,
        delta_stored=delta,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


class FakeBulkChainReader:
    """Bulk chain reader returning synthetic chains keyed by date.

    Filters each date's full chain by type / expiration window / cycle and
    returns the matching rows for every requested date in one call.
    """

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains_by_date
        self.bulk_calls: list[dict] = []

    async def query_chain_bulk(
        self,
        *,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        self.bulk_calls.append(
            {
                "root": root,
                "dates": list(dates),
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "expiration_cycle": expiration_cycle,
            }
        )
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            chain = self._chains.get(d, [])
            filtered = [
                (c, r)
                for (c, r) in chain
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and _cycle_matches(c.expiration_cycle, expiration_cycle)
            ]
            if filtered:
                result[d] = filtered
        return result


class FakeChainReader:
    """Per-date chain reader.

    Satisfies the resolver signature (and serves the ``NearestToTarget`` probe
    query); the bulk path does not call it for ByStrike / ByDelta.
    """

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains_by_date
        self.calls: list[dict] = []

    async def query_chain(
        self,
        *,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        self.calls.append(
            {
                "root": root,
                "date": date,
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "expiration_cycle": expiration_cycle,
                "limit": limit,
            }
        )
        chain = self._chains.get(date, [])
        out = [
            (c, r)
            for (c, r) in chain
            if (c.type == type or type == "both")
            and expiration_min <= c.expiration <= expiration_max
            and _cycle_matches(c.expiration_cycle, expiration_cycle)
        ]
        return out[:limit] if limit is not None else out
