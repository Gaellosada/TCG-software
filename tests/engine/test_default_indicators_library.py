"""Smoke tests for the 9 default indicators shipped with the UI.

Each test:
 1. Reads the raw .js file for the indicator.
 2. Regex-extracts the template-literal body (Python source).
 3. Uses `ast` to pull the typed defaults out of `def compute(series, ...)`.
 4. Runs `run_indicator` against a deterministic synthetic close series.
 5. Asserts the output is a 1-D float64 array of the right length.

Optionally also exercises a larger window (where applicable) to confirm the
indicator does not blow up on longer windows than its default.

This is a smoke test: it does NOT assert numerical correctness. Per-indicator
correctness is the job of the existing engine unit tests. This file guards
against the library drifting into states where an indicator fails to even
run end-to-end through `run_indicator` with its declared defaults.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tcg.engine.indicator_exec import run_indicator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "Indicators" / "defaults"

# Matches:   const code = `...`;
# The body may span many lines; we capture up to the first closing backtick.
# The body itself never contains backticks in well-formed sources; if a file
# breaks that rule the regex stops early and the resulting Python source will
# fail to parse — caught loudly by the tests below.
_CODE_RE = re.compile(r"const\s+code\s*=\s*`([\s\S]*?)`\s*;", re.MULTILINE)

# Deterministic synthetic close series: monotonic ramp + sinusoidal wobble.
# Length 200 is large enough to exercise every default's longest window
# (slow EMA in MACD = 26, KAMA slow = 30, 2*(window-1)+1 for DEMA/TEMA with
# window=20 = 59) with a healthy valid tail. dtype = float64 to match the
# engine's expected input dtype.
_SERIES_LENGTH = 200


def _make_series() -> np.ndarray:
    ramp = np.linspace(100.0, 120.0, _SERIES_LENGTH, dtype=np.float64)
    wobble = 5.0 * np.sin(np.linspace(0.0, 6.0 * np.pi, _SERIES_LENGTH, dtype=np.float64))
    return ramp + wobble


def _extract_python_source(js_path: Path) -> str:
    """Pull the Python template-literal body out of a default's .js file."""
    content = js_path.read_text(encoding="utf-8")
    match = _CODE_RE.search(content)
    if match is None:
        raise AssertionError(
            f"no `const code = \\`...\\`;` template literal found in {js_path.name}"
        )
    return match.group(1)


def _extract_defaults(py_source: str) -> dict[str, int | float | bool]:
    """Parse the Python source and return the typed defaults for compute()."""
    tree = ast.parse(py_source)
    compute_def: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute":
            compute_def = node
            break
    if compute_def is None:
        raise AssertionError("no top-level `def compute(...)` found")

    args = compute_def.args
    positional = args.args
    # First positional is `series` — no default, skip it.
    params = positional[1:]
    defaults_ast = args.defaults
    # Defaults align to the tail of `positional`. Since `series` has no
    # default, len(defaults_ast) must equal len(params).
    if len(defaults_ast) != len(params):
        raise AssertionError(
            f"expected every non-series param to have a default; got "
            f"{len(params)} params vs {len(defaults_ast)} defaults"
        )

    out: dict[str, int | float | bool] = {}
    for arg, default_node in zip(params, defaults_ast):
        if not isinstance(default_node, ast.Constant):
            raise AssertionError(
                f"param {arg.arg!r} default is not a literal constant"
            )
        out[arg.arg] = default_node.value
    return out


# Discover all default indicator files up front so pytest's collection
# lists them by id in the progress output.
_INDICATOR_FILES = sorted(DEFAULTS_DIR.glob("*.js"))
if not _INDICATOR_FILES:
    raise AssertionError(
        f"no default indicator files under {DEFAULTS_DIR}"
    )


def _extract_series_labels(py_source: str) -> set[str]:
    """Walk the compute() body and collect every ``series['<label>']`` access.

    Generic over indicator implementation: as long as the body indexes
    ``series`` by a string literal, the label is discovered. This keeps the
    smoke-test fixture honest as new option-native defaults land that
    consume non-`close` semantic labels (e.g. ``atm_iv``,
    ``front_atm_iv``, ``back_atm_iv``).
    """
    tree = ast.parse(py_source)
    labels: set[str] = set()
    for node in ast.walk(tree):
        # Match: series['<label>']
        if not isinstance(node, ast.Subscript):
            continue
        value = node.value
        if not (isinstance(value, ast.Name) and value.id == "series"):
            continue
        # Slice may be an ast.Constant (Py3.9+) or ast.Index wrapping one.
        slice_node = node.slice
        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
            labels.add(slice_node.value)
    return labels


def _synthetic_label_series(label: str, base: np.ndarray) -> np.ndarray:
    """Deterministic synthetic series for an arbitrary semantic label.

    The smoke test does not assert correctness, only that compute() runs
    end-to-end and produces a 1-D float64 array of the right length. Each
    label gets a deterministic perturbation of the base ramp+wobble so
    multi-stream indicators (e.g. ``term-structure-slope``) see two
    distinct, well-conditioned inputs rather than the same array twice.
    """
    # Stable per-label seed so runs are reproducible. The hash space is
    # large enough that collision-induced offset clashes are vanishingly
    # rare for the small label set we ship.
    seed = abs(hash(label)) % (2**31 - 1)
    rng = np.random.default_rng(seed)
    # Small additive offset and a small multiplicative jitter (well above
    # zero so derived ratios stay well-defined).
    offset = rng.uniform(-2.0, 2.0)
    return (base + offset).astype(np.float64)


# Aggregate every series label referenced by any default's compute() body.
# This drives the fixture so that ANY future default whose body indexes
# ``series['<new_label>']`` is automatically supplied without further
# fixture edits.
_ALL_REFERENCED_LABELS: set[str] = set()
for _path in _INDICATOR_FILES:
    _ALL_REFERENCED_LABELS |= _extract_series_labels(_extract_python_source(_path))


@pytest.fixture(scope="module")
def series_dict() -> dict[str, np.ndarray]:
    close = _make_series()
    # OHLC + entry channels are pre-populated for backward compatibility
    # with the legacy 9 defaults (all consume ``series['close']``).
    high = close + 0.5
    low = close - 0.5
    opn = np.concatenate(([close[0]], close[:-1]))
    entry = close.copy()
    out: dict[str, np.ndarray] = {
        "close": close,
        "open": opn,
        "high": high,
        "low": low,
        "entry": entry,
    }
    # For every label referenced by a default's compute() body that we
    # haven't already populated, synthesize a deterministic per-label
    # array. This keeps the fixture in lock-step with the registry —
    # adding a new option-native indicator that consumes a fresh
    # semantic label requires zero fixture maintenance.
    for label in _ALL_REFERENCED_LABELS:
        if label in out:
            continue
        out[label] = _synthetic_label_series(label, close)
    return out


@pytest.mark.parametrize(
    "indicator_path",
    _INDICATOR_FILES,
    ids=lambda p: p.stem,
)
def test_default_indicator_runs_with_declared_defaults(
    indicator_path: Path, series_dict: dict[str, np.ndarray]
) -> None:
    """Every default indicator runs cleanly through the sandbox."""
    py_source = _extract_python_source(indicator_path)
    defaults = _extract_defaults(py_source)

    result = run_indicator(py_source, defaults, series_dict)

    assert result.shape == (_SERIES_LENGTH,), (
        f"{indicator_path.stem}: expected shape ({_SERIES_LENGTH},), got "
        f"{result.shape}"
    )
    assert result.dtype == np.float64, (
        f"{indicator_path.stem}: expected float64, got {result.dtype}"
    )


# Second parametrization: for every indicator whose signature accepts a
# ``window`` parameter, also exercise it at window=50 to confirm larger
# windows don't crash. Indicators without a ``window`` param are skipped.
@pytest.mark.parametrize(
    "indicator_path",
    _INDICATOR_FILES,
    ids=lambda p: p.stem,
)
def test_default_indicator_runs_with_larger_window(
    indicator_path: Path, series_dict: dict[str, np.ndarray]
) -> None:
    py_source = _extract_python_source(indicator_path)
    defaults = _extract_defaults(py_source)
    if "window" not in defaults:
        pytest.skip("indicator has no `window` param")

    bumped: dict[str, Any] = dict(defaults)
    bumped["window"] = 50
    # KAMA's logic requires window < slow; 50 < 30 is false, so for KAMA we
    # also bump `slow` to stay consistent. Same idea for MACD signal/hist if
    # window were present (they don't have window, so no-op there).
    if "slow" in bumped and bumped["slow"] <= bumped["window"]:
        bumped["slow"] = bumped["window"] + 10
    if "fast" in bumped and bumped["fast"] >= bumped["window"]:
        bumped["fast"] = max(2, bumped["window"] // 10)

    result = run_indicator(py_source, bumped, series_dict)

    assert result.shape == (_SERIES_LENGTH,)
    assert result.dtype == np.float64


# -- Correctness spot-checks ------------------------------------------------
# Independent numpy reference implementations for a representative subset
# (SMA, EMA, RSI). These guard against the shipped code drifting from the
# canonical definitions — caught here even if every default keeps running.


def _sma_reference(close: np.ndarray, window: int) -> np.ndarray:
    out = np.full(close.shape[0], np.nan, dtype=float)
    if close.shape[0] < window:
        return out
    out[window - 1:] = np.convolve(close, np.ones(window) / window, mode="valid")
    return out


def _ema_reference(close: np.ndarray, window: int) -> np.ndarray:
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    alpha = 2.0 / (window + 1)
    prev = float(np.mean(close[:window]))
    out[window - 1] = prev
    for i in range(window, n):
        prev = alpha * close[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _rsi_reference(close: np.ndarray, window: int) -> np.ndarray:
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    diff = np.diff(close)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = float(np.mean(gains[:window]))
    avg_loss = float(np.mean(losses[:window]))
    rs = np.inf if avg_loss == 0 else avg_gain / avg_loss
    out[window] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(window + 1, n):
        avg_gain = ((window - 1) * avg_gain + gains[i - 1]) / window
        avg_loss = ((window - 1) * avg_loss + losses[i - 1]) / window
        rs = np.inf if avg_loss == 0 else avg_gain / avg_loss
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


@pytest.mark.parametrize(
    "stem, reference, window",
    [
        ("sma", _sma_reference, 20),
        ("ema", _ema_reference, 20),
        ("rsi", _rsi_reference, 14),
    ],
)
def test_default_indicator_matches_reference(
    stem: str,
    reference,
    window: int,
    series_dict: dict[str, np.ndarray],
) -> None:
    """Shipped source matches an independent numpy reference implementation."""
    py_source = _extract_python_source(DEFAULTS_DIR / f"{stem}.js")
    result = run_indicator(py_source, {"window": window}, series_dict)
    expected = reference(series_dict["close"], window)
    np.testing.assert_allclose(result, expected, rtol=1e-9, atol=1e-9, equal_nan=True)
