"""Pure matching helpers for Module 3 — selection.

These are pure functions over already-filtered rows.  They do NOT call
Module 1, Module 2, or Module 4.  They are unit-testable in isolation.

Conventions
-----------
- ``rows`` is always a list of ``(OptionContractDoc, OptionDailyRow)``
  tuples representing the contract chain on a single date.
- Tie-break: when two rows are equidistant from the target, **the one
  with the lower strike wins**.  This matches the legacy v2 selection
  behavior and keeps results deterministic.  Asserted in tests.
- Float-strike equality uses an absolute tolerance of ``1e-9`` to absorb
  Mongo float-encoding fuzz.
"""

from __future__ import annotations

from typing import Iterable

from tcg.types.options import (
    OptionContractDoc,
    OptionDailyRow,
    SelectionResult,
)

# Float-equality tolerance for ByStrike (absorbs storage fuzz).
_STRIKE_EQ_ABS_TOL: float = 1e-9


def _no_match(error_code: str, diagnostic: str) -> SelectionResult:
    """Build a no-match ``SelectionResult`` with no contract and no value."""
    return SelectionResult(
        contract=None,
        matched_value=None,
        error_code=error_code,
        diagnostic=diagnostic,
    )


def _match(contract: OptionContractDoc, value: float) -> SelectionResult:
    """Build a successful ``SelectionResult`` with no error_code."""
    return SelectionResult(
        contract=contract,
        matched_value=float(value),
        error_code=None,
        diagnostic=None,
    )


def match_by_strike(
    rows: Iterable[tuple[OptionContractDoc, OptionDailyRow]],
    K: float,
) -> SelectionResult:
    """Return the row whose ``contract.strike`` exactly equals *K*.

    Float equality uses absolute tolerance ``1e-9``.  When multiple rows
    match (defensive — should not happen per chain shape) the one with
    the lowest strike (i.e. the first in sorted order) wins.

    Returns ``error_code="strike_not_in_chain"`` on no match.
    """
    matches: list[tuple[OptionContractDoc, OptionDailyRow]] = [
        (c, r) for (c, r) in rows if abs(c.strike - K) < _STRIKE_EQ_ABS_TOL
    ]
    if not matches:
        return _no_match(
            "strike_not_in_chain",
            f"strike {K} not present in chain",
        )
    matches.sort(key=lambda cr: cr[0].strike)
    contract = matches[0][0]
    return _match(contract, contract.strike)


def match_by_delta(
    rows: Iterable[tuple[OptionContractDoc, OptionDailyRow]],
    deltas: list[float | None],
    target: float,
    tolerance: float,
    strict: bool,
    *,
    chain_size: int,
) -> SelectionResult:
    """Match the row whose delta is closest to *target*.

    Parameters
    ----------
    rows:
        Iterable of ``(contract, row)`` pairs aligned with *deltas*.
    deltas:
        One ``float | None`` per row — pre-resolved (stored-only OR
        stored-with-Module-2-fallback).  ``None`` means "no usable delta".
    target:
        The signed target delta.
    tolerance:
        Absolute tolerance band on ``|delta - target|``.
    strict:
        If True, return ``error_code="no_match_within_tolerance"`` when
        the closest delta exceeds ``tolerance``.  If False, return the
        closest row anyway with ``matched_value`` set (no error).
    chain_size:
        Total number of rows in the chain *before* the missing-delta
        filter (used for diagnostics).

    Returns
    -------
    SelectionResult
        See semantics above.  When all deltas are None, returns
        ``error_code="missing_delta_no_compute"``.
    """
    rows_list = list(rows)
    if len(rows_list) != len(deltas):  # pragma: no cover (defensive)
        raise ValueError(
            f"match_by_delta: rows ({len(rows_list)}) and deltas "
            f"({len(deltas)}) have different lengths"
        )

    usable: list[tuple[OptionContractDoc, OptionDailyRow, float]] = [
        (c, r, float(d))
        for (c, r), d in zip(rows_list, deltas)
        if d is not None
    ]

    if not usable:
        n_none = sum(1 for d in deltas if d is None)
        return _no_match(
            "missing_delta_no_compute",
            (
                f"chain has {chain_size} rows; {n_none} have no usable "
                f"delta (stored=None and compute opt-in not satisfied)"
            ),
        )

    # Sort by absolute distance to target, tie-break: lower strike wins.
    usable.sort(key=lambda crd: (abs(crd[2] - target), crd[0].strike))
    best_contract, _best_row, best_delta = usable[0]
    distance = abs(best_delta - target)

    if strict and distance > tolerance:
        return _no_match(
            "no_match_within_tolerance",
            (
                f"closest delta {best_delta:.6f} (strike {best_contract.strike}) "
                f"is {distance:.6f} from target {target} > tolerance {tolerance}"
            ),
        )

    return _match(best_contract, best_delta)


def match_by_moneyness(
    rows: Iterable[tuple[OptionContractDoc, OptionDailyRow]],
    target_K_over_S: float,
    tolerance: float,
    underlying_price: float,
) -> SelectionResult:
    """Match the row whose ``strike / underlying_price`` is closest to target.

    Parameters
    ----------
    rows:
        Iterable of ``(contract, row)`` pairs.
    target_K_over_S:
        Target K/S ratio (e.g. ``1.02`` for 2% OTM call).
    tolerance:
        Absolute tolerance band on ``|K/S - target|``.  ``ByMoneyness`` has
        no ``strict`` flag in spec §3.3 — but the spec language says "return
        error if min > tolerance and strict=True"; ``ByMoneyness`` has no
        ``strict`` field, so we always return the closest (no error on miss).
    underlying_price:
        The joined underlying price.  Caller is responsible for the join;
        Module 3 surfaces ``error_code="missing_underlying_price"`` upstream
        when ``None``.

    Returns
    -------
    SelectionResult
        Tie-break: lower strike wins.
    """
    rows_list = list(rows)
    if not rows_list:
        return _no_match(
            "no_chain_for_date",
            "chain is empty (no rows passed to match_by_moneyness)",
        )
    if underlying_price <= 0:  # pragma: no cover (defensive)
        return _no_match(
            "missing_underlying_price",
            f"underlying_price={underlying_price} is non-positive",
        )

    scored = [
        (c, r, c.strike / underlying_price)
        for (c, r) in rows_list
    ]
    scored.sort(key=lambda crk: (abs(crk[2] - target_K_over_S), crk[0].strike))
    best_contract, _best_row, best_ks = scored[0]
    # Per spec §3.3: ByMoneyness has no "strict" — we don't error on
    # tolerance miss; we surface the closest with matched_value=K/S.
    # (tolerance retained on the dataclass for forward compat / API doc.)
    _ = tolerance  # noqa: F841 — accepted for API symmetry; not used to gate
    return _match(best_contract, best_ks)
