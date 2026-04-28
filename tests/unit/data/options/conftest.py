"""Synthetic Mongo-doc factories for the OPT_* unit tests.

The fixtures here mimic the structure documented in
``DB_SCHEMA_FINDINGS.md`` §3 closely enough that ``_doc_to_dto`` and
``reader`` can exercise every translation rule without touching Mongo.

The shapes are minimal; we omit fields the reader does not consult.
"""

from __future__ import annotations

from typing import Any

import pytest


def _bar(
    date: int,
    *,
    bid: float | None = None,
    ask: float | None = None,
    close: float | None = 0.0,
    volume: float | None = 0.0,
    open_interest: float | None = 0.0,
    bid_size: float | None = None,
    ask_size: float | None = None,
    open: float | None = 0.0,
    high: float | None = 0.0,
    low: float | None = 0.0,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "date": date,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "openInterest": open_interest,
    }
    if bid is not None:
        out["bid"] = bid
    if ask is not None:
        out["ask"] = ask
    if bid_size is not None:
        out["bidSize"] = bid_size
    if ask_size is not None:
        out["askSize"] = ask_size
    return out


def _greek_full(
    date: int,
    *,
    iv: float = 0.20,
    delta: float = 0.50,
    gamma: float = 0.01,
    theta: float = -0.05,
    vega: float = 0.10,
    underlying_price: float = 100.0,
) -> dict[str, Any]:
    """Full INTERNAL-style Greek entry — includes the fields we ignore."""
    return {
        "date": date,
        "underlyingPrice": underlying_price,
        "atTheMoney": 0.5,        # ignored
        "moneyness": -0.01,       # ignored
        "daysToExpiry": 30,        # ignored
        "impliedVolatility": iv,
        "delta": delta,
        "theta": theta,
        "gamma": gamma,
        "vega": vega,
    }


def _greek_sparse(date: int, *, iv: float = 0.20) -> dict[str, Any]:
    """IVOLATILITY-style sparse early entry: IV + theta/gamma/vega only."""
    return {
        "date": date,
        "impliedVolatility": iv,
        "theta": 0.0,
        "gamma": 0.0,
        "vega": 0.0,
    }


@pytest.fixture
def make_bar():
    return _bar


@pytest.fixture
def make_greek_full():
    return _greek_full


@pytest.fixture
def make_greek_sparse():
    return _greek_sparse


@pytest.fixture
def sp500_doc(make_bar, make_greek_full):
    """A canonical OPT_SP_500 doc with one fully populated trading day."""
    return {
        "_id": {
            "internalSymbol": "OPT_FUT_SP_500_EMINI_20240315_5000_C",
            "expirationCycle": "M",
        },
        "expiration": 20240315,
        "strike": 5000.0,
        "type": "C",
        "underlyingSymbol": "ES",
        "underlying": "FUT_SP_500_EMINI_20240315",
        "rootUnderlying": "IND_SP_500",
        "code": "OPT_FUT_SP_500_EMINI",
        "contractSize": 50.0,
        "currency": "USD",
        "eodDatas": {
            "IVOLATILITY": [
                make_bar(20240301, bid=2.0, ask=2.1),
                make_bar(20240302, bid=None, ask=2.1),
                make_bar(20240303, bid=0.0, ask=0.0),
            ],
        },
        "eodGreeks": {
            "IVOLATILITY": [
                make_greek_full(20240301),
            ],
        },
    }


@pytest.fixture
def vix_doc(make_bar):
    """OPT_VIX with mixed-case ``type`` and no eodGreeks (DB §6)."""
    return {
        "_id": {
            "internalSymbol": "OPT_VIX_W3_20240320_15_p",
            "expirationCycle": "W",
        },
        "expiration": 20240320,
        "strike": 15.0,
        "type": "p",  # lower-case — must normalize to "P"
        "underlyingSymbol": "VIX",
        "rootUnderlying": "IND_VIX",
        "code": "OPT_VIX",
        "currency": "USD",
        "eodDatas": {
            "CBOE": [make_bar(20240315, bid=0.5, ask=0.6)],
        },
    }


@pytest.fixture
def btc_doc(make_bar, make_greek_full):
    """OPT_BTC with INTERNAL provider and full Greeks."""
    return {
        "_id": {
            "internalSymbol": "OPT_BTC_USD_20240329_60000_C",
            "expirationCycle": "D",
        },
        "expiration": 20240329,
        "strike": 60000.0,
        "type": "C",
        "rootUnderlying": "BTC",
        "code": "OPT_BTC_USD",
        "eodDatas": {
            "INTERNAL": [make_bar(20240320, bid=1500.0, ask=1600.0)],
        },
        "eodGreeks": {
            "INTERNAL": [make_greek_full(20240320, underlying_price=58000.0)],
        },
    }


@pytest.fixture
def eth_doc_with_internal(make_bar):
    """OPT_ETH with eodDatas under INTERNAL only (Decision E scan)."""
    return {
        "_id": {
            "internalSymbol": "OPT_ETH_USD_20240329_3000_P",
            "expirationCycle": "D",
        },
        "expiration": 20240329,
        "strike": 3000.0,
        "type": "P",
        "rootUnderlying": "ETH",
        "code": "OPT_ETH_USD",
        "eodDatas": {
            "INTERNAL": [make_bar(20240320, bid=50.0, ask=52.0)],
        },
    }


@pytest.fixture
def eth_doc_with_deribit(make_bar):
    """OPT_ETH with both DERIBIT and INTERNAL — DERIBIT wins by priority."""
    return {
        "_id": {
            "internalSymbol": "OPT_ETH_USD_20240329_3000_C",
            "expirationCycle": "D",
        },
        "expiration": 20240329,
        "strike": 3000.0,
        "type": "C",
        "rootUnderlying": "ETH",
        "eodDatas": {
            "DERIBIT": [make_bar(20240320, bid=80.0, ask=82.0)],
            "INTERNAL": [make_bar(20240320, bid=70.0, ask=72.0)],
        },
    }


@pytest.fixture
def eth_doc_empty():
    """OPT_ETH with eodDatas dict but every provider is empty."""
    return {
        "_id": {
            "internalSymbol": "OPT_ETH_USD_20240329_3000_C",
            "expirationCycle": "D",
        },
        "expiration": 20240329,
        "strike": 3000.0,
        "type": "C",
        "rootUnderlying": "ETH",
        "eodDatas": {"DERIBIT": [], "INTERNAL": []},
    }


@pytest.fixture
def t_note_doc(make_bar, make_greek_full):
    """OPT_T_NOTE_10_Y — strike_factor_verified must come back False."""
    return {
        "_id": {
            "internalSymbol": "OPT_FUT_T_NOTE_10_Y_20240621_120_C",
            "expirationCycle": "M",
        },
        "expiration": 20240621,
        "strike": 120.0,
        "type": "C",
        "underlyingSymbol": "TY",
        "underlying": "FUT_T_NOTE_10_Y_20240621",
        "rootUnderlying": "T_NOTE_10_Y",
        "eodDatas": {"IVOLATILITY": [make_bar(20240315, bid=0.4, ask=0.45)]},
        "eodGreeks": {"IVOLATILITY": [make_greek_full(20240315)]},
    }
