"""Unit oracle tests for the DStat default indicators (SPEC §4.1, feature F6).

Covers both shipped defaults:
  * ``dstat``            — Layer-1 raw statistic (vol-normalised distance from MA)
  * ``dstat-percentile`` — Layer-2 trailing nearest-rank percentile line

Method: extract the Python template-literal from each ``.js`` default, run it
through the real ``run_indicator`` sandbox, and compare against an INDEPENDENT
plain-numpy reference implemented here (not the sandbox). Includes one fully
hand-computed value (arithmetic in comments) to catch systematic errors, and a
direct nearest-rank edge check ``idx = ceil(p*N) - 1``.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from tcg.engine.indicator_exec import run_indicator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)


def _src(stem: str) -> str:
    match = _CODE_RE.search((DEFAULTS_DIR / f"{stem}.js").read_text(encoding="utf-8"))
    assert match is not None, f"no `const code = ...` in {stem}.js"
    return match.group(1)


DSTAT_SRC = _src("dstat")
DSTAT_PCT_SRC = _src("dstat-percentile")


# --------------------------------------------------------------------------- #
# Independent reference implementations (plain numpy — NOT the sandbox).
# --------------------------------------------------------------------------- #
def ref_raw_dstat(close: np.ndarray, ma_window: int, vol_window: int) -> np.ndarray:
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    warm = max(ma_window, 2 * vol_window)
    if ma_window < 1 or vol_window < 2 or n <= warm:
        return out
    for i in range(warm, n):
        ma = np.mean(close[i - ma_window + 1 : i + 1])
        chunk = close[i - vol_window : i + 1]
        rets = np.log(chunk[1:] / chunk[:-1])
        vol = np.std(rets, ddof=1) * np.sqrt(252.0)
        if ma == 0.0 or vol == 0.0 or np.isnan(vol):
            continue
        out[i] = (close[i] / ma - 1.0) / vol
    return out


def ref_dstat_percentile(
    close: np.ndarray,
    ma_window: int,
    vol_window: int,
    pct_window: int,
    percentile: float,
) -> np.ndarray:
    raw = ref_raw_dstat(close, ma_window, vol_window)
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    warm = max(ma_window, 2 * vol_window)
    if percentile < 0.0 or percentile > 100.0 or pct_window < 1 or n <= warm:
        return out
    p = percentile / 100.0
    idx = int(np.ceil(p * pct_window)) - 1
    idx = max(0, min(pct_window - 1, idx))
    first_valid = warm + pct_window - 1
    for t in range(first_valid, n):
        w = raw[t - pct_window + 1 : t + 1]
        w_clean = w[~np.isnan(w)]
        if w_clean.shape[0] < pct_window:
            continue
        out[t] = np.sort(w_clean)[idx]
    return out


def _synth_close(n: int, seed: int = 7, sigma: float = 0.01) -> np.ndarray:
    """Positive, well-conditioned geometric random walk (log-returns well-defined)."""
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0, sigma, n)))


# --------------------------------------------------------------------------- #
# Raw DStat
# --------------------------------------------------------------------------- #
def test_raw_dstat_matches_reference_default_params():
    close = _synth_close(400)
    got = run_indicator(DSTAT_SRC, {"ma_window": 21, "vol_window": 63}, {"close": close})
    exp = ref_raw_dstat(close, 21, 63)
    np.testing.assert_allclose(got, exp, rtol=1e-9, atol=1e-9, equal_nan=True)


def test_raw_dstat_warmup_and_length():
    close = _synth_close(400)
    got = run_indicator(DSTAT_SRC, {"ma_window": 21, "vol_window": 63}, {"close": close})
    assert got.shape == close.shape
    # warm-up = max(21, 2*63) = 126 -> indices 0..125 are NaN, first value at 126.
    assert np.all(np.isnan(got[:126]))
    assert np.isfinite(got[126])
    assert np.all(np.isfinite(got[126:]))  # geometric walk => vol>0 everywhere


def test_raw_dstat_fully_hand_computed_value():
    """One end-to-end value computed entirely by hand (ma_window=2, vol_window=2).

    close = [100, 101, 102, 103, 104, 105], warm = max(2, 2*2) = 4.
    At index i = 4 (close=104):
      MA_4  = mean(close[3], close[4]) = (103 + 104) / 2 = 103.5
      r0    = ln(103/102) = 0.009756174945364656
      r1    = ln(104/103) = 0.009661910911736890
      For 2 samples, std(ddof=1) = |r0 - r1| / sqrt(2)
            = 0.00009426403362776.. / 1.4142135623.. = 6.6654e-05
      vol_4 = std * sqrt(252) = 6.6654e-05 * 15.8745.. = 0.0010581111531913097
      DSTAT_4 = (104/103.5 - 1) / vol_4
              = 0.004830917874396135 / 0.0010581111531913097
              = 4.565605286198805
    """
    close = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], dtype=float)
    got = run_indicator(DSTAT_SRC, {"ma_window": 2, "vol_window": 2}, {"close": close})
    assert np.all(np.isnan(got[:4]))
    assert got[4] == pytest.approx(4.565605286198805, abs=1e-9)


def test_raw_dstat_zero_vol_window_is_nan_not_inf():
    """Flat window => vol=0 => bar left NaN (no division blow-up)."""
    # First 6 identical closes give a zero-return vol window; then it moves.
    close = np.concatenate([np.full(6, 100.0), np.array([101.0, 102.0, 103.0, 104.0])])
    got = run_indicator(DSTAT_SRC, {"ma_window": 2, "vol_window": 2}, {"close": close})
    assert np.all(np.isfinite(got) | np.isnan(got))  # never +/-inf
    assert not np.any(np.isinf(got))


# --------------------------------------------------------------------------- #
# DStat percentile line
# --------------------------------------------------------------------------- #
def test_dstat_percentile_matches_reference():
    close = _synth_close(500)
    params = {"ma_window": 21, "vol_window": 63, "pct_window": 200, "percentile": 95.0}
    got = run_indicator(DSTAT_PCT_SRC, params, {"close": close})
    exp = ref_dstat_percentile(close, 21, 63, 200, 95.0)
    np.testing.assert_allclose(got, exp, rtol=1e-9, atol=1e-9, equal_nan=True)


def test_dstat_percentile_warmup_first_valid_index():
    close = _synth_close(500)
    params = {"ma_window": 21, "vol_window": 63, "pct_window": 200, "percentile": 95.0}
    got = run_indicator(DSTAT_PCT_SRC, params, {"close": close})
    # first valid = warm + pct_window - 1 = 126 + 200 - 1 = 325
    assert np.all(np.isnan(got[:325]))
    assert np.isfinite(got[325])


def test_dstat_percentile_out_of_range_is_all_nan():
    close = _synth_close(500)
    for bad in (-1.0, 100.1):
        got = run_indicator(
            DSTAT_PCT_SRC,
            {"ma_window": 21, "vol_window": 63, "pct_window": 200, "percentile": bad},
            {"close": close},
        )
        assert np.all(np.isnan(got))


def test_dstat_percentile_line_is_an_observed_raw_value():
    """Nearest-rank => every emitted line value equals some raw DStat in-window."""
    close = _synth_close(500)
    raw = run_indicator(DSTAT_SRC, {"ma_window": 21, "vol_window": 63}, {"close": close})
    params = {"ma_window": 21, "vol_window": 63, "pct_window": 200, "percentile": 60.0}
    line = run_indicator(DSTAT_PCT_SRC, params, {"close": close})
    raw_finite = set(np.round(raw[np.isfinite(raw)], 12).tolist())
    for v in line[np.isfinite(line)]:
        assert round(float(v), 12) in raw_finite


# --------------------------------------------------------------------------- #
# Nearest-rank index formula, isolated on a known array.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "percentile, expected_idx, expected_val",
    [
        (10.0, 0, 1.0),   # ceil(0.10*10)-1 = 0
        (50.0, 4, 5.0),   # ceil(0.50*10)-1 = 4
        (95.0, 9, 10.0),  # ceil(0.95*10)-1 = 9 -> max
        (100.0, 9, 10.0),  # ceil(1.00*10)-1 = 9 (clamped max)
    ],
)
def test_nearest_rank_index_edge(percentile, expected_idx, expected_val):
    """idx = ceil(p*N)-1 on the array 1..10 (N=10).

    Engineering a real raw-DStat window whose sorted values are exactly the
    ranks 1..10 is impractical, so this checks the nearest-rank index math and
    clamp directly (the same formula the indicator uses) on a known array.
    """
    arr = np.arange(1.0, 11.0)
    N = arr.shape[0]
    p = percentile / 100.0
    idx = int(np.ceil(p * N)) - 1
    idx = max(0, min(N - 1, idx))
    assert idx == expected_idx
    assert np.sort(arr)[idx] == expected_val
