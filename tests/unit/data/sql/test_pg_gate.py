"""Self-check for the real-SQL fixture's skip/HARD-FAIL gate (D4 residual).

The ``seeded_dwh`` fixture auto-skips when no PostgreSQL server binary is
discoverable — a green skip that silently loses the real-SQL semantic-drift
protection.  ``$TCG_REQUIRE_PG_TESTS`` converts that skip into a hard failure so
CI can enforce the guard.  These tests pin both branches of that decision.

The gate helpers live in the sibling ``conftest.py``; the ``tests/`` tree has no
``__init__.py`` so a plain ``from .conftest import ...`` cannot reach them.  We
load the conftest module by file path instead (its import side effects are
limited to ``import`` statements — spinning a server only happens inside the
fixture body, which we never invoke here).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).with_name("conftest.py")
_MOD_NAME = "_sql_conftest_under_test"


def _load_conftest():
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _CONFTEST)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the module-level ``@dataclass`` resolves its field
    # annotations via ``sys.modules[cls.__module__]``, which must exist first.
    sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_MOD_NAME, None)
        raise
    return mod


def test_env_truthy_semantics():
    cf = _load_conftest()
    for v in ("1", "true", "TRUE", "yes", "on", " 1 "):
        assert cf._env_truthy(v) is True, v
    for v in (None, "", "0", "false", "no", "off", "   "):
        assert cf._env_truthy(v) is False, v


def test_gate_skips_when_no_binary_and_env_unset(monkeypatch):
    cf = _load_conftest()
    monkeypatch.setattr(cf, "_candidate_bindirs", lambda: [])
    monkeypatch.delenv("TCG_REQUIRE_PG_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        cf._select_bindir()


def test_gate_hard_fails_when_required_and_no_binary(monkeypatch):
    cf = _load_conftest()
    monkeypatch.setattr(cf, "_candidate_bindirs", lambda: [])
    monkeypatch.setenv("TCG_REQUIRE_PG_TESTS", "1")
    # HARD FAIL (not skip): a required-but-missing pg binary must go RED.
    with pytest.raises(pytest.fail.Exception):
        cf._select_bindir()


def test_gate_returns_bindir_when_discoverable(monkeypatch):
    cf = _load_conftest()
    monkeypatch.setattr(cf, "_candidate_bindirs", lambda: ["/somewhere/bin"])
    # Even with the CI gate set, a discoverable binary neither skips nor fails.
    monkeypatch.setenv("TCG_REQUIRE_PG_TESTS", "1")
    assert cf._select_bindir() == "/somewhere/bin"
