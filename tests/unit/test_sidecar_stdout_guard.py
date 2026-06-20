"""Regression test for the console=False / sys.stdout=None Windows crash.

The bug: PyInstaller's windowed build (the Tauri desktop sidecar uses
``console=False``) sets ``sys.stdout``/``sys.stderr`` = None on Windows; uvicorn's
logging then calls ``sys.stdout.isatty()`` -> ``AttributeError`` -> the backend
crashes at startup ("Backend unreachable"). The fix in
``desktop/sidecar/tcg_backend.py`` reattaches the std streams to fd 1/2 (or
devnull) at the very start of ``main()``, before uvicorn is imported.

This test exercises the ACTUAL guard loop extracted from the source file (so it
tracks the shipped code, not a paraphrase): it (1) demonstrates the failure
mode, (2) asserts the guard reattaches None streams to writable / isatty-able
streams, and (3) asserts the guard is a no-op when streams are already present
(Linux/macOS). It is OS-independent — the Windows condition is simulated by
setting the streams to None.
"""

import ast
import io
import os
import sys
from pathlib import Path

# Resolve the shipped sidecar entry relative to the repo (tests/unit -> repo
# root), overridable via TCG_BACKEND_PY. Repo-relative so it works in CI / any
# checkout, not just this machine.
_DEFAULT_BACKEND = (
    Path(__file__).resolve().parents[2] / "desktop" / "sidecar" / "tcg_backend.py"
)
BACKEND = Path(os.environ.get("TCG_BACKEND_PY") or _DEFAULT_BACKEND)


def _extract_guard_source() -> str:
    """Pull the ``for _fd, _name in ((1,'stdout'),(2,'stderr')): ...`` loop out of
    the real source so this test exercises the shipped code, not a copy."""
    text = BACKEND.read_text()
    tree = ast.parse(text)
    main = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    for node in main.body:
        if isinstance(node, ast.For) and "stdout" in ast.unparse(node):
            return ast.unparse(node)
    raise AssertionError("std-stream guard loop not found in tcg_backend.main()")


def test_bug_repro_none_stream_has_no_isatty():
    """Before the fix, uvicorn's ``sys.stdout.isatty()`` raises on Windows."""
    saved = sys.stdout
    try:
        sys.stdout = None
        raised = False
        try:
            sys.stdout.isatty()  # what uvicorn's logging config does
        except AttributeError:
            raised = True
        assert raised, "expected AttributeError on None.isatty()"
    finally:
        sys.stdout = saved


def test_guard_reattaches_none_streams():
    """After the fix, the guard reattaches None streams so ``.isatty()`` works and
    they are writable -- exactly the precondition uvicorn needs.

    fd-safety: the guard does ``os.fdopen(1/2, "w")``; closing that wrapper would
    close the process's real stdout/stderr fd. We dup fd 1 & 2 first and dup2
    them back afterwards, so the real fds (and pytest's capture) survive.
    """
    guard_src = _extract_guard_source()
    saved_out, saved_err = sys.stdout, sys.stderr
    dup1, dup2 = os.dup(1), os.dup(2)
    opened = []
    try:
        sys.stdout = None
        sys.stderr = None
        exec(compile(guard_src, "<guard>", "exec"), {"sys": sys, "os": os})

        assert sys.stdout is not None
        assert sys.stderr is not None
        assert sys.stdout.isatty() in (True, False)
        assert sys.stderr.isatty() in (True, False)
        sys.stdout.write("")
        sys.stderr.write("")
        opened = [sys.stdout, sys.stderr]
    finally:
        for s in opened:
            try:
                s.close()  # may close fd 1/2 ...
            except Exception:
                pass
        os.dup2(dup1, 1)  # ... so restore the real fds from our dups
        os.dup2(dup2, 2)
        os.close(dup1)
        os.close(dup2)
        sys.stdout, sys.stderr = saved_out, saved_err


def test_guard_is_noop_when_streams_present():
    """On Linux/macOS the streams are NOT None; the guard must leave them be."""
    guard_src = _extract_guard_source()
    sentinel_out = io.StringIO()
    sentinel_err = io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sentinel_out
        sys.stderr = sentinel_err
        exec(compile(guard_src, "<guard>", "exec"), {"sys": sys, "os": os})
        assert sys.stdout is sentinel_out
        assert sys.stderr is sentinel_err
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
