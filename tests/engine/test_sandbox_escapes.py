"""Sandbox-escape regression battery for ``tcg.engine.indicator_exec``.

Each entry in ``BAD_CODE`` is a known or plausible CPython
restricted-exec escape that previously worked or was thought to work.
All of them must be rejected by :func:`validate_code` /
:func:`run_indicator` with :class:`IndicatorValidationError`.

New escape vectors discovered in the wild should be added here as
regression tests before the fix lands.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
    validate_code,
)


def _series() -> dict[str, np.ndarray]:
    return {"SPX": np.asarray([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)}


# --------------------------------------------------------------------------
# Rejected escape attempts
# --------------------------------------------------------------------------

BAD_CODE: dict[str, str] = {
    # getattr is no longer in the builtins whitelist. Even if it were,
    # the string "__import__" starts with "_" and would be rejected by
    # the ast.Constant walker.
    "getattr_builtins_import": (
        "def compute(series):\n"
        "    return getattr(__builtins__, '__import__')('os').listdir('/tmp')\n"
    ),
    # type(...).__globals__ — rejected because .__globals__ starts with "_".
    "type_globals_attr": (
        "def compute(series):\n"
        "    return type(lambda: 0).__globals__\n"
    ),
    # ().__class__.__bases__ — rejected on the .__class__ attribute.
    "empty_tuple_class_bases": (
        "def compute(series):\n"
        "    return ().__class__.__bases__\n"
    ),
    # str.format attribute-traversal — the format spec "{0.__class__}"
    # matches the _FORMAT_ATTR_RE string-literal rejection.
    "format_attr_traversal": (
        "def compute(series):\n"
        "    return '{0.__class__}'.format(1)\n"
    ),
    # f-string attribute access — rejected because JoinedStr nodes
    # are forbidden outright.
    "fstring_attribute": (
        "def compute(series):\n"
        "    return f'{(1).__class__}'\n"
    ),
    # eval / exec / open / __import__ — historical direct-call blocks.
    "eval_call": (
        "def compute(series):\n"
        "    return eval('1+1')\n"
    ),
    "import_call_name": (
        "def compute(series):\n"
        "    __import__('os')\n"
        "    return next(iter(series.values()))\n"
    ),
    "open_call": (
        "def compute(series):\n"
        "    open('/etc/passwd')\n"
        "    return next(iter(series.values()))\n"
    ),
    # String-literal dunder reference even when nothing obviously
    # dangerous is in scope — belt-and-braces check that any future
    # whitelist addition (hasattr, operator.attrgetter, …) is
    # neutralised by the universal string-literal rejection.
    "string_literal_dunder_bare": (
        "def compute(series):\n"
        "    x = '__class__'\n"
        "    return next(iter(series.values()))\n"
    ),
    # Dict key that starts with "_" — would be dangerous if fed back
    # via ``**`` unpacking to a function that performs attribute access.
    "dict_key_underscore_literal": (
        "def compute(series):\n"
        "    d = {'_x': 1}\n"
        "    return next(iter(series.values()))\n"
    ),
    # %-formatting path. "%(_cls)s" starts with "%" so does not trigger
    # the format-regex, but the key "_cls" appears nowhere; instead the
    # realistic exploit uses "%s" on an object whose __repr__ leaks info,
    # which requires no literal. We still cover a dunder attr via %-format
    # of the form "%(__class__)s" — the literal starts with "%" which
    # does NOT start with "_", so this vector is handled by blocking
    # str % dict at the .format() Call level via the ``__mod__`` method
    # name match is not possible. We instead rely on the broader dunder
    # rule: "__class__" as a string literal starts with "_" → rejected.
    "percent_format_dunder_key": (
        "def compute(series):\n"
        "    d = {'__class__': 1}\n"
        "    return '%(__class__)s' % d\n"
    ),
    # Nested string-literal in a comprehension.
    "nested_literal_in_comp": (
        "def compute(series):\n"
        "    xs = ['__builtins__' for _ in range(1)]\n"
        "    return next(iter(series.values()))\n"
    ),
    # New (iter 5): typed-signature form tries to smuggle a dunder via a
    # parameter name. ``ast.Name.id`` for annotations is always a plain
    # identifier — the parser itself rejects `def f(x: __class__): ...`
    # syntactically, but we also ensure the signature validator rejects
    # non-whitelisted annotation IDs beyond int/float/bool.
    "typed_sig_annotation_not_whitelisted": (
        "def compute(series, window: object = 0):\n"
        "    return next(iter(series.values()))\n"
    ),
    # New (iter 5): typed-signature form with dunder access inside the body.
    # The body walker must still reject the dunder even though the signature
    # is well-formed.
    "typed_sig_body_dunder_access": (
        "def compute(series, window: int = 3):\n"
        "    cls = series.__class__\n"
        "    return next(iter(series.values()))\n"
    ),
}


@pytest.mark.parametrize("label,code", sorted(BAD_CODE.items()))
def test_escape_is_rejected(label: str, code: str) -> None:
    # All of these must fail at validation time (preferred) or at most
    # at runtime — but must NEVER succeed and return a value. We accept
    # either of the two sandbox exception types.
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


@pytest.mark.parametrize("label,code", sorted(BAD_CODE.items()))
def test_escape_rejected_by_validate_code_directly(
    label: str, code: str
) -> None:
    """Prefer static rejection — keeps the exec path from running at all.

    A couple of vectors (e.g. ``getattr`` with a non-underscore string)
    may only fail at runtime because the sandbox builtins dict no longer
    contains ``getattr``. For those we allow the static walker to pass
    and rely on the NameError at exec time.
    """
    # These labels need runtime to trigger (they are blocked only
    # because the callable isn't in the whitelist, not because the AST
    # contains a forbidden construct).
    # ``typed_sig_annotation_not_whitelisted`` passes the static AST walker
    # (no forbidden node shapes) but is rejected by the compute() signature
    # validator, which runs inside ``run_indicator`` after ``validate_code``.
    runtime_only = {"typed_sig_annotation_not_whitelisted"}

    if label in runtime_only:
        # Either is acceptable for these — just confirm end-to-end block.
        with pytest.raises(
            (IndicatorValidationError, IndicatorRuntimeError)
        ):
            run_indicator(code, {}, _series())
    else:
        with pytest.raises(IndicatorValidationError):
            validate_code(code)


# --------------------------------------------------------------------------
# Positive sanity check: the default SMA indicator still validates and runs.
# --------------------------------------------------------------------------

_SMA_CODE = (
    "def compute(series, window: int = 3):\n"
    "    s = next(iter(series.values()))\n"
    "    out = np.full_like(s, np.nan, dtype=float)\n"
    "    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')\n"
    "    return out\n"
)


def test_default_sma_still_runs_after_hardening() -> None:
    """Guardrail: hardening must not break the documented default."""
    result = run_indicator(_SMA_CODE, {"window": 3}, _series())
    assert result.shape == (5,)
    assert np.isnan(result[0])
    assert np.isnan(result[1])
    np.testing.assert_allclose(result[2:], [2.0, 3.0, 4.0])


# --------------------------------------------------------------------------
# numpy-facade attack regressions (PR robustness review: BLOCKER findings).
#
# Each of the four attack classes below was CONFIRMED against the pre-facade
# executor to execute successfully. They must all be blocked now that ``np``
# is a curated facade instead of the real ``numpy`` module.
# --------------------------------------------------------------------------


def _rce_marker(tmp_path, name: str) -> str:
    """Return a tmp-path-scoped marker path used to detect RCE/file-write."""
    return str(tmp_path / name)


def test_rce_via_np_f2py_os_system_is_blocked(tmp_path) -> None:
    """B1: ``np.f2py.os.system(...)`` must NOT execute a shell command.

    Pre-facade: this wrote a file to the filesystem. Post-facade: the
    attribute ``f2py`` is not exposed by the facade, so the very first
    attribute lookup raises ``AttributeError``.
    """
    marker = _rce_marker(tmp_path, "rce_marker.txt")
    code = (
        "def compute(series):\n"
        f"    np.f2py.os.system('echo PWNED > {marker}')\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())
    # Belt-and-braces: confirm the side-effect never occurred.
    assert not (tmp_path / "rce_marker.txt").exists()


def test_rce_via_np_f2py_subprocess_run_is_blocked(tmp_path) -> None:
    """B1 (alt): ``np.f2py.subprocess.run(...)`` must NOT spawn a process."""
    marker = _rce_marker(tmp_path, "rce_sub.txt")
    code = (
        "def compute(series):\n"
        "    np.f2py.subprocess.run(\n"
        f"        ['/bin/sh', '-c', 'echo PWNED > {marker}']\n"
        "    )\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())
    assert not (tmp_path / "rce_sub.txt").exists()


def test_shared_library_load_via_ctypeslib_is_blocked() -> None:
    """B2: ``np.ctypeslib.load_library`` must be unreachable."""
    code = (
        "def compute(series):\n"
        "    np.ctypeslib.load_library('libc', '/lib')\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


def test_arbitrary_file_write_via_np_savetxt_is_blocked(tmp_path) -> None:
    """B3: ``np.savetxt`` must NOT write to the filesystem."""
    target = tmp_path / "should_not_exist.txt"
    code = (
        "def compute(series):\n"
        f"    np.savetxt({str(target)!r}, series['SPX'])\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())
    assert not target.exists()


def test_arbitrary_file_write_via_np_save_is_blocked(tmp_path) -> None:
    """B3 (alt): ``np.save`` must NOT write a .npy file."""
    target = tmp_path / "should_not_exist.npy"
    code = (
        "def compute(series):\n"
        f"    np.save({str(target)!r}, series['SPX'])\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())
    assert not target.exists()


def test_arbitrary_file_read_via_np_loadtxt_is_blocked() -> None:
    """B4: ``np.loadtxt`` must NOT read from the filesystem.

    We target ``/etc/passwd`` because it is world-readable on Linux — a
    successful read would prove the exfil primitive works. Post-facade,
    the attribute ``loadtxt`` is not exposed, so even before numpy touches
    the disk we get ``AttributeError``.
    """
    code = (
        "def compute(series):\n"
        "    np.loadtxt('/etc/passwd')\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


def test_arbitrary_file_read_via_np_genfromtxt_is_blocked() -> None:
    """B4 (alt): ``np.genfromtxt`` must be unreachable."""
    code = (
        "def compute(series):\n"
        "    np.genfromtxt('/etc/passwd')\n"
        "    return series['SPX']\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


# Broad sweep: every numpy submodule / attribute known to re-export
# stdlib or expose file I/O must be blocked by the facade. This covers
# future numpy releases that may add new attributes without renaming
# (if someone adds a new submodule ``np.newio`` that re-exports ``os``,
# this sweep does NOT catch it automatically — but the facade's
# allow-list policy does, because new attributes are not auto-admitted).
_DANGEROUS_NUMPY_ATTRS: tuple[str, ...] = (
    # Submodules that re-export stdlib modules under non-underscore names
    "f2py", "ctypeslib", "distutils", "testing",
    # The ``lib`` subpackage exposes npyio (save/load/memmap helpers)
    "lib", "compat",
    # File I/O functions that live directly on the numpy namespace
    "save", "savez", "savez_compressed", "savetxt",
    "load", "loadtxt", "genfromtxt", "fromregex",
    "fromfile", "memmap", "DataSource",
    # Utility surface that leaks internals
    "show_config", "get_include", "source", "lookfor", "info",
    "who", "disp", "deprecate",
)


@pytest.mark.parametrize("attr", _DANGEROUS_NUMPY_ATTRS)
def test_dangerous_numpy_attr_is_blocked(attr: str) -> None:
    """Defense-in-depth: every dangerous numpy attribute must be unreachable.

    The facade admits only names in the vetted allow-list; anything else
    — including any of the known stdlib-re-exporting submodules — must
    raise ``AttributeError`` on access.
    """
    code = (
        "def compute(series):\n"
        f"    _ = np.{attr}\n"
        "    return series['SPX']\n"
    )
    # Some of these (e.g. ``load``, ``save``) start with a non-underscore
    # character and pass static validation — they fail at exec via the
    # facade's ``AttributeError``. Others (none in this list today) may
    # be caught statically. Either is acceptable.
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


# --------------------------------------------------------------------------
# Sandbox globals hygiene — pandas / scipy / ctypes / os / subprocess must
# NOT be reachable from the sandbox namespace, regardless of what the host
# process has imported elsewhere.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "pandas",
        "pd",
        "scipy",
        "sp",
        "ctypes",
        "os",
        "sys",
        "subprocess",
        "socket",
        "pickle",
        "builtins",
        "importlib",
        "__loader__",
    ],
)
def test_forbidden_global_is_not_in_sandbox(forbidden: str) -> None:
    """Referencing a forbidden global from indicator code must fail.

    The sandbox globals dict is explicitly constructed in
    :func:`tcg.engine.indicator_exec._build_safe_globals` with only
    ``__builtins__`` / ``np`` (facade) / ``math``. Any other name must
    either be rejected by the AST walker (underscore names) or fail at
    exec with ``NameError`` (plain names not present in the dict).
    """
    # Names starting with underscore are rejected at validation time.
    # Others (pandas, scipy, …) fail at exec with NameError.
    code = (
        "def compute(series):\n"
        f"    return {forbidden}\n"
    )
    with pytest.raises((IndicatorValidationError, IndicatorRuntimeError)):
        run_indicator(code, {}, _series())


def test_facade_is_not_the_real_numpy_module() -> None:
    """Identity check: the ``np`` handed to user code is the facade, not numpy.

    If this ever regresses (e.g. someone 'just injects np again for
    convenience'), every other adversarial test above may still pass while
    the real module is silently reachable. This test fails fast in that
    case.
    """
    captured: dict[str, object] = {}
    code = (
        "def compute(series):\n"
        "    return series['SPX']\n"
    )
    # Build sandbox globals the way run_indicator does, inspect the type.
    from tcg.engine.indicator_exec import (
        _build_safe_globals,
        _NumpyFacade,
    )
    g = _build_safe_globals()
    captured["np"] = g["np"]
    assert isinstance(captured["np"], _NumpyFacade)
    assert captured["np"] is not np
    # Calling through run_indicator should still work — sanity.
    result = run_indicator(code, {}, _series())
    assert result.shape == (5,)
