"""Tests for the ``rolls`` array on ``POST /api/options/stream``.

Covers CONTRACT.md §A.7:

1. Shape — every key in ``response["streams"]`` has a matching key in
   ``response["rolls"]``; each value is a list.
2. No roll when contract is stable (3-day window, same contract every
   day) → ``rolls[label] == []``.
3. Roll detected at ``contract_id`` transition — 3-day window where day
   3 picks a new contract → exactly one roll event with correct
   ``sold`` / ``bought`` metadata.
4. Skip roll on missing chain — day 2 has no chain → no roll emitted.
5. ``value`` field carries the plotted value (``values[i-1]`` for sold,
   ``values[i]`` for bought).
6. Both bulk paths produce structurally identical rolls (parametrize
   over strike-based and moneyness-based selection).
7. Legacy ``_resolve_one`` path produces rolls (no bulk reader wired).
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api._options_materialise import (
    _contract_meta,
    derive_rolls,
)
from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.options import router as options_router
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.errors import TCGError
from tcg.types.options import (
    ByMoneyness,
    ByStrike,
    NextThirdFriday,
    OptionContractDoc,
    OptionDailyRow,
    OptionRootInfo,
)


# ── Synthetic chain helpers (mirrored from test_stream_resolver.py) ────


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
    iv: float | None = 0.20,
    mid: float | None = 1.05,
    delta: float | None = 0.50,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
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


class FakeChainReader:
    """Chain reader keyed by date → list[(contract, row)]."""

    def __init__(self, chains_by_date):
        self._chains = chains_by_date
        self.calls: list[dict] = []

    async def query_chain(
        self,
        *,
        root,
        date,
        type,
        expiration_min,
        expiration_max,
        strike_min=None,
        strike_max=None,
        expiration_cycle=None,
    ):
        self.calls.append({"date": date, "expiration_cycle": expiration_cycle})
        chain = self._chains.get(date, [])
        return [
            (c, r)
            for (c, r) in chain
            if (c.type == type or type == "both")
            and expiration_min <= c.expiration <= expiration_max
            and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
        ]


class FakeBulkChainReader:
    """Bulk chain reader — identical filtering as FakeChainReader but
    multi-date in a single call."""

    def __init__(self, chains_by_date):
        self._chains = chains_by_date
        self.bulk_calls: list[dict] = []

    async def query_chain_bulk(
        self,
        *,
        root,
        dates,
        type,
        expiration_min,
        expiration_max,
        strike_min=None,
        strike_max=None,
        expiration_cycle=None,
    ):
        dates_list = list(dates)
        self.bulk_calls.append({"dates": dates_list})
        result: dict = {}
        for d in dates_list:
            chain = self._chains.get(d, [])
            filtered = [
                (c, r)
                for (c, r) in chain
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
            ]
            if filtered:
                result[d] = filtered
        return result


def _underlying_resolver_const(spot: float | None):
    async def _r(contract, on_date):
        return spot

    return _r


# ── Helper: derive_rolls unit tests ────────────────────────────────────


class TestDeriveRollsUnit:
    """Direct unit tests on the ``derive_rolls`` helper."""

    def test_stable_contract_no_rolls(self):
        c = _contract(strike=4500, expiration=date(2024, 4, 19))
        rolls = derive_rolls(
            ["2024-03-18", "2024-03-19", "2024-03-20"],
            [1.0, 1.1, 1.2],
            [c, c, c],
        )
        assert rolls == []

    def test_single_transition_emits_one_roll(self):
        c1 = _contract(strike=4500, expiration=date(2024, 4, 19))
        c2 = _contract(strike=4500, expiration=date(2024, 5, 17))
        rolls = derive_rolls(
            ["2024-04-17", "2024-04-18", "2024-04-19"],
            [1.0, 1.1, 5.0],
            [c1, c1, c2],
        )
        assert len(rolls) == 1
        ev = rolls[0]
        assert ev["date"] == "2024-04-19"
        # Values pulled from the plotted series.
        assert ev["sold"]["value"] == 1.1
        assert ev["bought"]["value"] == 5.0
        # Metadata correctness.
        assert ev["sold"]["contract_id"] == c1.contract_id
        assert ev["bought"]["contract_id"] == c2.contract_id
        assert ev["sold"]["expiration"] == "2024-04-19"
        assert ev["bought"]["expiration"] == "2024-05-17"

    def test_missing_contract_either_side_skips_roll(self):
        c1 = _contract(strike=4500, expiration=date(2024, 4, 19))
        c2 = _contract(strike=4500, expiration=date(2024, 5, 17))
        # Day 2 has None (no chain) — transition from c1 to None to c2 is
        # NOT a roll because None on either side aborts emission.
        rolls = derive_rolls(
            ["2024-04-17", "2024-04-18", "2024-04-19"],
            [1.0, None, 5.0],
            [c1, None, c2],
        )
        assert rolls == []

    def test_root_is_root_underlying_not_collection(self):
        """``root`` is ``OptionContractDoc.root_underlying``
        (``"IND_SP_500"``), NOT ``collection`` (``"OPT_SP_500"``)."""
        c1 = _contract(strike=4500, expiration=date(2024, 4, 19))
        c2 = _contract(strike=4500, expiration=date(2024, 5, 17))
        rolls = derive_rolls(
            ["2024-04-18", "2024-04-19"], [1.0, 5.0], [c1, c2]
        )
        assert rolls[0]["sold"]["root"] == "IND_SP_500"
        assert rolls[0]["bought"]["root"] == "IND_SP_500"

    def test_contract_meta_includes_required_fields(self):
        c = _contract(strike=4500, expiration=date(2024, 4, 19), type_="P")
        meta = _contract_meta(c, 12.5)
        assert set(meta.keys()) == {
            "contract_id",
            "root",
            "expiration",
            "strike",
            "type",
            "value",
        }
        assert meta["type"] == "P"
        assert meta["strike"] == 4500.0
        assert meta["value"] == 12.5

    def test_contract_meta_value_can_be_none(self):
        c = _contract(strike=4500, expiration=date(2024, 4, 19))
        meta = _contract_meta(c, None)
        assert meta["value"] is None


# ── Engine 3-tuple smoke tests ─────────────────────────────────────────


class TestEngineReturnsContracts:
    """The 3 engine code paths must return a parallel ``contracts``
    array on every selection success."""

    @pytest.mark.asyncio
    async def test_bulk_strike_path_returns_contracts(self):
        d1, d2, d3 = date(2024, 4, 17), date(2024, 4, 18), date(2024, 4, 19)
        exp_old = date(2024, 4, 19)  # third Friday of April
        exp_new = date(2024, 5, 17)  # third Friday of May
        c_old = _contract(strike=4500, expiration=exp_old)
        c_new = _contract(strike=4500, expiration=exp_new)
        chains = {
            d1: [(c_old, _row(row_date=d1, mid=1.0))],
            d2: [(c_old, _row(row_date=d2, mid=1.1))],
            d3: [(c_new, _row(row_date=d3, mid=5.0))],
        }
        reader = FakeChainReader(chains)
        bulk_reader = FakeBulkChainReader(chains)
        values, errors, contracts = await resolve_option_stream(
            dates=[d1, d2, d3],
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NextThirdFriday(offset_months=0),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=bulk_reader,
        )
        assert all(e is None for e in errors)
        assert len(contracts) == 3
        assert contracts[0].contract_id == c_old.contract_id
        assert contracts[1].contract_id == c_old.contract_id
        assert contracts[2].contract_id == c_new.contract_id
        # Bulk path was used.
        assert len(bulk_reader.bulk_calls) >= 1

    @pytest.mark.asyncio
    async def test_bulk_moneyness_path_returns_contracts(self):
        d1, d2 = date(2024, 4, 18), date(2024, 4, 19)
        exp_old = date(2024, 4, 19)
        exp_new = date(2024, 5, 17)
        c_old = _contract(strike=4500, expiration=exp_old)
        c_new = _contract(strike=4500, expiration=exp_new)
        chains = {
            d1: [(c_old, _row(row_date=d1, iv=0.20))],
            d2: [(c_new, _row(row_date=d2, iv=0.21))],
        }
        reader = FakeChainReader(chains)
        bulk_reader = FakeBulkChainReader(chains)
        values, errors, contracts = await resolve_option_stream(
            dates=[d1, d2],
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NextThirdFriday(offset_months=0),
            selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
            stream="iv",
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=_underlying_resolver_const(4500.0),
            bulk_chain_reader=bulk_reader,
        )
        assert errors == [None, None]
        assert contracts[0].contract_id == c_old.contract_id
        assert contracts[1].contract_id == c_new.contract_id

    @pytest.mark.asyncio
    async def test_legacy_per_date_path_returns_contracts(self):
        """No bulk_chain_reader → legacy ``_resolve_one`` path.  Captures
        the contract on each selection success."""
        d1, d2 = date(2024, 4, 18), date(2024, 4, 19)
        exp_old = date(2024, 4, 19)
        exp_new = date(2024, 5, 17)
        c_old = _contract(strike=4500, expiration=exp_old)
        c_new = _contract(strike=4500, expiration=exp_new)
        chains = {
            d1: [(c_old, _row(row_date=d1, mid=1.0))],
            d2: [(c_new, _row(row_date=d2, mid=5.0))],
        }
        reader = FakeChainReader(chains)
        values, errors, contracts = await resolve_option_stream(
            dates=[d1, d2],
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NextThirdFriday(offset_months=0),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            # No bulk_chain_reader on purpose — exercises the legacy path.
        )
        assert errors == [None, None]
        assert contracts[0].contract_id == c_old.contract_id
        assert contracts[1].contract_id == c_new.contract_id

    @pytest.mark.asyncio
    async def test_missing_chain_yields_none_contract(self):
        """When a date has no chain match (e.g. expired), the parallel
        contract entry is None (so downstream rolls are skipped)."""
        # NextThirdFriday(offset_months=0) on these dates resolves to
        # the April third-Friday expiration (2024-04-19).
        d1, d2 = date(2024, 4, 17), date(2024, 4, 18)
        exp = date(2024, 4, 19)
        c1 = _contract(strike=4500, expiration=exp)
        chains = {
            d1: [(c1, _row(row_date=d1, mid=1.0))],
            # d2: no chain — selection will fail.
        }
        reader = FakeChainReader(chains)
        bulk_reader = FakeBulkChainReader(chains)
        values, errors, contracts = await resolve_option_stream(
            dates=[d1, d2],
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NextThirdFriday(offset_months=0),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=bulk_reader,
        )
        assert contracts[0] is not None
        assert contracts[1] is None
        assert errors[1] == "no_chain_for_date"


# ── Endpoint integration: ``rolls`` in response ────────────────────────


_ROOT_WITH_GREEKS = OptionRootInfo(
    collection="OPT_SP_500",
    name="SP 500",
    has_greeks=True,
    providers=("IVOLATILITY",),
    expiration_first=date(2005, 1, 21),
    expiration_last=date(2027, 12, 19),
    doc_count_estimated=1234567,
    strike_factor_verified=True,
    last_trade_date=None,
)


def _stream_entry(label: str = "my_mid") -> dict:
    return {
        "ref": {
            "type": "option_stream",
            "collection": "OPT_SP_500",
            "option_type": "C",
            "cycle": None,
            "maturity": {"kind": "next_third_friday", "offset_months": 0},
            "selection": {"kind": "by_strike", "strike": 4500.0},
            "stream": "mid",
        },
        "label": label,
    }


def _request_body(streams, start="2024-04-17", end="2024-04-19"):
    return {"streams": streams, "start": start, "end": end}


@pytest.fixture
def app_with_materialise_stub(monkeypatch):
    """FastAPI app where ``materialise_option_streams`` is stubbed to
    return a deterministic 4-tuple — including a synthetic ``contracts``
    array — so we drive the endpoint's roll derivation directly."""
    # The contracts list is captured per-test via the closure on
    # ``contracts_factory``.
    state: dict = {}

    async def fake_materialise(
        refs_with_labels, *, svc, start_date, end_date, progress_callback=None
    ):
        labels = [label for label, _ref in refs_with_labels]
        result: dict = {}
        for label in labels:
            dates_arr = np.array(
                [20240417, 20240418, 20240419], dtype=np.int64
            )
            values = np.array(state.get("values", [1.0, 1.1, 5.0]), dtype=np.float64)
            diagnostics = state.get("diagnostics", [None, None, None])
            contracts = state.get("contracts", [None, None, None])
            result[label] = (dates_arr, values, diagnostics, contracts)
        return result

    monkeypatch.setattr(
        "tcg.core.api._options_materialise.materialise_option_streams",
        fake_materialise,
    )

    svc = MagicMock()
    svc.list_option_roots = AsyncMock(return_value=[_ROOT_WITH_GREEKS])

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(options_router)
    app.state.market_data = svc
    return app, state


@pytest.fixture
async def stub_client(app_with_materialise_stub):
    app, state = app_with_materialise_stub
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, state


@pytest.mark.asyncio
class TestEndpointRollsShape:
    """End-to-end: ``rolls`` is always present in the response,
    parallel to ``streams``."""

    async def test_rolls_key_always_present(self, stub_client):
        client, state = stub_client
        # Default state: contracts is [None, None, None] — no rolls.
        body = _request_body([_stream_entry("my_mid")])
        resp = await client.post("/api/options/stream", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "rolls" in data
        assert "my_mid" in data["rolls"]
        assert isinstance(data["rolls"]["my_mid"], list)

    async def test_rolls_parallel_to_streams(self, stub_client):
        """Every key in ``streams`` has a key in ``rolls``."""
        client, state = stub_client
        body = _request_body(
            [_stream_entry("a"), _stream_entry("b"), _stream_entry("c")]
        )
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        assert set(data["rolls"].keys()) == set(data["streams"].keys())
        for label in data["streams"]:
            assert isinstance(data["rolls"][label], list)

    async def test_no_roll_when_contract_stable(self, stub_client):
        """3 days, same contract every day → empty rolls list."""
        client, state = stub_client
        c = _contract(strike=4500, expiration=date(2024, 5, 17))
        state["contracts"] = [c, c, c]
        state["values"] = [1.0, 1.1, 1.2]
        body = _request_body([_stream_entry("my_mid")])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        assert data["rolls"]["my_mid"] == []

    async def test_roll_detected_at_transition(self, stub_client):
        """3 days, contract changes on day 3 → exactly one roll event."""
        client, state = stub_client
        c_old = _contract(strike=4500, expiration=date(2024, 4, 19))
        c_new = _contract(strike=4500, expiration=date(2024, 5, 17))
        state["contracts"] = [c_old, c_old, c_new]
        state["values"] = [1.0, 1.1, 5.0]
        body = _request_body([_stream_entry("my_mid")])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        rolls = data["rolls"]["my_mid"]
        assert len(rolls) == 1
        ev = rolls[0]
        assert ev["date"] == "2024-04-19"
        # ``sold`` is the OLD contract; ``bought`` is the NEW.
        assert ev["sold"]["contract_id"] == c_old.contract_id
        assert ev["bought"]["contract_id"] == c_new.contract_id
        # Value field carries plotted series at i-1 and i.
        assert ev["sold"]["value"] == 1.1
        assert ev["bought"]["value"] == 5.0
        # ``root`` is root_underlying, NOT collection.
        assert ev["sold"]["root"] == "IND_SP_500"
        assert ev["bought"]["root"] == "IND_SP_500"
        # Required fields all present.
        for side in (ev["sold"], ev["bought"]):
            assert set(side.keys()) == {
                "contract_id",
                "root",
                "expiration",
                "strike",
                "type",
                "value",
            }

    async def test_skip_roll_on_missing_chain(self, stub_client):
        """Day 2 has no contract (None) → no roll emitted on day 2 nor
        day 3 (day 3 transitions from None which is not a roll)."""
        client, state = stub_client
        c_old = _contract(strike=4500, expiration=date(2024, 4, 19))
        c_new = _contract(strike=4500, expiration=date(2024, 5, 17))
        state["contracts"] = [c_old, None, c_new]
        state["values"] = [1.0, np.nan, 5.0]
        state["diagnostics"] = [None, "no_chain_for_date", None]
        body = _request_body([_stream_entry("my_mid")])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        assert data["rolls"]["my_mid"] == []

    async def test_value_field_carries_plotted_values(self, stub_client):
        """CONTRACT §A.7 item 5: ``sold.value == values[i-1]`` and
        ``bought.value == values[i]``."""
        client, state = stub_client
        c_old = _contract(strike=4500, expiration=date(2024, 4, 19))
        c_new = _contract(strike=4510, expiration=date(2024, 5, 17))
        state["contracts"] = [c_old, c_old, c_new]
        state["values"] = [12.35, 13.50, 18.10]
        body = _request_body([_stream_entry("my_mid")])
        resp = await client.post("/api/options/stream", json=body)
        data = resp.json()
        rolls = data["rolls"]["my_mid"]
        assert len(rolls) == 1
        # i = 2 ; values[i-1] = 13.50 ; values[i] = 18.10
        assert rolls[0]["sold"]["value"] == pytest.approx(13.50)
        assert rolls[0]["bought"]["value"] == pytest.approx(18.10)


# ── Cross-path symmetry: bulk-strike vs bulk-moneyness vs legacy ───────


@pytest.mark.asyncio
class TestBulkAndLegacyPathSymmetry:
    """CONTRACT §A.7 items 6 & 7: both bulk paths AND the legacy path
    produce structurally identical roll output for an equivalent
    contract-transition scenario."""

    def _expected_roll_shape(self, ev):
        for side in ("sold", "bought"):
            assert set(ev[side].keys()) == {
                "contract_id",
                "root",
                "expiration",
                "strike",
                "type",
                "value",
            }

    async def _drive(self, *, selection, use_bulk: bool):
        d1, d2 = date(2024, 4, 18), date(2024, 4, 19)
        c_old = _contract(strike=4500, expiration=date(2024, 4, 19))
        c_new = _contract(strike=4500, expiration=date(2024, 5, 17))
        # Use mid for both selection variants so the value units match
        # across paths.
        chains = {
            d1: [(c_old, _row(row_date=d1, mid=1.1))],
            d2: [(c_new, _row(row_date=d2, mid=5.0))],
        }
        reader = FakeChainReader(chains)
        bulk_reader = FakeBulkChainReader(chains) if use_bulk else None
        ul_resolver = (
            _underlying_resolver_const(4500.0)
            if isinstance(selection, ByMoneyness)
            else None
        )
        values, _errors, contracts = await resolve_option_stream(
            dates=[d1, d2],
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NextThirdFriday(offset_months=0),
            selection=selection,
            stream="mid",
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=ul_resolver,
            bulk_chain_reader=bulk_reader,
        )
        # Convert NaN → None for parity with ``nan_safe_floats`` at the
        # API boundary.
        plotted = [
            float(v) if not np.isnan(v) else None for v in values.tolist()
        ]
        return derive_rolls(
            [d1.isoformat(), d2.isoformat()],
            plotted,
            contracts,
        )

    async def test_bulk_strike_emits_roll(self):
        rolls = await self._drive(
            selection=ByStrike(strike=4500.0), use_bulk=True
        )
        assert len(rolls) == 1
        self._expected_roll_shape(rolls[0])
        assert rolls[0]["sold"]["value"] == pytest.approx(1.1)
        assert rolls[0]["bought"]["value"] == pytest.approx(5.0)

    async def test_bulk_moneyness_emits_roll(self):
        rolls = await self._drive(
            selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
            use_bulk=True,
        )
        assert len(rolls) == 1
        self._expected_roll_shape(rolls[0])
        assert rolls[0]["sold"]["value"] == pytest.approx(1.1)
        assert rolls[0]["bought"]["value"] == pytest.approx(5.0)

    async def test_legacy_path_emits_roll(self):
        rolls = await self._drive(
            selection=ByStrike(strike=4500.0), use_bulk=False
        )
        assert len(rolls) == 1
        self._expected_roll_shape(rolls[0])
        assert rolls[0]["sold"]["value"] == pytest.approx(1.1)
        assert rolls[0]["bought"]["value"] == pytest.approx(5.0)

    async def test_all_three_paths_produce_identical_structure(self):
        """All three paths produce the same number of rolls and the same
        keys on each side."""
        rolls_a = await self._drive(
            selection=ByStrike(strike=4500.0), use_bulk=True
        )
        rolls_b = await self._drive(
            selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
            use_bulk=True,
        )
        rolls_c = await self._drive(
            selection=ByStrike(strike=4500.0), use_bulk=False
        )
        assert len(rolls_a) == len(rolls_b) == len(rolls_c) == 1
        for r in (rolls_a[0], rolls_b[0], rolls_c[0]):
            assert set(r.keys()) == {"date", "sold", "bought"}
            assert set(r["sold"].keys()) == set(r["bought"].keys())
