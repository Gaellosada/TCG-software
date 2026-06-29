"""Numerical-correctness tests for the two stateful default indicators.

Unlike ``test_default_indicators_library.py`` (a smoke test that only checks
each default runs end-to-end and returns a 1-D float64 array), this file
asserts EXACT hand-checked outputs for the two path-dependent defaults shipped
in Wave 2 — ``exhaustion`` and ``nthtap``.

The Python ``compute()`` body is extracted from the shipped ``.js`` files the
SAME way the smoke test does (``const code = `...`;`` template literal), so the
assertions exercise the literal source that ships to the UI — not a copy. Each
expected value below is documented with the hand calculation that produced it.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from tcg.engine.indicator_exec import run_indicator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"

# Same regex contract as test_default_indicators_library.py.
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _extract_python_source(stem: str) -> str:
    content = (DEFAULTS_DIR / f"{stem}.js").read_text(encoding="utf-8")
    match = _CODE_RE.search(content)
    if match is None:
        raise AssertionError(f"no `const code = `...`;` template literal in {stem}.js")
    return match.group(1)


# ---------------------------------------------------------------------------
# Exhaustion — signed symmetric {-1.0, 0.0, +1.0}
# ---------------------------------------------------------------------------
#
# All Exhaustion scenarios use ma_window=1 (ma == close, no smoothing) so the
# crossings are exactly the raw close crossings, except the explicit
# ma_window warmup case. upper=100, lower=90 throughout.


def test_exhaustion_down_cascade_fires_minus_one() -> None:
    src = _extract_python_source("exhaustion")
    params = {"upper": 100.0, "lower": 90.0, "window": 5, "ma_window": 1}
    # close:   [105,  98,  95,  92,  88,  80]
    #  bar1: 105>100 & 98<=100  -> crossed_down through UPPER (sequence start)
    #  bar4:  92>90  & 88<=90   -> crossed_down through LOWER (sequence end)
    #         start=bar1, end=bar4, span 3 <= window 5  -> DOWN cascade fires.
    # Expected: bar0 NaN (no predecessor for crossed_*), -1 at bar4, 0 else.
    close = np.array([105.0, 98.0, 95.0, 92.0, 88.0, 80.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert np.isnan(out[0])  # warmup: crossed_* has no bar-0 predecessor
    expected_tail = np.array([0.0, 0.0, 0.0, -1.0, 0.0])
    np.testing.assert_array_equal(out[1:], expected_tail)


def test_exhaustion_up_cascade_fires_plus_one() -> None:
    src = _extract_python_source("exhaustion")
    params = {"upper": 100.0, "lower": 90.0, "window": 5, "ma_window": 1}
    # close:   [ 85,  92,  95,  98, 102, 110]
    #  bar1:  85<90  & 92>=90   -> crossed_up through LOWER (sequence start)
    #  bar4:  98<100 & 102>=100 -> crossed_up through UPPER (sequence end)
    #         start=bar1, end=bar4, span 3 <= window 5  -> UP cascade fires.
    close = np.array([85.0, 92.0, 95.0, 98.0, 102.0, 110.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert np.isnan(out[0])
    expected_tail = np.array([0.0, 0.0, 0.0, 1.0, 0.0])
    np.testing.assert_array_equal(out[1:], expected_tail)


def test_exhaustion_reclaim_aborts_no_fire() -> None:
    src = _extract_python_source("exhaustion")
    params = {"upper": 100.0, "lower": 90.0, "window": 5, "ma_window": 1}
    # close:   [105,  98, 103, 108, 112, 120]
    #  bar1: crossed_down UPPER  -> down-candidate starts.
    #  bar2:  98<100 & 103>=100  -> crossed_up UPPER == ABORT, candidate reset.
    #  After the reclaim the series only rises; it never crosses LOWER again,
    #  so the down cascade never completes -> NO fire anywhere (all zeros).
    close = np.array([105.0, 98.0, 103.0, 108.0, 112.0, 120.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert np.isnan(out[0])
    np.testing.assert_array_equal(out[1:], np.zeros(5))


def test_exhaustion_window_expiry_no_fire() -> None:
    src = _extract_python_source("exhaustion")
    params = {"upper": 100.0, "lower": 90.0, "window": 2, "ma_window": 1}
    # close:   [105,  98,  97,  96,  88,  80]
    #  bar1: crossed_down UPPER  -> start.
    #  crossed_down LOWER only at bar4 (96>90 & 88<=90). span = 4-1 = 3 > W(2)
    #  -> candidate expires before completion -> NO fire.
    close = np.array([105.0, 98.0, 97.0, 96.0, 88.0, 80.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert np.isnan(out[0])
    np.testing.assert_array_equal(out[1:], np.zeros(5))


def test_exhaustion_ma_window_warmup_is_nan() -> None:
    src = _extract_python_source("exhaustion")
    params = {"upper": 100.0, "lower": 90.0, "window": 5, "ma_window": 3}
    # ma = SMA(close, 3): first 2 bars NaN (warmup). The crossings below are on
    # the SMA, not raw close.
    # close: [105, 104, 103, 98, 95, 92, 88, 80]
    # sma3 : [ nan, nan, 104.0, 101.67, 98.67, 95.0, 91.67, 86.67 ]
    #  sma crosses down UPPER(100) between bar3(101.67) and bar4(98.67) -> start bar4
    #  sma crosses down LOWER(90)  between bar6(91.67) and bar7(86.67) -> end bar7
    #  span 7-4 = 3 <= window 5 -> DOWN cascade fires at bar7.
    close = np.array([105.0, 104.0, 103.0, 98.0, 95.0, 92.0, 88.0, 80.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    # ma is NaN at bars 0,1 (SMA warm-up). bar2 is the first defined SMA value
    # but crossed_* at bar2 needs predecessor ma[1] (NaN) -> bar2 is also NaN.
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2])
    # From bar3 the crosses are defined; -1 only at the final bar (bar7).
    expected_from_3 = np.array([0.0, 0.0, 0.0, 0.0, -1.0])
    np.testing.assert_array_equal(out[3:], expected_from_3)


def test_exhaustion_rejects_inverted_bounds() -> None:
    src = _extract_python_source("exhaustion")
    # upper <= lower is ill-posed; compute() must fail loudly (assert).
    params = {"upper": 90.0, "lower": 100.0, "window": 5, "ma_window": 1}
    close = np.array([105.0, 98.0, 95.0, 92.0, 88.0, 80.0], dtype=float)
    try:
        run_indicator(src, params, {"close": close})
    except Exception:  # IndicatorRuntimeError wraps the in-sandbox AssertionError
        return
    raise AssertionError("expected Exhaustion to reject upper <= lower")


# ---------------------------------------------------------------------------
# NthTap — rolling count of level taps in a trailing window
# ---------------------------------------------------------------------------


def test_nthtap_rolling_count_matches_hand_count() -> None:
    src = _extract_python_source("nthtap")
    params = {"level": 100.0, "window": 3, "ma_window": 1}
    # close:  [ 95, 102,  98, 103,  97, 105]   level = 100
    #  taps (any crossing of 100):
    #   bar0: NaN (crossed_* has no predecessor)
    #   bar1: 95->102  cross UP    -> tap
    #   bar2: 102->98  cross DOWN  -> tap
    #   bar3: 98->103  cross UP    -> tap
    #   bar4: 103->97  cross DOWN  -> tap
    #   bar5: 97->105  cross UP    -> tap
    #  count_in_window(tap, 3): first window-1 = 2 bars NaN (warmup);
    #   bar2 window [0,1,2] contains the bar-0 NaN -> NaN (count undefined);
    #   bar3 window [1,2,3] = 1+1+1 = 3
    #   bar4 window [2,3,4] = 1+1+1 = 3
    #   bar5 window [3,4,5] = 1+1+1 = 3
    close = np.array([95.0, 102.0, 98.0, 103.0, 97.0, 105.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2])
    np.testing.assert_array_equal(out[3:], np.array([3.0, 3.0, 3.0]))


def test_nthtap_counts_only_crossings_not_levels() -> None:
    src = _extract_python_source("nthtap")
    params = {"level": 100.0, "window": 4, "ma_window": 1}
    # close:  [ 95,  96,  97,  98,  99, 99.5]  -- rises but NEVER crosses 100.
    #  No crossing anywhere -> every defined bar has tap 0.
    #  count_in_window(tap, 4): bars 0..2 NaN (warmup); bar3 window [0,1,2,3]
    #  contains bar-0 NaN -> NaN; bars 4,5 = 0 taps.
    close = np.array([95.0, 96.0, 97.0, 98.0, 99.0, 99.5], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert (
        np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2]) and np.isnan(out[3])
    )
    np.testing.assert_array_equal(out[4:], np.array([0.0, 0.0]))


def test_nthtap_ma_window_smooths_before_counting() -> None:
    src = _extract_python_source("nthtap")
    params = {"level": 100.0, "window": 3, "ma_window": 2}
    # ma = SMA(close, 2): first bar NaN.
    # close: [ 98, 104,  96, 104,  96, 104]
    # sma2 : [ nan, 101.0, 100.0, 100.0, 100.0, 100.0 ]
    #  crossings of level 100 on the SMA:
    #   bar1: NaN predecessor (sma[0] NaN)               -> tap NaN
    #   bar2: 101.0 -> 100.0  crossed_down (prev>100, cur<=100) -> tap 1
    #   bar3: 100.0 -> 100.0  no cross                          -> tap 0
    #   bar4: 100.0 -> 100.0  no cross                          -> tap 0
    #   bar5: 100.0 -> 100.0  no cross                          -> tap 0
    #  count_in_window(tap, 3): bars 0,1 NaN (warmup); bar2 NaN (bar1 NaN in
    #   window); bar3 window [1,2,3] has bar1 NaN -> NaN; bar4 window [2,3,4]
    #   = tap 1+0+0 = 1; bar5 window [3,4,5] = 0+0+0 = 0.
    close = np.array([98.0, 104.0, 96.0, 104.0, 96.0, 104.0], dtype=float)
    out = run_indicator(src, params, {"close": close})
    assert (
        np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2]) and np.isnan(out[3])
    )
    np.testing.assert_array_equal(out[4:], np.array([1.0, 0.0]))


def test_both_outputs_are_float64_aligned() -> None:
    for stem, params in (
        ("exhaustion", {"upper": 100.0, "lower": 90.0, "window": 5, "ma_window": 1}),
        ("nthtap", {"level": 100.0, "window": 3, "ma_window": 1}),
    ):
        src = _extract_python_source(stem)
        close = np.linspace(80.0, 120.0, 40, dtype=float)
        out = run_indicator(src, params, {"close": close})
        assert out.shape == (40,), f"{stem}: shape"
        assert out.dtype == np.float64, f"{stem}: dtype"
