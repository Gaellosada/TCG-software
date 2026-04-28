"""Unit tests for the options router (Wave B4).

Mocks the MarketDataService + MongoOptionsDataReader stub via
``conftest.py``.  Uses ``httpx.AsyncClient`` with ``ASGITransport``,
mirroring ``tests/unit/test_api_continuous.py``.

Coverage:
- ``GET /api/options/roots`` happy path + 502 on data-access error.
- ``GET /api/options/chain`` happy path (stored Greeks); compute_missing
  path (computed Greeks); OPT_VIX gate (missing_forward_vix_curve);
  validation error (expiration_min > expiration_max); empty chain.
- ``GET /api/options/contract/{coll}/{id}`` happy path, 404 on missing,
  date filtering, compute_missing path including raw + wrapped fields.
- ``GET /api/options/select`` happy ByStrike, JSON-parse 400, OPT_VIX
  ByDelta cascade (missing_delta_no_compute → 422), no-chain → 422.
- ``GET /api/options/chain-snapshot`` happy path (smile points by
  expiration); 9-expirations 400; multi-expiration uses cache.
- Error envelope smoke: 502 envelope shape.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient

from tcg.types.errors import OptionsDataAccessError, OptionsContractNotFound
from tcg.types.options import OptionContractDoc, OptionContractSeries

from conftest import (  # type: ignore[import-not-found]
    StubOptionsReader,
    make_contract,
    make_root_info,
    make_row,
)


# ---------------------------------------------------------------------------
# /roots
# ---------------------------------------------------------------------------


async def test_roots_happy_path(client: AsyncClient, options_reader: StubOptionsReader):
    options_reader.list_roots_result = [
        make_root_info("OPT_SP_500"),
        make_root_info("OPT_VIX"),
    ]
    resp = await client.get("/api/options/roots")
    assert resp.status_code == 200
    body = resp.json()
    assert "roots" in body
    assert len(body["roots"]) == 2
    assert body["roots"][0]["collection"] == "OPT_SP_500"
    assert body["roots"][0]["strike_factor_verified"] is True
    assert body["roots"][0]["providers"] == ["IVOLATILITY"]


async def test_roots_data_access_error_502(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.list_roots_side_effect = OptionsDataAccessError(
        "Mongo timeout"
    )
    resp = await client.get("/api/options/roots")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_type"] == "options_data_access_error"
    assert "Mongo timeout" in body["message"]


# ---------------------------------------------------------------------------
# /expirations
# ---------------------------------------------------------------------------


async def test_expirations_happy_path(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """Returns the distinct expirations the reader surfaced, sorted ascending,
    serialised as ISO strings. Backs the chain / smile date pickers."""
    options_reader.list_expirations_result = [
        date(2024, 4, 19),
        date(2024, 5, 17),
        date(2024, 6, 21),
    ]
    resp = await client.get("/api/options/expirations", params={"root": "OPT_SP_500"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == "OPT_SP_500"
    assert body["expirations"] == ["2024-04-19", "2024-05-17", "2024-06-21"]


async def test_expirations_empty_collection_is_empty_list(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.list_expirations_result = []
    resp = await client.get("/api/options/expirations", params={"root": "OPT_BTC"})
    assert resp.status_code == 200
    assert resp.json() == {"root": "OPT_BTC", "expirations": []}


async def test_expirations_data_access_error_502(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.list_expirations_side_effect = OptionsDataAccessError(
        "Mongo down"
    )
    resp = await client.get("/api/options/expirations", params={"root": "OPT_SP_500"})
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_type"] == "options_data_access_error"


# ---------------------------------------------------------------------------
# /chain
# ---------------------------------------------------------------------------


async def test_chain_stored_greeks_happy_path(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.query_chain_result = [
        (make_contract(), make_row()),
    ]
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == "OPT_SP_500"
    assert body["date"] == "2024-03-15"
    assert body["underlying_price"]["source"] == "stored"
    assert body["underlying_price"]["value"] == pytest.approx(5117.94)
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["iv"]["source"] == "stored"
    assert row["delta"]["source"] == "stored"
    assert row["delta"]["value"] == pytest.approx(0.512)


async def test_chain_compute_missing_fills_greeks(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """When stored is None and compute_missing=true, source becomes computed."""
    row = make_row(
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
    )
    options_reader.query_chain_result = [(make_contract(), row)]
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
            "compute_missing": "true",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    row_out = body["rows"][0]
    # IV inverted from mid → source="computed".  All other Greeks
    # depend on IV, so they should be computed too.
    assert row_out["iv"]["source"] == "computed"
    assert row_out["iv"]["value"] is not None
    for greek in ("delta", "gamma", "theta", "vega"):
        assert row_out[greek]["source"] == "computed", greek


async def test_chain_opt_vix_blocked(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """OPT_VIX with compute_missing=true → all Greeks missing_forward_vix_curve."""
    contract = make_contract(
        collection="OPT_VIX",
        contract_id="VIX_C_15_20240419|M",
        strike=15.0,
        underlying_ref=None,
        root_underlying="IND_VIX",
    )
    row = make_row(
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
    )
    options_reader.query_chain_result = [(contract, row)]
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_VIX",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
            "compute_missing": "true",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    row_out = body["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        assert row_out[greek]["source"] == "missing", greek
        assert row_out[greek]["error_code"] == "missing_forward_vix_curve", greek


async def test_chain_validation_error_400(client: AsyncClient):
    """expiration_min > expiration_max → 400."""
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-06-30",
            "expiration_max": "2024-03-15",
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "options_validation_error"


async def test_chain_empty_returns_note(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """Empty chain → 200 with notes, underlying_price=missing, rows=[]."""
    options_reader.query_chain_result = []
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert len(body["notes"]) >= 1
    assert body["underlying_price"]["source"] == "missing"
    assert body["underlying_price"]["error_code"] == "missing_underlying_price"


async def test_chain_data_access_error_502(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.query_chain_side_effect = OptionsDataAccessError(
        "Mongo down"
    )
    resp = await client.get(
        "/api/options/chain",
        params={
            "root": "OPT_SP_500",
            "date": "2024-03-15",
            "type": "both",
            "expiration_min": "2024-03-15",
            "expiration_max": "2024-06-30",
        },
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_type"] == "options_data_access_error"
    assert "Mongo down" in body["message"]


# ---------------------------------------------------------------------------
# /contract/{coll}/{id}
# ---------------------------------------------------------------------------


async def test_contract_happy_path(
    client: AsyncClient, options_reader: StubOptionsReader
):
    contract = make_contract()
    rows = (
        make_row(row_date=date(2024, 3, 14)),
        make_row(row_date=date(2024, 3, 15)),
        make_row(row_date=date(2024, 3, 16)),
    )
    options_reader.get_contract_result = OptionContractSeries(
        contract=contract, rows=rows
    )
    resp = await client.get(
        "/api/options/contract/OPT_SP_500/SPX_C_5100_20240419%7CM",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract"]["contract_id"] == "SPX_C_5100_20240419|M"
    assert len(body["rows"]) == 3
    # Decision D: both *_stored fields and ComputeResult wrappers present.
    row0 = body["rows"][0]
    assert row0["delta_stored"] == pytest.approx(0.512)
    assert row0["delta"]["source"] == "stored"
    assert row0["delta"]["value"] == pytest.approx(0.512)


async def test_contract_not_found_404(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.get_contract_side_effect = OptionsContractNotFound(
        "no such contract"
    )
    resp = await client.get(
        "/api/options/contract/OPT_SP_500/UNKNOWN%7CM",
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_type"] == "options_contract_not_found"


async def test_contract_compute_missing_fills_greeks(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """When stored Greeks are None and compute_missing=true, the contract
    endpoint must invert IV from mid via BS76 and fill every Greek with
    source='computed'. Mirrors the chain-side test, but for /contract."""
    contract = make_contract()
    row = make_row(
        row_date=date(2024, 3, 15),
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
    )
    options_reader.get_contract_result = OptionContractSeries(
        contract=contract, rows=(row,)
    )
    resp = await client.get(
        "/api/options/contract/OPT_SP_500/SPX_C_5100_20240419%7CM",
        params={"compute_missing": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1
    row_out = body["rows"][0]
    assert row_out["iv"]["source"] == "computed"
    assert row_out["iv"]["value"] is not None
    for greek in ("delta", "gamma", "theta", "vega"):
        assert row_out[greek]["source"] == "computed", greek
        assert row_out[greek]["value"] is not None, greek


async def test_contract_compute_missing_false_keeps_missing(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """Without compute_missing=true, missing stored Greeks stay missing."""
    contract = make_contract()
    row = make_row(
        row_date=date(2024, 3, 15),
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
    )
    options_reader.get_contract_result = OptionContractSeries(
        contract=contract, rows=(row,)
    )
    resp = await client.get(
        "/api/options/contract/OPT_SP_500/SPX_C_5100_20240419%7CM",
    )
    assert resp.status_code == 200
    body = resp.json()
    row_out = body["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        assert row_out[greek]["source"] == "missing", greek


async def test_contract_date_filter(
    client: AsyncClient, options_reader: StubOptionsReader
):
    contract = make_contract()
    rows = (
        make_row(row_date=date(2024, 3, 14)),
        make_row(row_date=date(2024, 3, 15)),
        make_row(row_date=date(2024, 3, 16)),
    )
    options_reader.get_contract_result = OptionContractSeries(
        contract=contract, rows=rows
    )
    resp = await client.get(
        "/api/options/contract/OPT_SP_500/SPX_C_5100_20240419%7CM",
        params={"date_from": "2024-03-15", "date_to": "2024-03-15"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["date"] == "2024-03-15"


# ---------------------------------------------------------------------------
# /select
# ---------------------------------------------------------------------------


async def test_select_by_strike_happy_path(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """ByStrike with FixedDate maturity — straightforward selector flow."""
    options_reader.query_chain_result = [
        (make_contract(strike=5100.0), make_row()),
        (
            make_contract(
                contract_id="SPX_C_5200_20240419|M", strike=5200.0
            ),
            make_row(),
        ),
    ]

    import json

    payload = {
        "root": "OPT_SP_500",
        "date": "2024-03-15",
        "type": "C",
        "criterion": {"kind": "by_strike", "strike": 5100.0},
        "maturity": {"kind": "fixed", "date": "2024-04-19"},
        "compute_missing_for_delta_selection": False,
    }
    resp = await client.get(
        "/api/options/select", params={"q": json.dumps(payload)}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error_code"] is None
    assert body["contract"] is not None
    assert body["contract"]["strike"] == 5100.0


async def test_select_malformed_json_400(client: AsyncClient):
    resp = await client.get(
        "/api/options/select", params={"q": "{not-json}"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "options_validation_error"


async def test_select_no_chain_returns_422(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """Empty chain → no_chain_for_date → 422 OptionsSelectionError."""
    options_reader.query_chain_result = []

    import json

    payload = {
        "root": "OPT_SP_500",
        "date": "2024-03-15",
        "type": "C",
        "criterion": {"kind": "by_strike", "strike": 5100.0},
        "maturity": {"kind": "fixed", "date": "2024-04-19"},
        "compute_missing_for_delta_selection": False,
    }
    resp = await client.get(
        "/api/options/select", params={"q": json.dumps(payload)}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_type"] == "options_selection_error"


# ---------------------------------------------------------------------------
# /chain-snapshot
# ---------------------------------------------------------------------------


async def test_chain_snapshot_happy_path(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.query_chain_result = [
        (make_contract(strike=5000.0), make_row(iv_stored=0.16)),
        (
            make_contract(
                contract_id="SPX_C_5100_20240419|M", strike=5100.0
            ),
            make_row(iv_stored=0.155),
        ),
        (
            make_contract(
                contract_id="SPX_C_5200_20240419|M", strike=5200.0
            ),
            make_row(iv_stored=0.15),
        ),
    ]
    resp = await client.get(
        "/api/options/chain-snapshot",
        params=[
            ("root", "OPT_SP_500"),
            ("date", "2024-03-15"),
            ("type", "C"),
            ("expirations", "2024-04-19"),
            ("field", "iv"),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == "OPT_SP_500"
    assert len(body["series"]) == 1
    smile = body["series"][0]
    assert smile["expiration"] == "2024-04-19"
    assert len(smile["points"]) == 3
    # Smile points carry strike, K_over_S, and a ComputeResult value.
    p = smile["points"][0]
    assert "strike" in p
    assert "value" in p
    assert p["value"]["source"] == "stored"


async def test_chain_snapshot_smile_point_carries_expiration_cycle(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """Each SmilePoint exposes the contract's expirationCycle, so the
    frontend can populate the cycle dropdown from the unfiltered
    response."""
    options_reader.query_chain_result = [
        (make_contract(strike=5000.0, contract_id="SPX_C_5000_20240419|M"),
         make_row(iv_stored=0.16)),
    ]
    resp = await client.get(
        "/api/options/chain-snapshot",
        params=[
            ("root", "OPT_SP_500"),
            ("date", "2024-03-15"),
            ("type", "C"),
            ("expirations", "2024-04-19"),
            ("field", "iv"),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    point = body["series"][0]["points"][0]
    # make_contract() defaults expiration_cycle to "M".
    assert point["expiration_cycle"] == "M"


async def test_chain_snapshot_filters_by_expiration_cycle(
    client: AsyncClient, options_reader: StubOptionsReader
):
    """When expiration_cycle is supplied, only contracts of that cycle
    flow into the response. Cycle mismatches must be filtered out before
    the SmilePoint is built."""

    # Two contracts at the same strike, same expiration date, different
    # cycles. This is the SP_500 weekly-vs-monthly overlap that produces
    # duplicate strike markers on the smile.
    monthly_contract = make_contract(
        contract_id="SPX_C_5000_20240419|M",
        strike=5000.0,
    )
    # Override cycle by reconstructing the contract — make_contract
    # hard-codes "M".
    weekly_contract = OptionContractDoc(
        collection=monthly_contract.collection,
        contract_id="SPXW_C_5000_20240419|W",
        root_underlying=monthly_contract.root_underlying,
        underlying_ref=monthly_contract.underlying_ref,
        underlying_symbol=monthly_contract.underlying_symbol,
        expiration=monthly_contract.expiration,
        expiration_cycle="W",
        strike=monthly_contract.strike,
        type=monthly_contract.type,
        contract_size=monthly_contract.contract_size,
        currency=monthly_contract.currency,
        provider=monthly_contract.provider,
        strike_factor_verified=monthly_contract.strike_factor_verified,
    )
    options_reader.query_chain_result = [
        (monthly_contract, make_row(iv_stored=0.16)),
        (weekly_contract, make_row(iv_stored=0.18)),
    ]

    # Without the filter — both points present (current behaviour).
    resp_all = await client.get(
        "/api/options/chain-snapshot",
        params=[
            ("root", "OPT_SP_500"),
            ("date", "2024-03-15"),
            ("type", "C"),
            ("expirations", "2024-04-19"),
            ("field", "iv"),
        ],
    )
    assert resp_all.status_code == 200
    assert len(resp_all.json()["series"][0]["points"]) == 2

    # With expiration_cycle=M — only the monthly survives.
    resp_m = await client.get(
        "/api/options/chain-snapshot",
        params=[
            ("root", "OPT_SP_500"),
            ("date", "2024-03-15"),
            ("type", "C"),
            ("expirations", "2024-04-19"),
            ("field", "iv"),
            ("expiration_cycle", "M"),
        ],
    )
    assert resp_m.status_code == 200
    pts = resp_m.json()["series"][0]["points"]
    assert len(pts) == 1
    assert pts[0]["expiration_cycle"] == "M"


async def test_chain_snapshot_max_eight_expirations(client: AsyncClient):
    params: list[tuple[str, str]] = [
        ("root", "OPT_SP_500"),
        ("date", "2024-03-15"),
        ("type", "C"),
        ("field", "iv"),
    ]
    for i in range(9):
        params.append(("expirations", f"2024-0{(i % 9) + 1}-15"))
    resp = await client.get("/api/options/chain-snapshot", params=params)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "options_validation_error"


async def test_chain_snapshot_data_access_error_502(
    client: AsyncClient, options_reader: StubOptionsReader
):
    options_reader.query_chain_side_effect = OptionsDataAccessError(
        "Mongo timeout"
    )
    resp = await client.get(
        "/api/options/chain-snapshot",
        params=[
            ("root", "OPT_SP_500"),
            ("date", "2024-03-15"),
            ("type", "C"),
            ("expirations", "2024-04-19"),
        ],
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_type"] == "options_data_access_error"


# ---------------------------------------------------------------------------
# Sanity: app mounts the 5 paths
# ---------------------------------------------------------------------------


async def test_app_registers_six_options_paths(client: AsyncClient):
    """OpenAPI exposes the 6 options endpoints."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    options_paths = sorted(p for p in paths if p.startswith("/api/options"))
    assert options_paths == [
        "/api/options/chain",
        "/api/options/chain-snapshot",
        "/api/options/contract/{coll}/{contract_id}",
        "/api/options/expirations",
        "/api/options/roots",
        "/api/options/select",
    ]
