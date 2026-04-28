"""Unit tests for Module 7 — ``tcg.engine.options.pnl.DefaultOptionsPnL``.

All tests use synthetic ``OptionContractSeries`` fixtures and an
``AsyncMock`` port — no Mongo dependency (guardrail #10).

Key invariants tested
---------------------
- Happy-path long: cumulative and daily P&L are correct over a 3-day series.
- Happy-path short: qty=-1 flips signs.
- Missing mid in middle: zero P&L on missing day; jump materialises on resume.
- Long gap (4 missing days) then resume: entire gap materialises on resume day.
- exit_reason: "exit_date", "held_to_expiry", "contract_data_ended".
- mark_field="close": uses close field when specified.
- Entry row missing: raises ValueError with helpful message.
- Entry mark is None: raises ValueError.
- Empty subsequent rows (no rows after entry): returns PnLSeries with empty points.
"""

from __future__ import annotations

import math
import pytest
from datetime import date
from typing import Literal
from unittest.mock import AsyncMock

from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    PnLSeries,
)
from tcg.engine.options.pnl.pnl import DefaultOptionsPnL


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_contract(
    expiration: date,
    collection: str = "OPT_SP_500",
    contract_id: str = "TEST_C_2024|M",
) -> OptionContractDoc:
    return OptionContractDoc(
        collection=collection,
        contract_id=contract_id,
        root_underlying="IND_SP_500",
        underlying_ref=None,
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle="M",
        strike=4500.0,
        type="C",
        contract_size=100.0,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def make_row(
    d: date,
    mid: float | None = None,
    close: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=d,
        open=None,
        high=None,
        low=None,
        close=close,
        bid=bid,
        ask=ask,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=mid,
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


def make_port(series: OptionContractSeries) -> AsyncMock:
    port = AsyncMock()
    port.get_contract = AsyncMock(return_value=series)
    return port


def make_series(
    contract: OptionContractDoc,
    rows: list[OptionDailyRow],
) -> OptionContractSeries:
    return OptionContractSeries(contract=contract, rows=tuple(rows))


# ---------------------------------------------------------------------------
# Happy-path: long position (qty=+1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_long() -> None:
    """3-day series, long, entry at day 1 mid=2.0, day 2 mid=2.1, day 3 mid=2.2."""
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    contract = make_contract(expiration=d3)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=2.1),
        make_row(d3, mid=2.2),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert isinstance(result, PnLSeries)
    assert result.entry_date == d1
    assert result.entry_price == 2.0
    assert result.qty == 1.0

    # Two points for d2 and d3 (d1 is entry, not a "subsequent" point)
    assert len(result.points) == 2

    p1 = result.points[0]
    assert p1.date == d2
    assert p1.mark == pytest.approx(2.1)
    assert p1.pnl_daily == pytest.approx(0.1)
    assert p1.pnl_cumulative == pytest.approx(0.1)

    p2 = result.points[1]
    assert p2.date == d3
    assert p2.mark == pytest.approx(2.2)
    assert p2.pnl_daily == pytest.approx(0.1)
    assert p2.pnl_cumulative == pytest.approx(0.2)

    # d3 == expiration → held_to_expiry
    assert result.exit_reason == "held_to_expiry"
    assert result.notes == ()


# ---------------------------------------------------------------------------
# Happy-path: short position (qty=-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_short() -> None:
    """Same 3-day series, qty=-1 → cumulatives are negated."""
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    contract = make_contract(expiration=d3)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=2.1),
        make_row(d3, mid=2.2),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=-1.0)

    assert result.points[0].pnl_daily == pytest.approx(-0.1)
    assert result.points[0].pnl_cumulative == pytest.approx(-0.1)
    assert result.points[1].pnl_daily == pytest.approx(-0.1)
    assert result.points[1].pnl_cumulative == pytest.approx(-0.2)
    assert result.exit_reason == "held_to_expiry"


# ---------------------------------------------------------------------------
# Missing mid in middle (day 2 has no mid)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_mid_in_middle() -> None:
    """Marks [2.0, None, 2.2]: cumulatives [0.0, 0.0, 0.2], dailys [0.0, 0.0, 0.2]."""
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    contract = make_contract(expiration=d3)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=None),
        make_row(d3, mid=2.2),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert len(result.points) == 2

    # d2: mark=None, pnl frozen
    p1 = result.points[0]
    assert p1.date == d2
    assert p1.mark is None
    assert p1.pnl_daily == pytest.approx(0.0)
    assert p1.pnl_cumulative == pytest.approx(0.0)

    # d3: mark resumes; jump = 2.2 - 2.0 (from last_known_mark)
    p2 = result.points[1]
    assert p2.date == d3
    assert p2.mark == pytest.approx(2.2)
    assert p2.pnl_daily == pytest.approx(0.2)
    assert p2.pnl_cumulative == pytest.approx(0.2)

    # Note appended for missing day
    assert len(result.notes) == 1
    assert "2024-01-03" in result.notes[0]
    assert "pnl_daily=0" in result.notes[0]


# ---------------------------------------------------------------------------
# Long gap (4 consecutive None mids) then resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_gap_then_resume() -> None:
    """Marks [2.0, None, None, None, 2.5], qty=1.

    Gap days: each pnl_daily=0, cumulative stays 0.0.
    Resume day (d5): pnl_daily = qty * (2.5 - 2.0) = 0.5; cumulative = 0.5.

    This tests the "long-gap materialises on resume day" documented behavior.
    """
    d1 = date(2024, 1, 2)
    d2 = date(2024, 1, 3)
    d3 = date(2024, 1, 4)
    d4 = date(2024, 1, 5)
    d5 = date(2024, 1, 8)  # Monday after a long weekend (realistic gap)
    contract = make_contract(expiration=d5)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=None),
        make_row(d3, mid=None),
        make_row(d4, mid=None),
        make_row(d5, mid=2.5),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert len(result.points) == 4

    # Three gap days: pnl frozen
    for i, gap_date in enumerate([d2, d3, d4]):
        p = result.points[i]
        assert p.date == gap_date
        assert p.mark is None
        assert p.pnl_daily == pytest.approx(0.0)
        assert p.pnl_cumulative == pytest.approx(0.0)

    # Resume day: entire 0.5 jump materialises here
    p_resume = result.points[3]
    assert p_resume.date == d5
    assert p_resume.mark == pytest.approx(2.5)
    assert p_resume.pnl_daily == pytest.approx(0.5)
    assert p_resume.pnl_cumulative == pytest.approx(0.5)

    # Three notes for gap days
    assert len(result.notes) == 3
    assert result.exit_reason == "held_to_expiry"


# ---------------------------------------------------------------------------
# exit_reason = "exit_date"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_reason_exit_date() -> None:
    """Provide exit_date within data range → exit_reason='exit_date'."""
    d1, d2, d3, d4 = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    )
    contract = make_contract(expiration=d4)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=2.1),
        make_row(d3, mid=2.2),
        make_row(d4, mid=2.3),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0, exit_date=d3)

    # Only d2 and d3 in points (d1 is entry, d4 excluded by exit_date)
    assert len(result.points) == 2
    assert result.points[-1].date == d3
    assert result.exit_reason == "exit_date"


# ---------------------------------------------------------------------------
# exit_reason = "contract_data_ended" — data ends before expiry, no exit_date
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_reason_contract_data_ended() -> None:
    """Data ends before expiration, no exit_date → contract_data_ended."""
    d1 = date(2024, 1, 2)
    d2 = date(2024, 1, 3)
    expiry = date(2024, 2, 16)  # Far in the future; data doesn't reach it
    contract = make_contract(expiration=expiry)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=2.1),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert len(result.points) == 1
    assert result.points[0].date == d2
    assert result.exit_reason == "contract_data_ended"


# ---------------------------------------------------------------------------
# exit_reason = "held_to_expiry" — last point is exactly expiration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_reason_held_to_expiry() -> None:
    """Last row date == contract.expiration → held_to_expiry."""
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    contract = make_contract(expiration=d2)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=1.5),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert result.exit_reason == "held_to_expiry"
    assert result.points[-1].date == d2
    assert result.points[-1].pnl_cumulative == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# mark_field="close"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_field_close() -> None:
    """When mark_field='close', close values are used instead of mid."""
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    contract = make_contract(expiration=d3)
    rows = [
        make_row(d1, mid=99.0, close=3.0),   # close differs from mid
        make_row(d2, mid=99.0, close=3.5),
        make_row(d3, mid=99.0, close=4.0),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(
        contract, entry_date=d1, qty=1.0, mark_field="close"
    )

    assert result.entry_price == pytest.approx(3.0)
    assert result.points[0].mark == pytest.approx(3.5)
    assert result.points[0].pnl_daily == pytest.approx(0.5)
    assert result.points[1].mark == pytest.approx(4.0)
    assert result.points[1].pnl_cumulative == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Entry row missing → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_row_missing_raises() -> None:
    """entry_date not present in series → ValueError with descriptive message."""
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    contract = make_contract(expiration=d2)
    rows = [
        make_row(d2, mid=2.0),  # only d2; d1 is missing
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    with pytest.raises(ValueError, match="Entry row missing"):
        await pnl_engine.compute(contract, entry_date=d1, qty=1.0)


# ---------------------------------------------------------------------------
# Entry mark is None → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_mark_none_raises() -> None:
    """Entry row exists but mid is None → ValueError."""
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    contract = make_contract(expiration=d2)
    rows = [
        make_row(d1, mid=None),  # entry row has no mid
        make_row(d2, mid=2.0),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    with pytest.raises(ValueError, match="entry_price is None"):
        await pnl_engine.compute(contract, entry_date=d1, qty=1.0)


# ---------------------------------------------------------------------------
# Empty subsequent rows (entry only — no rows after entry in range)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_subsequent_rows() -> None:
    """No rows after entry_date in series → empty points, contract_data_ended."""
    d1 = date(2024, 1, 2)
    expiry = date(2024, 2, 16)
    contract = make_contract(expiration=expiry)
    rows = [
        make_row(d1, mid=2.5),  # Only entry row; no subsequent data
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    assert result.entry_price == pytest.approx(2.5)
    assert result.points == ()
    assert result.exit_reason == "contract_data_ended"
    assert result.notes == ()


# ---------------------------------------------------------------------------
# Exit date exactly on expiration (edge: should be exit_date, not held_to_expiry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_date_on_expiry_prefers_exit_date() -> None:
    """exit_date == expiration → exit_reason='exit_date' (exit_date takes priority)."""
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    contract = make_contract(expiration=d2)
    rows = [
        make_row(d1, mid=2.0),
        make_row(d2, mid=2.5),
    ]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    # exit_date == expiration; exit_date is explicitly provided
    result = await pnl_engine.compute(contract, entry_date=d1, qty=1.0, exit_date=d2)

    assert result.exit_reason == "exit_date"


# ---------------------------------------------------------------------------
# Port is called with the correct arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_port_called_with_correct_args() -> None:
    """Verify get_contract is called with (contract.collection, contract.contract_id)."""
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    contract = make_contract(
        expiration=d2,
        collection="OPT_GOLD",
        contract_id="GOLD_C_2024|M",
    )
    rows = [make_row(d1, mid=3.0), make_row(d2, mid=3.5)]
    series = make_series(contract, rows)
    port = make_port(series)
    pnl_engine = DefaultOptionsPnL(port=port)

    await pnl_engine.compute(contract, entry_date=d1, qty=1.0)

    port.get_contract.assert_awaited_once_with("OPT_GOLD", "GOLD_C_2024|M")


# ---------------------------------------------------------------------------
# Floating-point precision (accumulation over 10 days)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cumulative_precision_over_many_days() -> None:
    """Cumulative P&L is computed correctly over 10 rows without drift."""
    from datetime import timedelta

    d0 = date(2024, 1, 2)
    marks = [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0]
    days = [d0 + timedelta(days=i) for i in range(len(marks))]
    expiry = days[-1]
    contract = make_contract(expiration=expiry)
    rows = [make_row(d, mid=m) for d, m in zip(days, marks)]
    series = make_series(contract, rows)
    pnl_engine = DefaultOptionsPnL(port=make_port(series))

    result = await pnl_engine.compute(contract, entry_date=days[0], qty=1.0)

    # 10 subsequent points (index 1..10)
    assert len(result.points) == 10
    # Final cumulative: qty * (3.0 - 2.0) = 1.0
    assert result.points[-1].pnl_cumulative == pytest.approx(1.0, abs=1e-9)
    assert result.exit_reason == "held_to_expiry"
