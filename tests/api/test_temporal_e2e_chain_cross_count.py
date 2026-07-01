"""Deep end-to-end correctness test for the two PR #69 features used TOGETHER:

  * ``cross_count`` (``CrossCondition`` ``count``/``window``) — the head of the
    chain requires TWO same-direction up-crosses inside a trailing window; and
  * a bounded **temporal chain** (block-level ``links``) — the second stage must
    land STRICTLY AFTER the head, within the link window.

Unlike the existing tests (which assert the engine's POSITION/automaton outputs
on isolated tiny cases, or assert the HTTP response *shape*), this test runs a
full SIMULATION through the real ``POST /api/signals/compute`` HTTP endpoint via
``ASGITransport`` and asserts BOTH the latched position series AND the downstream
``realized_pnl`` (cumulative percent-return) series — a complete open→close
round-trip.

The expected values below were derived INDEPENDENTLY by hand (a pure-python
oracle, no engine code) and then cross-checked against the engine; a reviewer can
re-derive every asserted number from the price series and the locked semantics in
the comment block. The test does NOT snapshot the code's output.

NOTE: This e2e test exercises both features working together over a realistic
simulation, but does not discriminate a count-ignoring or arm-before-advance bug
(identical positions/pnl/fired_indices would result). The actual discrimination
for those properties lives in the unit tests (``test_cross_count_two_in_window``,
the coincident-fire automaton test in ``tests/engine/test_temporal_composition.py``
and ``tests/property/test_temporal_automaton.py``).

────────────────────────────────────────────────────────────────────────────
SIGNAL
  Input X = spot SPX (close field). One ENTRY chain + one EXIT.

  Entry block E (weight +100 → signed +1.0, input X), a 2-stage chain:
    stage 0 = cross_above(close, 100), count=2, window=3   [cross_count feature]
    stage 1 = cross_above(close, 110)                       [single up-cross]
    links   = {1: 2}                                        [chain, window = 2 bars]
    Locked chain semantics (per tcg.engine.signal_exec._sequence_active):
      head arms a single forward-only candidate (latest-start); the successor
      must match on a LATER bar with 1 <= (t - tau) <= window (strictly-after,
      window inclusive); reaching the last stage FIRES an IMPULSE on that one
      bar and consumes the candidate.

  Exit block XE (targets EntryE):
    cross_below(close, 105)   [single down-cross] → clears E's latch.

SYNTHETIC CLOSE SERIES (13 bars, dates 2024-01-01 .. 2024-01-13)
  idx : 0    1    2    3    4    5    6    7    8    9    10   11   12
  px  : 101  99   102  98   104  107  113  109  104  108  112  106  110

BAR-BY-BAR HAND DERIVATION
  up-crosses of 100 (prev<=100 < cur): t2 (99→102), t4 (98→104).        [2 total]
  stage0 = (>=2 up-crosses of 100 in trailing 3 bars):
    t2: window {t0,t1,t2} has 1 up-cross  → False  (exercises count=2:
        a single cross of 100 does NOT satisfy the head)
    t4: window {t2,t3,t4} has 2 up-crosses → TRUE  (stage0 holds at t4 only)
    elsewhere False.                          stage0 = {t4}
  up-crosses of 110: t6 (107→113), t10 (108→112).      stage1 = {t6, t10}
  Chain automaton (window = 2):
    t4: head matches → arm candidate, tau = 4.
    t5: no advance (stage1[5] = False).
    t6: stage1[6] = True and (t - tau) = 6 - 4 = 2 = window (inclusive upper
        edge) → advance to final stage → FIRE @ t6; candidate consumed (impulse).
    t10: stage1 matches again, BUT stage0 never re-fires (no second pair of
        up-crosses of 100), so NO head is armed → NO fire (exercises the
        strictly-after arming: a bare stage-1 cross cannot fire the chain).
    fire = {t6}
  Exit cross_below 105: down-cross at t8 (109→104).      exit = {t8}
  Latch loop (locked per-bar order: clear-pass THEN entry-pass THEN emit):
    t6: entry fires, latch opens  → pos = +1.0
    t7: latch held                → pos = +1.0
    t8: exit clears latch (before any entry pass) → pos = 0.0
    pos = [0,0,0,0,0,0, 1,1, 0,0,0,0,0]
    Round trip: trade opens @ t6, closes @ t8 (long, signed_weight +1.0).

  realized_pnl (endpoint contract, Issue #4 — COMPOUNDED single-account equity):
    net_step[s]     = Σ_i pos_i[s-1] * (px_i[s] - px_i[s-1]) / px_i[s-1]
    equity_ratio[t] = Π_{s=1..t} (1 + net_step[s])            (clamped at 0)
    realized_pnl[t] = equity_ratio[t] - 1     (single input → the whole ratio-1)
    i.e. the signal is ONE account whose net per-bar exposure COMPOUNDS
    bar-to-bar (Issue #4 — see test_temporal_e2e_realized_pnl_is_compounding
    below, which pins this against the additive sum). The two features are
    orthogonal: the temporal chain/cross_count decide the POSITION series
    (unchanged); Issue #4 only changes how that position series is turned into
    an equity curve.
    Only two steps carry a non-zero position (pos[5]=0 → step into t6 is 0):
      step into t7 : r7 = pos[6]=1 * (109 - 113)/113 = -0.03539823008849557
      step into t8 : r8 = pos[7]=1 * (104 - 109)/109 = -0.04587155963302752
    compounded cumulative (equity_ratio - 1):
      pnl[0..6] = 0.0
      pnl[7]    = (1 + r7) - 1                       = -0.03539823008849557
      pnl[8..12]= (1 + r7)*(1 + r8) - 1              = -0.07964601769911506
                  (compounds the two steps; pos[8..]=0 so it holds flat after)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.api.errors import tcg_error_handler
from tcg.core.api.signals import router as signals_router
from tcg.types.errors import TCGError
from tcg.types.market import PriceSeries

# ── synthetic market data ────────────────────────────────────────────────────
DATES = np.array(
    [
        20240101,
        20240102,
        20240103,
        20240104,
        20240105,
        20240106,
        20240107,
        20240108,
        20240109,
        20240110,
        20240111,
        20240112,
        20240113,
    ],
    dtype=np.int64,
)
CLOSES = np.array(
    [
        101.0,
        99.0,
        102.0,
        98.0,
        104.0,
        107.0,
        113.0,
        109.0,
        104.0,
        108.0,
        112.0,
        106.0,
        110.0,
    ]
)

# Independently hand-derived expected results (see module docstring).
EXPECTED_POS = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
_STEP_T7 = (109.0 - 113.0) / 113.0  # -0.03539823008849557
_STEP_T8 = (104.0 - 109.0) / 109.0  # -0.04587155963302752
# Issue #4: realized_pnl is the COMPOUNDED equity_ratio − 1, not the additive
# sum. With the position open over exactly the two steps t6→t7 and t7→t8, the
# equity ratio is (1 + r7) after t7 and (1 + r7)*(1 + r8) after t8; it then
# holds flat (pos[8..]=0 → net_step 0 → factor 1). Derived by product, not typed.
_RATIO_T7 = 1.0 + _STEP_T7
_RATIO_T8 = _RATIO_T7 * (1.0 + _STEP_T8)
EXPECTED_PNL = [
    0.0,  # t0
    0.0,  # t1
    0.0,  # t2
    0.0,  # t3
    0.0,  # t4
    0.0,  # t5
    0.0,  # t6 (pos[5]=0 → no step into t6)
    _RATIO_T7 - 1.0,  # t7  = (1 + r7) − 1
    _RATIO_T8 - 1.0,  # t8  = (1 + r7)*(1 + r8) − 1  (compounded)
    _RATIO_T8 - 1.0,  # t9  (flat: pos=0 after close)
    _RATIO_T8 - 1.0,  # t10
    _RATIO_T8 - 1.0,  # t11
    _RATIO_T8 - 1.0,  # t12
]


def _price_series() -> PriceSeries:
    n = DATES.shape[0]
    # high/low padded generously so the close-driven crosses are the only thing
    # that matters; the engine reads only the ``close`` field here.
    return PriceSeries(
        dates=DATES,
        open=CLOSES - 0.5,
        high=CLOSES + 5.0,
        low=CLOSES - 5.0,
        close=CLOSES,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


@pytest.fixture
def temporal_app():
    svc = MagicMock()

    async def fake_get_prices(collection, instrument_id, start=None, end=None):
        return _price_series() if instrument_id == "SPX" else None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = svc
    # app-data repo is resolved by get_write_repository but never invoked
    # (no signal legs / no persistence).
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def temporal_client(temporal_app):
    transport = ASGITransport(app=temporal_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _cross(op: str, level: float, *, count=None, window=None) -> dict:
    c: dict = {
        "op": op,
        "lhs": {"kind": "instrument", "input_id": "X", "field": "close"},
        "rhs": {"kind": "constant", "value": level},
    }
    if count is not None:
        c["count"] = count
    if window is not None:
        c["window"] = window
    return c


def _signal_body() -> dict:
    return {
        "spec": {
            "id": "temporal-e2e",
            "name": "temporal chain + cross_count e2e",
            "inputs": [
                {
                    "id": "X",
                    "instrument": {
                        "type": "spot",
                        "collection": "INDEX",
                        "instrument_id": "SPX",
                    },
                }
            ],
            "rules": {
                "entries": [
                    {
                        "id": "E",
                        "name": "EntryE",
                        "input_id": "X",
                        "weight": 100.0,
                        "conditions": [
                            # stage 0 — cross_count head (2 up-crosses of 100 in
                            # a trailing 3-bar window).
                            _cross("cross_above", 100.0, count=2, window=3),
                            # stage 1 — a single up-cross of 110.
                            _cross("cross_above", 110.0),
                        ],
                        # temporal chain: stage 1 within 2 bars STRICTLY AFTER
                        # stage 0.
                        "links": {"1": 2},
                    }
                ],
                "exits": [
                    {
                        "id": "XE",
                        "target_entry_block_names": ["EntryE"],
                        "conditions": [_cross("cross_below", 105.0)],
                    }
                ],
            },
        },
        "indicators": [],
        "instruments": {},
    }


async def test_temporal_e2e_chain_cross_count_full_pipeline(
    temporal_client: AsyncClient,
):
    """A real simulation that uses BOTH PR #69 features together (a temporal
    chain whose head is a ``cross_count`` count=2 condition) plus an exit,
    driven through ``POST /api/signals/compute``. Asserts the full position
    series, the round-trip trade, the event traces, AND the downstream
    ``realized_pnl`` series — all against the independently hand-derived values
    in the module docstring."""
    resp = await temporal_client.post("/api/signals/compute", json=_signal_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # ── full latched position series (open @ t6 via the chain, close @ t8) ──
    assert len(data["positions"]) == 1
    pos = data["positions"][0]["values"]
    assert len(pos) == len(DATES)
    assert pos == pytest.approx(EXPECTED_POS)

    # ── downstream equity / cumulative-return series ──
    assert len(data["realized_pnl"]) == 1
    pnl = data["realized_pnl"][0]
    assert len(pnl) == len(DATES)
    assert pnl == pytest.approx(EXPECTED_PNL)
    # The two load-bearing bars, named explicitly so a reviewer can check them
    # against the compounded contract (Issue #4): the first step off ratio 1.0,
    # then the compounded product of both steps.
    assert pnl[7] == pytest.approx(_STEP_T7)  # = -0.03539823008849557
    assert pnl[12] == pytest.approx((1.0 + _STEP_T7) * (1.0 + _STEP_T8) - 1.0)
    # The position is flat until the chain fires → zero P&L through t6.
    assert pnl[6] == pytest.approx(0.0)

    # ── event traces prove BOTH features were load-bearing ──
    ev = {e["block_id"]: e for e in data["events"]}
    # The entry chain fires exactly once, at t6 — only because the cross_count
    # head held at t4 AND stage 1 landed within the 2-bar window. The lone
    # stage-1 up-cross at t10 (no armed head) does NOT fire.
    assert ev["E"]["kind"] == "entry"
    assert ev["E"]["fired_indices"] == [6]
    assert ev["E"]["latched_indices"] == [6]
    assert ev["E"]["active_indices"] == [6, 7]
    # The exit fires once at t8 and effectively closes the open latch.
    assert ev["XE"]["kind"] == "exit"
    assert ev["XE"]["fired_indices"] == [8]
    assert ev["XE"]["latched_indices"] == [8]
    assert ev["XE"]["target_entry_block_names"] == ["EntryE"]

    # ── one closed round-trip trade ──
    trades = data["trades"]
    assert len(trades) == 1
    tr = trades[0]
    assert tr["entry_block_id"] == "E"
    assert tr["entry_block_name"] == "EntryE"
    assert tr["exit_block_id"] == "XE"
    assert tr["open_bar"] == 6
    assert tr["close_bar"] == 8
    assert tr["direction"] == "long"
    assert tr["signed_weight"] == pytest.approx(1.0)
    assert tr["input_id"] == "X"


async def test_temporal_e2e_realized_pnl_is_compounding(
    temporal_client: AsyncClient,
):
    """Pins the endpoint's equity contract (Issue #4): ``realized_pnl`` is the
    COMPOUNDED equity curve (``equity_ratio − 1``), NOT a simple cumulative sum
    of per-bar returns. With the position open over exactly two bars (t6→t7 and
    t7→t8), the final P&L equals the PRODUCT ``(1 + r7)*(1 + r8) − 1``. For two
    NEGATIVE returns the compounded product carries a positive cross term, so it
    is LESS negative (smaller magnitude) than the additive sum. This is the
    post-#4 behaviour (Gael's reported bug fix: the signal is one compounding
    account); this test is the inverted successor of the pre-#4 guard —
    retained, pointed the right way, so a regression BACK to the additive sum is
    caught, not silently absorbed."""
    resp = await temporal_client.post("/api/signals/compute", json=_signal_body())
    assert resp.status_code == 200, resp.text
    pnl = resp.json()["realized_pnl"][0]

    simple_sum = _STEP_T7 + _STEP_T8
    compounded = (1.0 + _STEP_T7) * (1.0 + _STEP_T8) - 1.0
    # The engine emits the COMPOUNDED result…
    assert pnl[12] == pytest.approx(compounded)
    # …which is NOT the additive sum (they differ by the +cross term), so this
    # assertion genuinely discriminates a regression to non-compounding.
    assert compounded != pytest.approx(simple_sum)
    # Two negative returns → compounding is LESS negative (cross term > 0); the
    # compounded value the engine reports is therefore the less-negative one.
    assert compounded > simple_sum
    assert pnl[12] == pytest.approx(compounded) and pnl[12] > simple_sum
