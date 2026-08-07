"""Live-dwh integration test for F3 — ``NDaysBeforeExpiry`` roll rule.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by
dwh reachability (``load_dwh_config`` / ``pool.connect()`` -> skip on
failure), mirroring ``tests/integration/data/options/test_sql_options_bulk_integration.py``.

No mocks anywhere in the exercised path (per strategy-repro-impl guardrails
"No mocks in the end-to-end integration tests — real API+engine path"):

- ``SqlOptionsDataReader`` (Module 1, real SQL against the dwh ``tcg_instruments``
  schema, read-only ``tcg_read`` role)
- ``DefaultMaturityResolver`` (Module 4, real)
- ``DefaultOptionsSelector`` (Module 3, real, wired to the reader above)
- ``DefaultOptionsRoller`` (Module 5 — the code under test)

Selects a real monthly-cycle VIX call (SPEC §5.5/§5.6's "30-day VIX call")
from the dwh OPT_VIX chain and asserts the roll fires exactly 2 TRADING days
before its real expiration — the SPEC's concrete case — and that
``next_contract`` rolls into the real next monthly VIX call.
"""

from __future__ import annotations

from datetime import date

import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._sql.options import SqlOptionsDataReader
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.roll.roller import DefaultOptionsRoller
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.options import ByStrike, NDaysBeforeExpiry, NearestToTarget

# A real monthly-cycle (expiration_cycle="M") VIX call expiring 2023-04-19
# (Wednesday). CME_TradeDate has no holiday in the surrounding week, so the
# 2-trading-days-before boundary (2023-04-17, Monday) is unambiguous and
# independently verifiable: valid_days('2023-04-10'..'2023-04-19') on
# CME_TradeDate = [4/10, 4/11, 4/12, 4/13, 4/14, 4/17, 4/18, 4/19].
_ROOT = "OPT_VIX"
_HELD_EXPIRATION = date(2023, 4, 19)
_QUERY_DATE = date(2023, 4, 17)  # a date the held contract is quoted on
_EXPECTED_TRIGGER = date(2023, 4, 17)  # 2 trading days before 4/19
_DAY_BEFORE_TRIGGER = date(2023, 4, 14)  # previous trading day (Friday)
_EXPECTED_NEXT_EXPIRATION = date(2023, 5, 17)  # real next monthly VIX call


@pytest.fixture
async def roller_and_held():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")

    reader = SqlOptionsDataReader(pool)
    rows = await reader.query_chain(
        root=_ROOT,
        date=_QUERY_DATE,
        type="C",
        expiration_min=_HELD_EXPIRATION,
        expiration_max=_HELD_EXPIRATION,
        expiration_cycle="M",
    )
    if not rows:
        await pool.close()
        pytest.skip(
            f"no {_ROOT} monthly call rows for expiration {_HELD_EXPIRATION} "
            f"on {_QUERY_DATE} — dwh data may have changed"
        )
    held, held_row = rows[0]
    assert held.expiration == _HELD_EXPIRATION  # sanity: dwh data unchanged

    selector = DefaultOptionsSelector(reader=reader, maturity_resolver=DefaultMaturityResolver())
    roller = DefaultOptionsRoller(selector=selector)
    try:
        yield roller, held, held_row
    finally:
        await pool.close()


@pytest.mark.integration
async def test_should_roll_fires_2_trading_days_before_real_vix_expiry(roller_and_held):
    roller, held, held_row = roller_and_held
    rule = NDaysBeforeExpiry(n=2)

    assert (
        roller.should_roll(held, held_row, as_of=_DAY_BEFORE_TRIGGER, rule=rule) is False
    )
    assert roller.should_roll(held, held_row, as_of=_EXPECTED_TRIGGER, rule=rule) is True
    assert (
        roller.should_roll(held, held_row, as_of=held.expiration, rule=rule) is True
    )


@pytest.mark.integration
async def test_next_contract_rolls_into_real_next_monthly_vix_call(roller_and_held):
    roller, held, _held_row = roller_and_held
    rule = NDaysBeforeExpiry(n=2)

    result = await roller.next_contract(
        held=held,
        as_of=_EXPECTED_TRIGGER,
        rule=rule,
        criterion_for_new=ByStrike(strike=held.strike),
        maturity_for_new=NearestToTarget(target_dte_days=30),
    )

    assert result.error_code is None, result
    assert result.new_contract is not None
    assert result.new_contract.expiration == _EXPECTED_NEXT_EXPIRATION
    assert result.new_contract.expiration > held.expiration
    assert result.new_contract.strike == held.strike
    assert result.roll_date == _EXPECTED_TRIGGER
    assert result.reason == "rolled_n_days_before_expiry"
