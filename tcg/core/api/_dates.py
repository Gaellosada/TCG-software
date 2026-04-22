"""Date-parsing helpers shared across API routers.

Keeps the ISO-date parse / error-message shape identical in every
router so frontend callers see a single canonical ``Invalid date
format: ...`` error.
"""

from __future__ import annotations

from datetime import date


def parse_iso_range(
    start: str | None, end: str | None
) -> tuple[date | None, date | None]:
    """Parse ``start``/``end`` as ISO dates, returning ``(None, None)`` for empty.

    Raises ``ValueError`` with a canonical ``"Invalid date format: ..."``
    message so callers can forward it verbatim to their preferred error
    channel (``error_response`` or ``raise ValidationError``).
    """
    try:
        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValueError(f"Invalid date format: {exc}") from exc
    return start_date, end_date
