"""v2-native continuous options resolver (settlement-value based).

Lives in ``tcg.data`` on purpose: it must NOT depend on ``tcg.engine`` (the
import-linter forbids engine<->data), and it deliberately does NOT reuse the
greeks/pricing machinery in ``tcg.engine.options`` — v2 has no greeks
(``fact_greeks`` is empty). It builds a continuous stream by, per trade date,
selecting ONE contract from the front-expiration chain (by absolute strike or by
moneyness) and reading its daily settlement ``value``, rolling AtExpiry.

Pure orchestration + selection: reads come from :class:`SqlInstrumentReaderV2`,
the math is plain Python. ``criterion="delta"`` is rejected with a clean
``ValidationError`` (→ HTTP 400) because greeks are unavailable in v2.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date

from tcg.data._sql.instruments_v2 import SqlInstrumentReaderV2
from tcg.types.errors import ValidationError
from tcg.types.market import OptionsContinuousV2

# API option_type ("call"/"put") already matches the dwh ``contract.option_type``
# domain, so no mapping table is needed — validated at the boundary instead.
_VALID_OPTION_TYPES = ("call", "put")
_VALID_CRITERIA = ("strike", "moneyness")


def _front_close_by_date(
    future_rows: list[dict[str, object]],
) -> dict[int, float]:
    """Map each date → front future close (nearest expiration >= that date).

    ``future_rows`` are ``{ts_int, expiration_int, close}`` sorted by
    (ts, expiration). For each date, the first row (smallest expiration) whose
    expiration is >= the date is the front contract on that date.
    """
    out: dict[int, float] = {}
    for row in future_rows:
        ts_int = int(row["ts_int"])  # type: ignore[arg-type]
        exp = int(row["expiration_int"])  # type: ignore[arg-type]
        close = float(row["close"])  # type: ignore[arg-type]
        if exp < ts_int:
            continue  # already-expired contract cannot be the front on this date
        if ts_int not in out:
            out[ts_int] = close  # first (smallest expiration) wins → front
    return out


def _front_expiration(chain_exps: list[int], ts_int: int) -> int | None:
    """Nearest listed contract expiration >= ``ts_int`` (the active front).

    ``chain_exps`` is sorted ascending (distinct contract expirations). Returns
    ``None`` when ``ts_int`` lies beyond the last listed expiration. This is the
    calendar front — a function of the contract chain only, NOT of which
    settlements happen to exist today — so a settlement hole in the true front
    cannot make the roll jump to a later expiration and back.
    """
    idx = bisect_left(chain_exps, ts_int)
    return chain_exps[idx] if idx < len(chain_exps) else None


async def resolve_options_continuous_v2(
    reader: SqlInstrumentReaderV2,
    object_row: dict[str, object],
    *,
    criterion: str,
    target: float,
    option_type: str,
    start: date | None,
    end: date | None,
) -> OptionsContinuousV2:
    """Build a v2 continuous options settlement stream.

    Parameters
    ----------
    reader:
        The v2 SQL reader (read-only).
    object_row:
        The option object's row (``object_id``, ``kind``, ``underlying_object_id``).
    criterion:
        ``"strike"`` (absolute strike, ``target`` = strike) or ``"moneyness"``
        (``target`` = strike/spot ratio; spot = underlying future front close).
        ``"delta"`` raises ``ValidationError`` (greeks unavailable in v2).
    target:
        Selection target — absolute strike, or moneyness ratio.
    option_type:
        ``"call"`` or ``"put"``.
    start, end:
        Optional inclusive date window.

    AtExpiry roll: on each trade date the *active* expiration is the nearest
    contract expiration >= that date; when it advances, a roll is recorded.
    """
    if criterion == "delta":
        raise ValidationError(
            "Delta-based selection is unavailable in Database v2: the v2 "
            "warehouse has no greeks (fact_greeks is empty). Use criterion "
            "'strike' or 'moneyness'."
        )
    if criterion not in _VALID_CRITERIA:
        raise ValidationError(
            f"Invalid criterion {criterion!r}. Must be one of: "
            f"{', '.join(_VALID_CRITERIA)} (delta unavailable in v2)."
        )
    if option_type not in _VALID_OPTION_TYPES:
        raise ValidationError(
            f"Invalid option_type {option_type!r}. Must be 'call' or 'put'."
        )
    if criterion == "moneyness" and target <= 0:
        raise ValidationError("Moneyness target must be > 0 (strike/spot ratio).")
    if criterion == "strike" and target <= 0:
        raise ValidationError("Strike target must be > 0 (absolute strike).")

    object_id = int(object_row["object_id"])  # type: ignore[arg-type]

    settlements = await reader.fetch_option_settlements(
        object_id, option_type, start=start, end=end
    )

    # Tradeable expiration chain from the contract dimension (sorted ascending),
    # independent of settlement availability. The active (front) expiration for
    # each date is derived from this — NOT from that date's usable settlements —
    # so a data hole in the true front contract cannot emit a spurious /
    # non-monotonic roll.
    chain_exps = await reader.fetch_option_expirations(object_id, option_type)

    # spot map (moneyness only) from the underlying future's front close.
    front_close: dict[int, float] = {}
    if criterion == "moneyness":
        underlying = object_row.get("underlying_object_id")
        if underlying is None:
            raise ValidationError(
                "Moneyness selection needs an underlying future, but this "
                "option object has no underlying_object_id."
            )
        future_rows = await reader.fetch_future_front_closes(
            int(underlying), start=start, end=end
        )
        front_close = _front_close_by_date(future_rows)

    # Group settlements by trade date, keeping only usable ( > 0 ) settlements.
    by_date: dict[int, list[dict[str, object]]] = {}
    for row in settlements:
        val = row["value"]
        if val is None or float(val) <= 0.0:  # type: ignore[arg-type]
            continue  # false-zero / NULL settlement guard — dropped, not plotted
        strike = row["strike"]
        if strike is None:
            continue
        by_date.setdefault(int(row["ts_int"]), []).append(row)  # type: ignore[arg-type]

    dates_out: list[int] = []
    values_out: list[float] = []
    roll_dates: list[int] = []
    contracts_out: list[str] = []  # distinct, first-seen (count/summary only)
    contract_codes_out: list[str] = []  # per-date, 1:1 with dates_out
    seen_contracts: set[str] = set()
    prev_active_exp: int | None = None

    for ts_int in sorted(by_date):
        # Active (front) expiration on this date from the CONTRACT chain: the
        # nearest listed expiration >= date. Derived from the contract chain, not
        # from which settlements exist today, so a hole in the true front cannot
        # advance/rewind the roll.
        active_exp = _front_expiration(chain_exps, ts_int)
        if active_exp is None:
            continue  # date lies beyond the last listed expiration
        # Usable ( > 0 ) settlements in the active expiration on this date. When
        # the true front has a settlement hole today this is empty → drop the
        # date (do NOT roll to a later expiration).
        chain = [
            c
            for c in by_date[ts_int]
            if int(c["expiration_int"]) == active_exp  # type: ignore[arg-type]
        ]
        if not chain:
            continue

        # Target strike for this date.
        if criterion == "strike":
            target_strike = target
        else:  # moneyness
            spot = front_close.get(ts_int)
            if spot is None or spot <= 0:
                continue  # no spot on this date → cannot resolve moneyness; drop
            target_strike = target * spot

        # Nearest strike (tie-break: lower strike, then contract_id).
        chosen = min(
            chain,
            key=lambda c: (
                abs(float(c["strike"]) - target_strike),  # type: ignore[arg-type]
                float(c["strike"]),  # type: ignore[arg-type]
                int(c["contract_id"]),  # type: ignore[arg-type]
            ),
        )

        if prev_active_exp is not None and active_exp != prev_active_exp:
            roll_dates.append(ts_int)
        prev_active_exp = active_exp

        dates_out.append(ts_int)
        values_out.append(float(chosen["value"]))  # type: ignore[arg-type]
        code = str(chosen["contract_code"])
        contract_codes_out.append(code)  # per-date, aligned to dates_out
        if code not in seen_contracts:
            seen_contracts.add(code)
            contracts_out.append(code)

    return OptionsContinuousV2(
        object_id=object_id,
        criterion=criterion,
        option_type=option_type,
        dates=tuple(dates_out),
        values=tuple(values_out),
        roll_dates=tuple(roll_dates),
        contracts=tuple(contracts_out),
        contract_codes=tuple(contract_codes_out),
    )
