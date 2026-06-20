"""Anti-drift guard for the frontend "new custom indicator" seed template.

The editor seeds a new custom indicator with the ``NEW_CODE_TEMPLATE`` literal
in ``frontend/src/pages/Indicators/IndicatorsPage.jsx``. Its header comment
documents the execution contract enforced by :mod:`tcg.engine.indicator_exec`,
and its body is meant to be a runnable example.

This test extracts that literal *from the JSX file at runtime* and pushes the
example body through the REAL sandbox (:func:`run_indicator`). If anyone edits
the template into something that no longer validates/executes — a bad
signature, a forbidden construct, a wrong return shape — this test fails, so an
invalid template can never ship. (It deliberately does NOT re-state the contract
itself; the sandbox module is the single source of truth.)
"""

from __future__ import annotations

import pathlib
import re

import numpy as np

from tcg.engine.indicator_exec import run_indicator


_TEMPLATE_RE = re.compile(r"NEW_CODE_TEMPLATE\s*=\s*`(.*?)`", re.DOTALL)
_JSX_REL = "frontend/src/pages/Indicators/IndicatorsPage.jsx"


def _repo_root() -> pathlib.Path:
    """Walk up from this file until the dir that contains ``frontend/``."""
    root = pathlib.Path(__file__).resolve()
    while not (root / "frontend").exists():
        if root.parent == root:
            raise RuntimeError("repo root (with a 'frontend' dir) not found")
        root = root.parent
    return root


def _template_code() -> str:
    """Return the raw JS-template-literal body of ``NEW_CODE_TEMPLATE``."""
    text = (_repo_root() / _JSX_REL).read_text(encoding="utf-8")
    match = _TEMPLATE_RE.search(text)
    assert match, "NEW_CODE_TEMPLATE literal not found in IndicatorsPage.jsx"
    return match.group(1)


def test_template_literal_has_no_js_interpolation_chars():
    """The literal must stay a plain JS template string.

    A backtick or ``${`` inside it would either terminate the string early or
    inject an interpolation — both silently corrupt the seeded code.
    """
    code = _template_code()
    assert "`" not in code, "template must not contain a backtick"
    assert "${" not in code, "template must not contain a ${ interpolation"


def test_new_indicator_template_validates_and_runs():
    """The example body validates + executes through the real sandbox."""
    code = _template_code()
    n = 50
    close = np.linspace(100.0, 110.0, n).astype(np.float64)

    out = run_indicator(code, {"window": 20}, {"close": close})

    assert out.shape == (n,)
    assert out.dtype == np.float64
    # Warm-up region is NaN; the rest is a finite rolling mean.
    assert np.all(np.isnan(out[:19]))
    assert np.all(np.isfinite(out[19:]))
    # The series is monotone increasing, so the trailing SMA sits below the
    # final price and above the window-start price — a cheap sanity bound.
    assert close[0] < out[-1] < close[-1]


def test_template_documents_real_constraint_phrases():
    """The header comment names the actual contract terms.

    Mirrors the frontend phrase assertions so a backend-only run still pins
    that the documented constraints (imports forbidden, curated ``np``, the
    ``compute(series, ...)`` signature) are present. The sandbox file remains
    the source of truth — this only guards against the doc silently emptying.
    """
    code = _template_code()
    for phrase in ("def compute(series", "pandas", "import", "np", "math"):
        assert phrase in code, f"template no longer mentions {phrase!r}"
