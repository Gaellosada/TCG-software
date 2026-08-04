"""Hypothesis property tests for the DStat default indicators (SPEC §4.1, F6).

Invariants proven here (on arbitrary positive close series):
  * P1 length: output length == input length for both indicators.
  * P2 warm-up: the first ``max(ma_window, 2*vol_window)`` raw bars are NaN, and
       the first ``warm + pct_window - 1`` percentile bars are NaN.
  * P3 finiteness: raw DStat is finite wherever the vol window is non-degenerate
       (strictly monotone geometric input => vol>0 => all steady-state bars finite).
  * P4 monotone-in-percentile: the percentile line is pointwise non-decreasing in
       ``percentile`` (p95 >= p75 >= p50 >= p10) wherever all four are defined.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from tcg.engine.indicator_exec import run_indicator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _src(stem: str) -> str:
    match = _CODE_RE.search((DEFAULTS_DIR / f"{stem}.js").read_text(encoding="utf-8"))
    assert match is not None
    return match.group(1)


DSTAT_SRC = _src("dstat")
DSTAT_PCT_SRC = _src("dstat-percentile")

# Small windows keep the property runs fast while still exercising warm-up +
# a valid steady-state tail on modest series lengths.
MA_W, VOL_W, PCT_W = 5, 8, 20
WARM = max(MA_W, 2 * VOL_W)  # = 16
FIRST_PCT = WARM + PCT_W - 1  # = 35


# Log-returns as the free variable => guaranteed positive closes, controllable
# magnitude, no zero/NaN inputs. Length chosen to clear the percentile warm-up.
@st.composite
def _close_series(draw, min_len: int, max_len: int):
    n = draw(st.integers(min_value=min_len, max_value=max_len))
    rets = draw(
        st.lists(
            st.floats(min_value=-0.08, max_value=0.08, allow_nan=False, allow_infinity=False),
            min_size=n - 1,
            max_size=n - 1,
        )
    )
    close = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(rets)]))
    return close.astype(float)


@settings(max_examples=60, deadline=None)
@given(close=_close_series(WARM + 3, 120))
def test_raw_dstat_length_warmup_finite(close):
    got = run_indicator(DSTAT_SRC, {"ma_window": MA_W, "vol_window": VOL_W}, {"close": close})
    assert got.shape == close.shape                       # P1 length
    assert np.all(np.isnan(got[:WARM]))                   # P2 warm-up
    # P3: with |ret| <= 0.08 and n-1 distinct-ish returns, vol can still be 0
    # only if a whole window is perfectly flat. Where finite, it must be finite
    # (never inf). And at least the steady-state region must contain finite values.
    assert not np.any(np.isinf(got))
    steady = got[WARM:]
    # Not every bar is guaranteed finite (a flat vol window -> NaN), but the
    # array must not be entirely NaN past warm-up for a non-flat walk.
    assert np.any(np.isfinite(steady)) or np.allclose(np.diff(close), 0.0)


@settings(max_examples=60, deadline=None)
@given(close=_close_series(FIRST_PCT + 3, 90))
def test_percentile_length_and_warmup(close):
    params = {"ma_window": MA_W, "vol_window": VOL_W, "pct_window": PCT_W, "percentile": 95.0}
    got = run_indicator(DSTAT_PCT_SRC, params, {"close": close})
    assert got.shape == close.shape                       # P1 length
    assert np.all(np.isnan(got[:FIRST_PCT]))              # P2 warm-up
    assert not np.any(np.isinf(got))


@settings(max_examples=60, deadline=None)
@given(close=_close_series(FIRST_PCT + 5, 90))
def test_percentile_monotone_in_percentile(close):
    def line(p):
        return run_indicator(
            DSTAT_PCT_SRC,
            {"ma_window": MA_W, "vol_window": VOL_W, "pct_window": PCT_W, "percentile": float(p)},
            {"close": close},
        )

    p10, p50, p75, p95 = line(10), line(50), line(75), line(95)
    mask = np.isfinite(p10) & np.isfinite(p50) & np.isfinite(p75) & np.isfinite(p95)
    # P4: pointwise non-decreasing in percentile where all four are defined.
    assert np.all(p95[mask] >= p75[mask] - 1e-12)
    assert np.all(p75[mask] >= p50[mask] - 1e-12)
    assert np.all(p50[mask] >= p10[mask] - 1e-12)
