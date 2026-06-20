"""Regression test for the parent-death watchdog in the Tauri sidecar.

The bug it guards (B1): the desktop sidecar is a PyInstaller *one-file* build —
a bootloader process that forks the real uvicorn child. Tauri spawns/kills only
the bootloader (SIGKILL on Unix / TerminateProcess on Windows — both uncatchable,
so the bootloader can't forward them), which would ORPHAN the uvicorn child: it
keeps holding port 8000 and blocks the next launch / credentials-restart from
binding. The fix in ``desktop/sidecar/tcg_backend.py`` installs a parent-death
watchdog (``_install_parent_death_watchdog``) that waits on the bootloader and
hard-exits this process when it dies, releasing the socket.

Runtime behaviour is proven end-to-end by the CI orphan smoke (Linux/macOS +
Windows) which kills the bootloader and asserts the watchdog logs its shutdown
and leaves no surviving process. This file is the FAST unit-level guard so a
refactor can't silently break the watchdog's safety invariants without a CI run:

  1. it is armed BEFORE the blocking ``server.serve()`` (so it covers startup);
  2. it hard-exits with ``os._exit`` (NOT ``sys.exit``, which from a daemon
     thread would only raise SystemExit in that thread and leave uvicorn up);
  3. it is best-effort — if psutil is unavailable it must NOT arm and must NOT
     raise, so a missing dependency can never take down a healthy server.

It does NOT simulate an actual parent death (that path ends in ``os._exit`` and
would terminate the test runner); the end-to-end kill is the CI smoke's job.
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path

_DEFAULT_BACKEND = (
    Path(__file__).resolve().parents[2] / "desktop" / "sidecar" / "tcg_backend.py"
)
BACKEND = Path(os.environ.get("TCG_BACKEND_PY") or _DEFAULT_BACKEND)

_WATCHDOG = "_install_parent_death_watchdog"


def _backend_ast() -> ast.Module:
    return ast.parse(BACKEND.read_text())


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
    )


def test_watchdog_armed_before_serve():
    """In main(), the watchdog is installed BEFORE the uvicorn serve call.

    If it armed after ``server.serve()`` (which blocks forever) it would never
    run, and a kill during the blocking dwh-pool startup would orphan the child.
    """
    main = _func(_backend_ast(), "main")
    stmts = [ast.unparse(s) for s in main.body]
    arm_idx = next(i for i, s in enumerate(stmts) if f"{_WATCHDOG}(" in s)
    serve_idx = next(i for i, s in enumerate(stmts) if "run_until_complete" in s)
    assert arm_idx < serve_idx, "watchdog must be armed before server.serve()"


def test_watchdog_hard_exits_not_sys_exit():
    """The watchdog must terminate the whole process (os._exit), not sys.exit().

    From its daemon thread, ``sys.exit`` only raises SystemExit in that thread —
    uvicorn keeps running and the port stays held. ``os._exit`` is the only
    correct choice.
    """
    fn = _func(_backend_ast(), _WATCHDOG)
    src = ast.unparse(fn)
    assert "os._exit" in src, "watchdog must call os._exit to release the socket"
    # No sys.exit / SystemExit anywhere in the watchdog.
    assert "sys.exit" not in src, (
        "watchdog must not use sys.exit (ineffective from a thread)"
    )


def _load_backend_module():
    """Import tcg_backend.py as a module WITHOUT running main() (its
    ``if __name__ == '__main__'`` guard stays false under an explicit name)."""
    spec = importlib.util.spec_from_file_location("tcg_backend_under_test", BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_watchdog_is_noop_without_psutil():
    """If psutil can't be imported, the watchdog must NOT arm and MUST NOT raise.

    Guarantees a missing optional dependency degrades to "no watchdog" (the
    pre-fix behaviour) rather than crashing the backend at startup.
    """
    mod = _load_backend_module()
    saved = sys.modules.get("psutil", "absent")
    try:
        # A None entry makes ``import psutil`` raise ImportError.
        sys.modules["psutil"] = None  # type: ignore[assignment]
        # Must return cleanly (the function swallows the ImportError and warns).
        assert mod._install_parent_death_watchdog() is None
        # And it must not have started the watchdog thread.
        import threading

        assert not any(
            t.name == "parent-death-watchdog" for t in threading.enumerate()
        ), "watchdog thread must not start when psutil is unavailable"
    finally:
        if saved == "absent":
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved
