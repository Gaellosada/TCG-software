"""Asset-type × indicator compatibility matrix — backend canonical guard.

For every default indicator (auto-discovered from the FE registry on disk)
crossed with every canonical asset-type literal, POST a minimal request to
``/api/indicators/compute`` carrying both ``asset_type`` and
``compatible_asset_types`` and verify:

* Negative pairing (``asset_type`` ∉ ``compatible_asset_types``) returns
  HTTP 422 with body ``{"error_code": "INDICATOR_INCOMPATIBLE_ASSET",
  "asset_type": ..., "accepted_asset_types": [...]}`` and — when the test
  passes ``indicator_id`` — echoes that id.
* Positive pairing (``asset_type`` ∈ ``compatible_asset_types``) does NOT
  return 422 with INDICATOR_INCOMPATIBLE_ASSET. Any other outcome is
  acceptable here — the test only proves the compat layer didn't fire.

Discovery is glob+regex over ``frontend/src/pages/Indicators/defaults/*.js``
mirroring ``tests/engine/test_default_indicators_library.py``. New default
files (Wave 2c's option indicators) are picked up automatically.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.indicators import router as indicators_router
from tcg.core.indicators.asset_types import ASSET_TYPES
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = (
    REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
)


# Match e.g. ``compatibleAssetTypes: ['index', 'equity'],`` — single OR
# double quotes, any whitespace. We capture the raw bracketed list and
# parse the literals with a second pass to keep the regex small.
_COMPAT_RE = re.compile(
    r"compatibleAssetTypes\s*:\s*\[([^\]]*)\]",
    re.MULTILINE,
)
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_ID_RE = re.compile(r"^\s*id\s*:\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def _extract_compat(js_path: Path) -> tuple[str, list[str]]:
    """Return ``(id, compatibleAssetTypes)`` parsed from a default's .js."""
    content = js_path.read_text(encoding="utf-8")

    id_match = _ID_RE.search(content)
    if id_match is None:
        raise AssertionError(
            f"no `id: '...'` field found in {js_path.name}"
        )
    indicator_id = id_match.group(1)

    compat_match = _COMPAT_RE.search(content)
    if compat_match is None:
        raise AssertionError(
            f"no `compatibleAssetTypes: [...]` field in {js_path.name}"
        )
    raw = compat_match.group(1)
    compat = [m.group(1) for m in _QUOTED_RE.finditer(raw)]
    if not compat:
        raise AssertionError(
            f"compatibleAssetTypes is empty in {js_path.name}"
        )
    for item in compat:
        if item not in ASSET_TYPES:
            raise AssertionError(
                f"{js_path.name}: compat entry {item!r} not in ASSET_TYPES "
                f"{sorted(ASSET_TYPES)}"
            )
    return indicator_id, compat


# Discover at collection time so pytest lists each (id, asset_type) row.
_INDICATOR_FILES = sorted(DEFAULTS_DIR.glob("*.js"))
if not _INDICATOR_FILES:
    raise AssertionError(
        f"no default indicator files found under {DEFAULTS_DIR}"
    )

# Build the (indicator_id, compat_list, asset_type, expect_compat) matrix.
_MATRIX: list[tuple[str, tuple[str, ...], str, bool]] = []
for path in _INDICATOR_FILES:
    indicator_id, compat = _extract_compat(path)
    compat_tuple = tuple(compat)
    for asset_type in sorted(ASSET_TYPES):
        expect_compat = asset_type in compat
        _MATRIX.append((indicator_id, compat_tuple, asset_type, expect_compat))


# Sanity: at least 9 indicators × 3 asset types = 27 rows on entry to this
# wave. Wave 2c bumps to 11 × 3 = 33 — the matrix grows automatically.
assert len(_MATRIX) >= len(_INDICATOR_FILES) * len(ASSET_TYPES)


# ── Fixtures ───────────────────────────────────────────────────────────


_DATES = np.array(
    [
        20240102, 20240103, 20240104, 20240105, 20240108,
        20240109, 20240110, 20240111, 20240112, 20240115,
    ],
    dtype=np.int64,
)
_CLOSES = np.array(
    [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0],
    dtype=np.float64,
)


def _price_series() -> PriceSeries:
    n = _DATES.shape[0]
    return PriceSeries(
        dates=_DATES,
        open=_CLOSES - 1.0,
        high=_CLOSES + 1.0,
        low=_CLOSES - 2.0,
        close=_CLOSES,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def client():
    svc = MagicMock()
    svc.get_prices = AsyncMock(return_value=_price_series())

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(indicators_router)
    app.state.market_data = svc

    transport = ASGITransport(app=app)

    async def _factory() -> AsyncClient:
        return AsyncClient(transport=transport, base_url="http://test")

    return _factory


# Minimal valid sandbox source — the request body must be syntactically a
# valid indicator so we exercise the compat-check ordering correctly: it
# must fire BEFORE any series resolution / sandbox execution.
_TRIVIAL_CODE = (
    "def compute(series):\n"
    "    return series['price']\n"
)


def _request_body(
    *,
    indicator_id: str,
    compat: tuple[str, ...],
    asset_type: str,
) -> dict:
    return {
        "code": _TRIVIAL_CODE,
        "params": {},
        "series": {
            "price": {
                "type": "spot",
                "collection": "INDEX",
                "instrument_id": "SPX",
            },
        },
        "indicator_id": indicator_id,
        "asset_type": asset_type,
        "compatible_asset_types": list(compat),
    }


# ── Matrix tests ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("indicator_id", "compat", "asset_type", "expect_compat"),
    _MATRIX,
    ids=[
        f"{ind}-{at}-{'ok' if ok else 'reject'}"
        for ind, _c, at, ok in _MATRIX
    ],
)
async def test_compatibility_matrix(
    indicator_id: str,
    compat: tuple[str, ...],
    asset_type: str,
    expect_compat: bool,
    client,
) -> None:
    body = _request_body(
        indicator_id=indicator_id, compat=compat, asset_type=asset_type
    )
    async with await client() as ac:
        resp = await ac.post("/api/indicators/compute", json=body)

    if not expect_compat:
        # Negative pairing — must reject with structured 422.
        assert resp.status_code == 422, (
            f"{indicator_id} × {asset_type}: expected 422, got "
            f"{resp.status_code} body={resp.text!r}"
        )
        data = resp.json()
        assert data.get("error_code") == "INDICATOR_INCOMPATIBLE_ASSET", (
            f"{indicator_id} × {asset_type}: expected INDICATOR_INCOMPATIBLE_ASSET, "
            f"got {data!r}"
        )
        assert data.get("asset_type") == asset_type
        assert sorted(data.get("accepted_asset_types", [])) == sorted(compat)
        # We pass indicator_id, so it must be echoed.
        assert data.get("indicator_id") == indicator_id
    else:
        # Positive pairing — compat layer MUST NOT have rejected. The
        # request may still fail downstream (it shouldn't here, since we
        # mock the data layer), but we strictly disallow a 422 +
        # INDICATOR_INCOMPATIBLE_ASSET combination.
        if resp.status_code == 422:
            data = resp.json()
            assert data.get("error_code") != "INDICATOR_INCOMPATIBLE_ASSET", (
                f"{indicator_id} × {asset_type}: compat layer rejected a "
                f"declared-compatible pairing — body={data!r}"
            )


# ── Targeted regression tests for the contract ─────────────────────────


async def test_compat_check_skipped_when_asset_type_missing(client) -> None:
    """``asset_type`` None + ``compatible_asset_types`` set → no 422 from compat."""
    body = {
        "code": _TRIVIAL_CODE,
        "params": {},
        "series": {
            "price": {
                "type": "spot",
                "collection": "INDEX",
                "instrument_id": "SPX",
            },
        },
        "compatible_asset_types": ["option"],
    }
    async with await client() as ac:
        resp = await ac.post("/api/indicators/compute", json=body)
    if resp.status_code == 422:
        data = resp.json()
        assert data.get("error_code") != "INDICATOR_INCOMPATIBLE_ASSET"


async def test_compat_check_skipped_when_compat_list_missing(client) -> None:
    """``asset_type`` set + ``compatible_asset_types`` None → no 422 from compat."""
    body = {
        "code": _TRIVIAL_CODE,
        "params": {},
        "series": {
            "price": {
                "type": "spot",
                "collection": "INDEX",
                "instrument_id": "SPX",
            },
        },
        "asset_type": "option",
    }
    async with await client() as ac:
        resp = await ac.post("/api/indicators/compute", json=body)
    if resp.status_code == 422:
        data = resp.json()
        assert data.get("error_code") != "INDICATOR_INCOMPATIBLE_ASSET"


async def test_compat_error_omits_indicator_id_when_not_provided(client) -> None:
    """Without ``indicator_id`` in the request, the 422 body omits the key."""
    body = {
        "code": _TRIVIAL_CODE,
        "params": {},
        "series": {
            "price": {
                "type": "spot",
                "collection": "INDEX",
                "instrument_id": "SPX",
            },
        },
        "asset_type": "option",
        "compatible_asset_types": ["index", "equity"],
    }
    async with await client() as ac:
        resp = await ac.post("/api/indicators/compute", json=body)
    assert resp.status_code == 422, resp.text
    data = resp.json()
    assert data["error_code"] == "INDICATOR_INCOMPATIBLE_ASSET"
    assert data["asset_type"] == "option"
    assert sorted(data["accepted_asset_types"]) == ["equity", "index"]
    assert "indicator_id" not in data
