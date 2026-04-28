"""Module 7 — per-contract daily P&L replay (``tcg.engine.options.pnl``).

This module implements mark-to-market P&L replay for a single held option
contract.  It is a Phase 2 primitive: not API-surfaced in Phase 1, but
fully implemented and tested here so that Phase 2 can reuse it without
changes.

Design decisions / judgment calls
----------------------------------

Entry-row matching — exact-match required
    We require an exact ``row.date == entry_date`` match.  A "first row >=
    entry_date" fallback would silently mis-price the entry when data has
    gaps (e.g. a bank-holiday gap moves the effective entry price without
    the caller knowing).  The caller is responsible for picking a valid
    trading date.  If the entry row is absent, a ``ValueError`` with a
    descriptive message is raised so the problem surfaces immediately.

Empty series (no rows in [entry_date, exit_date])
    Returning a ``PnLSeries`` with empty ``points`` and ``exit_reason=
    "contract_data_ended"`` is the correct behavior.  We have validated the
    entry mark (entry_price is set), so the series is valid — it just has no
    subsequent marks to replay.  This can happen when a contract is entered on
    its expiration day or when the data feed ends on the entry date.

Long-gap behavior — entire gap materialises on resume day
    When marks are ``None`` for N consecutive days and then resume, the full
    price move (``last_known_mark_before_gap → resumed_mark``) is recorded as
    ``pnl_daily`` on the resume day.  It is NOT amortised across the gap days.
    Each gap day carries ``pnl_daily=0`` and ``mark=None``.

    **Callers must be aware**: a large ``pnl_daily`` on the resume day does
    NOT mean a single-day extreme move — it represents the accumulated MTM
    catch-up for the full gap.  Notes are appended for each missing-mark day
    to make the gap visible in the output.

    Rationale: amortisation would be an arbitrary synthetic interpolation,
    which violates the "no synthetic prices" principle.  The jump-on-resume
    is the honest representation of what we know.

No Black-Scholes re-pricing
    Even when marks are ``None`` for long stretches, we never call Module 2
    (pricing) to fill them.  Module 7 has no dependency on Module 2.

Spec reference: OPTIONS_FEATURE_SPEC.md §3.7.
Guardrail #4: ``mid = (bid+ask)/2``; never default to ``close``.
Guardrail #8: no ``tcg.data`` imports (use the local ``OptionsDataPort``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from tcg.types.options import (
    OptionContractDoc,
    OptionDailyRow,
    PnLPoint,
    PnLSeries,
)

from tcg.engine.options.pnl.protocol import OptionsDataPort


class DefaultOptionsPnL:
    """Default implementation of the ``OptionsPnL`` Protocol.

    Parameters
    ----------
    port:
        Any object structurally satisfying ``OptionsDataPort`` — i.e.
        exposing ``async def get_contract(collection, contract_id) ->
        OptionContractSeries``.  In production the wiring layer in
        ``tcg.core`` injects the real ``OptionsDataReader`` here.
    """

    def __init__(self, port: OptionsDataPort) -> None:
        self._port = port

    async def compute(
        self,
        contract: OptionContractDoc,
        entry_date: date,
        qty: float,
        exit_date: date | None = None,
        mark_field: Literal["mid", "close"] = "mid",
    ) -> PnLSeries:
        """Replay daily P&L for *contract* from *entry_date* onward.

        See ``OptionsPnL.compute`` docstring for full parameter documentation.

        Algorithm (step numbers match the brief):

        1. Fetch the full series from the port.
        2. Trim rows to [entry_date, exit_date or expiration].
        3. Locate the entry row by exact date match.
        4. Extract entry_price; raise ``ValueError`` if None.
        5. Walk forward, building ``PnLPoint`` per day.
        6. Determine exit_reason from the last processed row.
        7. Return ``PnLSeries``.
        """
        end_date = exit_date if exit_date is not None else contract.expiration

        # Step 1 — fetch full series from Module 1 via port.
        series = await self._port.get_contract(contract.collection, contract.contract_id)

        # Step 2 — trim to [entry_date, end_date].
        trimmed: list[OptionDailyRow] = [
            row for row in series.rows
            if entry_date <= row.date <= end_date
        ]

        # Step 3 — locate entry row by exact date match.
        entry_row: OptionDailyRow | None = None
        for row in trimmed:
            if row.date == entry_date:
                entry_row = row
                break

        if entry_row is None:
            raise ValueError(
                f"Entry row missing: no row found for entry_date={entry_date} "
                f"in contract '{contract.contract_id}' (collection '{contract.collection}'). "
                f"Exact-match is required — choose a valid trading date."
            )

        # Step 4 — entry_price from mark_field; fail fast if None.
        entry_price = _get_mark(entry_row, mark_field)
        if entry_price is None:
            raise ValueError(
                f"entry_price is None at {entry_date} for contract "
                f"'{contract.contract_id}' (mark_field='{mark_field}'); "
                f"cannot replay P&L. Ensure the entry row has a non-None {mark_field}."
            )

        # Step 5 — walk rows after entry_date, building PnLPoints.
        points: list[PnLPoint] = []
        notes: list[str] = []
        pnl_cumulative: float = 0.0
        last_known_mark: float = entry_price

        subsequent_rows = [row for row in trimmed if row.date > entry_date]

        for row in subsequent_rows:
            mark = _get_mark(row, mark_field)

            if mark is None:
                # Missing mark: P&L frozen, daily contribution is zero.
                pnl_daily = 0.0
                note = f"Mark missing on {row.date} (mark_field='{mark_field}'); pnl_daily=0"
                notes.append(note)
                # pnl_cumulative stays at last known value.
            else:
                # Non-None mark: daily P&L is the move from last known mark.
                pnl_daily = qty * (mark - last_known_mark)
                pnl_cumulative += pnl_daily
                last_known_mark = mark

            points.append(PnLPoint(
                date=row.date,
                mark=mark,
                pnl_cumulative=pnl_cumulative,
                pnl_daily=pnl_daily,
            ))

        # Step 6 — determine exit_reason.
        if points:
            last_date = points[-1].date
        elif trimmed:
            # Only entry row was in range; no subsequent rows.
            last_date = entry_date
        else:
            last_date = entry_date

        exit_reason: str | None
        if exit_date is not None and points and points[-1].date == exit_date:
            exit_reason = "exit_date"
        elif last_date == contract.expiration:
            exit_reason = "held_to_expiry"
        else:
            exit_reason = "contract_data_ended"

        # Step 7 — return PnLSeries.
        return PnLSeries(
            contract=contract,
            entry_date=entry_date,
            entry_price=entry_price,
            qty=qty,
            points=tuple(points),
            exit_reason=exit_reason,  # type: ignore[arg-type]
            notes=tuple(notes),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_mark(row: OptionDailyRow, mark_field: Literal["mid", "close"]) -> float | None:
    """Extract the mark from *row* according to *mark_field*.

    Returns ``None`` when the field is absent (``None``).  Callers treat
    ``None`` as "mark unavailable for this day".
    """
    if mark_field == "mid":
        return row.mid
    elif mark_field == "close":
        return row.close
    else:
        # Defensive: the Protocol's Literal["mid","close"] already constrains
        # callers, but we guard against dynamic dispatch with an unknown value.
        raise ValueError(
            f"Unknown mark_field '{mark_field}'; expected 'mid' or 'close'."
        )
