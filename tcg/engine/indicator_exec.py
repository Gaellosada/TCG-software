"""Restricted execution sandbox for user-defined indicator code.

Pure engine module. Does NOT depend on ``tcg.core`` or ``tcg.data``.

Security model
--------------
User code is parsed with :mod:`ast`, then walked to reject:

* ``Import`` / ``ImportFrom`` nodes
* ``Global`` / ``Nonlocal`` nodes
* any ``Name`` or ``Attribute`` beginning with an underscore (blocks dunder
  access like ``obj.__class__`` or ``__builtins__``)
* any string literal (``ast.Constant`` with ``str`` value) beginning with
  ``_`` — blocks string-literal attribute traversal through e.g.
  ``getattr(x, '__class__')`` or ``operator.attrgetter('_x')``
* string literals containing a format attribute spec (``{...<name>.<attr>}``)
  — blocks ``"{0.__class__}".format(obj)`` info-disclosure
* ``ast.JoinedStr`` (f-strings) entirely — not needed for indicator math
  and they enable attribute traversal via ``f'{(1).__class__}'``
* ``Call`` to a handful of dangerous builtins (``eval``, ``exec``,
  ``compile``, ``__import__``, ``open``)

In addition to the above, the top-level ``compute`` function signature is
validated: the first positional argument must be named ``series`` (annotation
optional, or ``dict``), and every other argument MUST be annotated as one of
``int``/``float``/``bool`` with a matching-type default literal. ``*args``,
``**kwargs``, positional-only, and keyword-only arguments are rejected to
keep the calling convention uniform and auditable.

If the AST walk succeeds the code is compiled and executed with a
restricted ``__builtins__`` dict and only ``np`` / ``math`` pre-injected
as globals. ``np`` is a *curated facade* (see ``_NumpyFacade``), not the
real ``numpy`` module — exposing the real module would allow RCE via
``np.f2py.os.system(...)``, arbitrary file I/O via ``np.save`` /
``np.loadtxt``, and shared-library loading via ``np.ctypeslib``. The
facade exposes only a vetted allow-list of array-math symbols. The
user's module must define a top-level function named ``compute`` which
takes the series dict and returns a numpy array.

A 5-second wall-clock timeout is enforced with :mod:`signal.SIGALRM`
(Linux / WSL only — the project has no Windows support).
"""

from __future__ import annotations

import ast
import math
import re
import signal
import traceback
from typing import Any

import numpy as np
import numpy.typing as npt


# --- Public errors -----------------------------------------------------------


class IndicatorValidationError(ValueError):
    """User-supplied indicator code violates a static validation rule."""


class IndicatorRuntimeError(RuntimeError):
    """User-supplied indicator code failed at runtime (exception or timeout).

    Attributes
    ----------
    user_traceback:
        Sanitized traceback string containing only frames from inside the
        user's indicator code (filename ``<indicator>``).  Empty string when
        the error originated outside the user code (e.g. timeout).
    """

    def __init__(self, message: str, user_traceback: str = "") -> None:
        super().__init__(message)
        self.user_traceback: str = user_traceback


# --- AST validation ----------------------------------------------------------


_FORBIDDEN_CALL_NAMES: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "format"}
)


# Detect format-string attribute specs like ``{0.__class__}`` or
# ``{name.attr}``. We reject any string literal matching this because
# str.format (and %-formatting via ``__mod__``) performs attribute access
# on the argument, which otherwise bypasses the AST attribute whitelist.
_FORMAT_ATTR_RE: re.Pattern[str] = re.compile(r"\{[^}]*\.")


def _reject(node: ast.AST, message: str) -> IndicatorValidationError:
    """Build a validation error that includes the offending source line."""
    lineno = getattr(node, "lineno", None)
    if lineno is not None:
        return IndicatorValidationError(f"line {lineno}: {message}")
    return IndicatorValidationError(message)


def validate_code(source: str) -> ast.Module:
    """Parse *source* and reject disallowed constructs.

    Returns the parsed :class:`ast.Module` on success so callers can
    reuse it for compilation without re-parsing.
    """
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise IndicatorValidationError(f"syntax error: {exc.msg}") from exc

    for node in ast.walk(tree):
        # 1. Imports of any kind are forbidden.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise _reject(node, "import statements are not allowed")

        # 2. Global / nonlocal let code escape the sandbox's namespace rules.
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise _reject(node, "global/nonlocal declarations are not allowed")

        # 3. Block dunder / leading-underscore access.
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise _reject(
                node, f"names starting with '_' are not allowed: {node.id!r}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise _reject(
                node,
                f"attribute access starting with '_' is not allowed: "
                f".{node.attr}",
            )

        # 4. Reject f-strings outright. Users writing indicator math don't
        # need them, and they allow attribute access on formatted values
        # (e.g. ``f'{(1).__class__}'``) that bypasses the Attribute check.
        if isinstance(node, ast.JoinedStr):
            raise _reject(
                node,
                "f-strings are not allowed in indicator code",
            )

        # 5. Inspect all string literals universally.
        # This covers:
        #   * string-literal attribute access via hypothetical helpers
        #     (``getattr``/``hasattr``/``setattr``/``delattr``/
        #     ``operator.attrgetter`` on names starting with ``_``)
        #   * ``str.format`` / ``%``-format attribute traversal
        #     (``"{0.__class__}".format(x)`` or
        #     ``"%(_cls)s" % {"_cls": x}``)
        # Applying it universally (not just inside Call args) defends
        # against indirect routes such as a dict literal key being
        # unpacked back into a call via ``**``.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("_"):
                raise _reject(
                    node,
                    "Underscore/dunder references are not allowed in "
                    "indicator code",
                )
            if _FORMAT_ATTR_RE.search(node.value):
                raise _reject(
                    node,
                    "Format-string attribute specs (e.g. '{0.attr}') "
                    "are not allowed in indicator code",
                )

        # 6. Block a handful of dangerous direct calls.
        if isinstance(node, ast.Call):
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _FORBIDDEN_CALL_NAMES:
                raise _reject(node, f"call to '{name}' is not allowed")

    return tree


# --- Signature validation ----------------------------------------------------


_ALLOWED_ANNOTATION_TYPES: dict[str, type] = {
    "int": int,
    "float": float,
    "bool": bool,
}


def _find_compute_def(tree: ast.Module) -> ast.FunctionDef:
    """Locate the top-level ``def compute(...)`` function definition.

    Only top-level ``FunctionDef`` nodes (direct children of the module) are
    considered — a nested helper called ``compute`` does not count.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute":
            return node
    raise IndicatorValidationError(
        "code must define a top-level function named 'compute'"
    )


def _validate_compute_signature(func: ast.FunctionDef) -> dict[str, type]:
    """Validate the ``compute`` signature and return the typed-param map.

    Returns a dict mapping each declared parameter name (excluding ``series``)
    to its Python type (``int``, ``float``, or ``bool``). Raises
    :class:`IndicatorValidationError` otherwise.
    """
    args = func.args

    # Reject exotic argument shapes up front.
    if args.vararg is not None:
        raise _reject(
            func,
            "compute() must not use *args",
        )
    if args.kwarg is not None:
        raise _reject(
            func,
            "compute() must not use **kwargs",
        )
    if args.posonlyargs:
        raise _reject(
            func,
            "compute() must not declare positional-only arguments",
        )
    if args.kwonlyargs:
        raise _reject(
            func,
            "compute() must not declare keyword-only arguments",
        )

    positional = args.args
    if not positional:
        raise _reject(
            func,
            "compute() must accept a first positional 'series' argument",
        )

    # First positional argument: must be named 'series'. Annotation optional;
    # if present must be the plain Name 'dict'.
    first = positional[0]
    if first.arg != "series":
        raise _reject(
            func,
            f"compute() first argument must be named 'series', got {first.arg!r}",
        )
    if first.annotation is not None:
        if not (
            isinstance(first.annotation, ast.Name)
            and first.annotation.id == "dict"
        ):
            raise _reject(
                func,
                "compute() 'series' annotation, if present, must be 'dict'",
            )

    # 'series' must not have a default. ast.arguments.defaults applies to the
    # trailing args; trailing defaults align with the tail of positional.
    n_defaults = len(args.defaults)
    n_positional = len(positional)
    first_default_index = n_positional - n_defaults
    if first_default_index == 0 and n_positional > 0:
        # A default was given to every positional including 'series'.
        raise _reject(
            func,
            "compute() 'series' must not have a default value",
        )

    typed_params: dict[str, type] = {}

    for idx, arg in enumerate(positional[1:], start=1):
        name = arg.arg

        # Belt-and-braces: reject parameter names beginning with '_'. The
        # body walker already catches *references* to such names, but a param
        # named e.g. ``__class__`` could shadow a builtin lookup before the
        # walker checks call sites.
        if name.startswith("_"):
            raise _reject(
                func,
                f"compute() parameter names must not start with '_': "
                f"{name!r}",
            )

        if arg.annotation is None:
            raise _reject(
                func,
                f"compute() parameter {name!r} must be annotated as "
                f"int, float, or bool",
            )
        if not isinstance(arg.annotation, ast.Name):
            raise _reject(
                func,
                f"compute() parameter {name!r} annotation must be a bare name "
                f"(int, float, or bool)",
            )
        ann_id = arg.annotation.id
        if ann_id not in _ALLOWED_ANNOTATION_TYPES:
            raise _reject(
                func,
                f"compute() parameter {name!r} annotation must be "
                f"int, float, or bool; got {ann_id!r}",
            )

        # Must have a default. Because 'series' has no default and defaults
        # align to the tail, every non-'series' arg must appear at or after
        # first_default_index.
        if idx < first_default_index:
            raise _reject(
                func,
                f"compute() parameter {name!r} must have a default value",
            )
        default_node = args.defaults[idx - first_default_index]

        if not isinstance(default_node, ast.Constant):
            raise _reject(
                func,
                f"compute() parameter {name!r} default must be a literal "
                f"int/float/bool constant",
            )
        default_value = default_node.value
        expected_type = _ALLOWED_ANNOTATION_TYPES[ann_id]

        # Booleans are a subclass of int in Python; keep strict matching so
        # `window: int = True` is rejected.
        if ann_id == "bool":
            if not isinstance(default_value, bool):
                raise _reject(
                    func,
                    f"compute() parameter {name!r} annotated bool must have a "
                    f"bool default (True/False)",
                )
        elif ann_id == "int":
            if isinstance(default_value, bool) or not isinstance(
                default_value, int
            ):
                raise _reject(
                    func,
                    f"compute() parameter {name!r} annotated int must have an "
                    f"int default",
                )
        elif ann_id == "float":
            # Accept either int or float literal for a float annotation.
            if isinstance(default_value, bool) or not isinstance(
                default_value, (int, float)
            ):
                raise _reject(
                    func,
                    f"compute() parameter {name!r} annotated float must have "
                    f"a numeric default",
                )

        typed_params[name] = expected_type

    return typed_params


def _coerce_param_value(
    name: str, value: Any, expected_type: type
) -> int | float | bool:
    """Validate *value* against *expected_type* and return the coerced value.

    * bool annotation → only Python ``bool`` accepted.
    * int annotation → int, or float that is exactly integer-valued.
    * float annotation → int or float (never bool).
    """
    if expected_type is bool:
        if not isinstance(value, bool):
            raise IndicatorValidationError(
                f"param {name!r} expects bool, got {type(value).__name__}"
            )
        return value

    # For numeric annotations, reject bool explicitly (bool is an int subclass).
    if isinstance(value, bool):
        raise IndicatorValidationError(
            f"param {name!r} expects {expected_type.__name__}, got bool"
        )

    if expected_type is int:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise IndicatorValidationError(
                    f"param {name!r} expects int, got non-integer float "
                    f"{value!r}"
                )
            return int(value)
        raise IndicatorValidationError(
            f"param {name!r} expects int, got {type(value).__name__}"
        )

    if expected_type is float:
        if isinstance(value, (int, float)):
            return float(value)
        raise IndicatorValidationError(
            f"param {name!r} expects float, got {type(value).__name__}"
        )

    # Defensive — should be unreachable given the annotation whitelist.
    raise IndicatorValidationError(
        f"param {name!r} has unsupported expected type {expected_type!r}"
    )


# --- Sandbox globals ---------------------------------------------------------

# ### Design note
# The sandbox exposes a curated *facade* (``_NumpyFacade``) instead of the
# real ``numpy`` module. Rationale: numpy's public surface has non-underscore
# submodules that re-export stdlib (``np.f2py.os``, ``np.ctypeslib.ctypes``)
# and file I/O helpers (``np.save``, ``np.loadtxt``) that together give full
# RCE and arbitrary file read/write — the AST walker never fires because none
# of these use dunders. Blocking submodules by name (deny-list) is brittle:
# numpy adds/renames submodules across releases. Instead we invert the policy
# to an allow-list that mirrors the existing builtins whitelist, exposing
# only vetted array-math symbols. The facade is a plain object with a fixed
# ``__slots__``-style attribute set; attribute access to anything outside the
# whitelist raises ``AttributeError`` at exec time. This keeps the policy in
# one place (``_NUMPY_FACADE_NAMES``) and fails closed for any new numpy
# attribute. If a legitimate indicator needs a symbol not in the list, add it
# explicitly after reviewing its file-I/O / subprocess / ctypes exposure.


class _NumpyFacade:
    """Read-only proxy exposing a curated allow-list of numpy symbols.

    Only names in :data:`_NUMPY_FACADE_NAMES` are reachable. Every other
    attribute raises :class:`AttributeError` — including public submodules
    like ``f2py``, ``ctypeslib``, ``lib``, ``testing`` that would otherwise
    be RCE gadgets via their non-underscore re-exports of ``os`` /
    ``subprocess`` / ``ctypes``.
    """

    __slots__ = ("_allowed",)

    def __init__(self, allowed: dict[str, Any]) -> None:
        # Store as a private dict; access goes through __getattribute__.
        object.__setattr__(self, "_allowed", allowed)

    def __getattr__(self, name: str) -> Any:  # noqa: D401 — proxy
        allowed = object.__getattribute__(self, "_allowed")
        if name in allowed:
            return allowed[name]
        raise AttributeError(
            f"'np' facade has no attribute {name!r} — the indicator sandbox "
            f"exposes only a curated subset of numpy"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("'np' facade is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("'np' facade is read-only")

    def __repr__(self) -> str:
        return "<np facade (sandboxed)>"


# Curated allow-list. Every entry must be a pure array-math helper with
# NO side effects on the filesystem, NO subprocess execution, NO ctypes
# access, and NO import side effects. Grouped for review clarity.
#
# Explicitly EXCLUDED (RCE / arbitrary I/O gadgets — do not add back):
#   f2py, ctypeslib, lib, testing, distutils, compat, core, random
#   (``random.BitGenerator`` is fine but the submodule surface isn't worth
#   vetting — if users want RNG, expose ``np.random.default_rng`` alone
#   after review), memmap, DataSource, load, save, savez, savez_compressed,
#   savetxt, loadtxt, genfromtxt, fromfile, fromstring, frombuffer, tofile,
#   show_config, get_include, source, lookfor, info, who, disp, deprecate,
#   seterr, geterr, seterrcall, setbufsize.
_NUMPY_FACADE_NAMES: tuple[str, ...] = (
    # Array creation
    "array", "asarray", "asanyarray", "copy",
    "zeros", "ones", "full", "empty",
    "zeros_like", "ones_like", "full_like", "empty_like",
    "arange", "linspace", "logspace", "geomspace",
    "eye", "identity", "diag", "tri", "tril", "triu",
    # Constants
    "nan", "inf", "pi", "e", "euler_gamma", "newaxis",
    # Dtypes
    "float64", "float32", "float16",
    "int64", "int32", "int16", "int8",
    "uint64", "uint32", "uint16", "uint8",
    "bool_", "dtype", "integer", "floating", "number",
    # Shape / layout
    # Note: `flatten` is an ndarray method, not a numpy top-level
    # attribute — listing it would leave a dead entry that the facade
    # silently skips. Users call `arr.flatten()` directly.
    "reshape", "ravel", "transpose", "swapaxes",
    "expand_dims", "squeeze", "broadcast_to", "broadcast_arrays",
    "concatenate", "stack", "vstack", "hstack", "dstack", "column_stack",
    "split", "array_split", "hsplit", "vsplit",
    "tile", "repeat", "flip", "fliplr", "flipud", "roll",
    "atleast_1d", "atleast_2d", "atleast_3d",
    # Element-wise math
    "add", "subtract", "multiply", "divide", "true_divide", "floor_divide",
    "mod", "remainder", "power", "negative", "positive",
    "abs", "absolute", "fabs", "sign", "reciprocal",
    "sqrt", "cbrt", "square", "exp", "exp2", "expm1",
    "log", "log2", "log10", "log1p",
    "sin", "cos", "tan", "arcsin", "arccos", "arctan", "arctan2",
    "sinh", "cosh", "tanh", "arcsinh", "arccosh", "arctanh",
    "degrees", "radians", "deg2rad", "rad2deg",
    "floor", "ceil", "trunc", "rint", "round", "around",
    "clip", "maximum", "minimum", "fmax", "fmin",
    "hypot", "copysign",
    # Logical / comparison
    "where", "select", "piecewise",
    "isnan", "isinf", "isfinite", "isneginf", "isposinf",
    "isclose", "allclose", "array_equal", "array_equiv",
    "equal", "not_equal", "less", "less_equal", "greater", "greater_equal",
    "logical_and", "logical_or", "logical_not", "logical_xor",
    "any", "all",
    # Reductions / statistics
    "sum", "prod", "cumsum", "cumprod",
    "mean", "std", "var", "median", "average",
    "min", "max", "amin", "amax", "ptp",
    "argmin", "argmax", "argsort", "sort", "lexsort", "partition",
    "percentile", "quantile",
    "nansum", "nanprod", "nancumsum", "nancumprod",
    "nanmean", "nanstd", "nanvar", "nanmedian",
    "nanmin", "nanmax", "nanargmin", "nanargmax", "nanpercentile",
    "nanquantile",
    "count_nonzero", "nonzero",
    "unique", "searchsorted",
    # Indexing helpers
    "take", "put", "choose", "compress",
    "indices", "ix_", "r_", "c_", "meshgrid",
    # Sliding / signal
    "diff", "ediff1d", "gradient", "convolve", "correlate",
    "pad", "interp",
    # Dot / reductions-over-axes
    "dot", "vdot", "inner", "outer", "matmul", "tensordot", "einsum",
    "cross", "trace",
    # Type-checks / casting
    "isscalar", "ndim", "shape", "size",
    "result_type", "can_cast", "promote_types",
)


_MISSING = object()


def _build_numpy_facade() -> _NumpyFacade:
    """Materialise the ``np`` facade from the allow-list.

    Each name is resolved once against the real numpy module. If a name is
    missing (numpy API drift), we skip it silently rather than blow up
    import — the test suite is responsible for catching drift that removes
    a name the default indicators rely on.
    """
    allowed: dict[str, Any] = {}
    for name in _NUMPY_FACADE_NAMES:
        value = getattr(np, name, _MISSING)
        if value is _MISSING:
            continue
        allowed[name] = value
    return _NumpyFacade(allowed)


_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "abs",
    "min",
    "max",
    "sum",
    "len",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "list",
    "tuple",
    "dict",
    "set",
    "float",
    "int",
    "bool",
    "round",
    "pow",
    "True",
    "False",
    "None",
    "isinstance",
    # NOTE: `getattr` was removed after iter 1 — it is the main vector for
    # the ``getattr(obj, '__class__')`` → ``__subclasses__`` → RCE escape,
    # and user indicator code has no legitimate need for dynamic attribute
    # lookup. Same reasoning excludes ``hasattr`` / ``setattr`` / ``delattr``.
    # ADDED beyond the brief's explicit whitelist: `iter` and `next` are
    # required by the example indicator in the contract (which uses
    # ``next(iter(series.values()))``), and are safe — they cannot escape
    # the sandbox on their own.
    "iter",
    "next",
)


def _build_safe_builtins() -> dict[str, Any]:
    """Collect the small whitelist of builtins user code may use."""
    # ``True``, ``False``, ``None`` are literal constants in Python 3; we
    # still expose them by name for completeness.
    safe: dict[str, Any] = {}
    builtins_module: Any = __builtins__
    if isinstance(builtins_module, dict):
        getter = builtins_module.__getitem__
    else:  # module object (normal case outside of restricted exec)
        getter = lambda n: getattr(builtins_module, n)
    for name in _SAFE_BUILTIN_NAMES:
        safe[name] = getter(name)
    return safe


def _build_safe_globals() -> dict[str, Any]:
    """Sandbox globals: restricted builtins + numpy facade + math.

    Only three names enter the sandbox namespace: ``__builtins__`` (a
    curated dict), ``np`` (the facade — NOT the real numpy module), and
    ``math`` (the stdlib math module, which is pure array-math with no
    filesystem / subprocess / ctypes surface). ``pandas``, ``scipy``,
    ``ctypes``, ``os``, ``sys``, ``subprocess``, etc. are never placed
    here regardless of what the parent process has imported elsewhere.
    """
    return {
        "__builtins__": _build_safe_builtins(),
        "np": _build_numpy_facade(),
        "math": math,
    }


# --- Timeout -----------------------------------------------------------------


class _TimeoutContext:
    """Wall-clock timeout via ``SIGALRM`` (Linux/WSL only).

    If signals are unavailable (non-main thread, Windows), the context
    silently skips the alarm. Callers can still rely on validation.
    """

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._previous: Any = None
        self._installed = False

    def __enter__(self) -> "_TimeoutContext":
        if not hasattr(signal, "SIGALRM"):
            return self
        try:
            self._previous = signal.signal(signal.SIGALRM, self._on_alarm)
            signal.alarm(self._seconds)
            self._installed = True
        except (ValueError, OSError):
            # Not in main thread — skip the alarm; validation still applies.
            self._installed = False
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._installed:
            return
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._previous)

    @staticmethod
    def _on_alarm(signum: int, frame: Any) -> None:
        raise IndicatorRuntimeError("Indicator exceeded time limit")


# --- Result coercion ---------------------------------------------------------


def _coerce_to_float_array(
    value: Any, expected_length: int
) -> npt.NDArray[np.float64]:
    """Validate and coerce *value* to a float numpy array of the right length.

    Accepts ``np.ndarray`` of numeric dtype or a list/tuple of numbers.
    Rejects strings, dicts, scalars, and mismatched lengths.
    """
    if isinstance(value, str) or isinstance(value, (bytes, dict, set)):
        raise IndicatorRuntimeError(
            f"indicator returned {type(value).__name__!r}; expected a "
            f"numeric array of length {expected_length}"
        )

    if isinstance(value, np.ndarray):
        arr = value
    elif isinstance(value, (list, tuple)):
        try:
            arr = np.asarray(value, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise IndicatorRuntimeError(
                f"indicator return value could not be converted to a "
                f"float array: {exc}"
            ) from exc
    else:
        raise IndicatorRuntimeError(
            f"indicator returned {type(value).__name__!r}; expected a "
            f"numpy array or list of floats"
        )

    if arr.ndim != 1:
        raise IndicatorRuntimeError(
            f"indicator must return a 1-D array, got shape {arr.shape}"
        )
    if arr.shape[0] != expected_length:
        raise IndicatorRuntimeError(
            f"indicator returned length {arr.shape[0]}; expected "
            f"{expected_length}"
        )

    if arr.dtype.kind not in ("f", "i", "u", "b"):
        # object / complex / string dtypes → reject
        raise IndicatorRuntimeError(
            f"indicator returned array of dtype {arr.dtype}; expected a "
            f"numeric dtype"
        )
    if arr.dtype != np.float64:
        arr = arr.astype(np.float64)

    return arr


# --- Traceback sanitization --------------------------------------------------


_USER_FILENAME = "<indicator>"

# Regex that matches absolute POSIX paths in an exception message.
# Requirements:
#   * leading "/" must not be preceded by ":" (excludes URL schemes like
#     http://, file://),
#   * at least two path segments ("/word/word...") so single-slash tokens
#     like "1/2" (fractions) and "2026/04/18" (dates) are preserved,
#   * segments are composed of word chars, dot, or dash — trailing
#     punctuation (comma, semicolon) stops the match cleanly.
# Relative paths ("../foo", "./foo") are preserved by design — they are
# not absolute filesystem paths and typically don't leak install layout.
_ABS_PATH_RE: re.Pattern[str] = re.compile(
    r"(?<![:/\w.])/(?:[\w.-]+/)+[\w.-]+"
)


def _sanitize_message(message: str) -> str:
    """Strip absolute filesystem paths from an exception message string.

    Replaces absolute POSIX paths (``/foo/bar/baz``) with ``<path>`` so
    internal directory structure does not leak to the client. URL tails
    (``http://host/api``), single-slash tokens (fractions, dates), and
    relative paths are left untouched.
    """
    return _ABS_PATH_RE.sub("<path>", message)


def _sanitize_traceback(exc: BaseException) -> str:
    """Extract only the user-code frames from *exc*'s traceback.

    Keeps frames whose ``filename`` is ``<indicator>`` (the name passed to
    ``compile()``).  Internal TCG frames and stdlib frames are stripped, so
    no OS paths or internal module names leak to the client.

    Returns the formatted string (may be empty if no user-code frames are
    present, e.g. for a timeout signal).
    """
    tb = exc.__traceback__
    frames: list[traceback.FrameSummary] = []
    for frame_summary in traceback.extract_tb(tb):
        if frame_summary.filename == _USER_FILENAME:
            frames.append(frame_summary)

    if not frames:
        return ""

    lines: list[str] = ["Traceback (indicator code):\n"]
    lines.extend(traceback.StackSummary.from_list(frames).format())
    # Append the exception type + message as the final line. The message
    # must be routed through _sanitize_message — otherwise a KeyError
    # containing a path (e.g. ``KeyError: '/home/alice/secret'``) leaks
    # the path via the traceback envelope.
    lines.append(f"{type(exc).__name__}: {_sanitize_message(str(exc))}\n")
    return "".join(lines)


# --- Main entry point --------------------------------------------------------


TIMEOUT_SECONDS = 5


def run_indicator(
    code: str,
    params: dict[str, float | int | bool],
    series_dict: dict[str, npt.NDArray[np.float64]],
    *,
    timeout: int = TIMEOUT_SECONDS,
) -> npt.NDArray[np.float64]:
    """Execute a user-defined indicator against one or more price series.

    Parameters
    ----------
    code:
        Python source that must define a top-level ``compute`` function
        whose first positional argument is ``series``; every other argument
        must be annotated as ``int``, ``float``, or ``bool`` with a
        matching-type default literal.
    params:
        Keyword arguments forwarded to ``compute``. The set of keys must
        exactly match the non-``series`` parameters of the signature; each
        value is type-checked against the declared annotation.
    series_dict:
        Mapping of series label → 1-D float price array. All arrays must
        have the same length.

    Returns
    -------
    ``np.ndarray`` of dtype ``float64`` and length equal to the common
    length of the input series.

    Raises
    ------
    IndicatorValidationError
        If the code fails static validation or does not define ``compute``.
    IndicatorRuntimeError
        If execution fails, times out, or returns the wrong shape/type.
    """
    if not series_dict:
        raise IndicatorValidationError(
            "series_dict must contain at least one price series"
        )

    lengths = {label: arr.shape[0] for label, arr in series_dict.items()}
    unique_lengths = set(lengths.values())
    if len(unique_lengths) != 1:
        raise IndicatorValidationError(
            f"all series must have the same length; got {lengths}"
        )
    expected_length = unique_lengths.pop()

    tree = validate_code(code)

    # Locate and validate compute()'s typed signature.
    compute_def = _find_compute_def(tree)
    typed_params = _validate_compute_signature(compute_def)

    # Enforce exact kwargs match between `params` dict and the signature.
    declared = set(typed_params.keys())
    supplied = set(params.keys())
    extras = supplied - declared
    missing = declared - supplied
    if extras:
        raise IndicatorValidationError(
            f"unexpected parameter(s): {sorted(extras)!r}; "
            f"compute() accepts {sorted(declared)!r}"
        )
    if missing:
        raise IndicatorValidationError(
            f"missing parameter(s): {sorted(missing)!r}; "
            f"compute() requires {sorted(declared)!r}"
        )

    coerced: dict[str, int | float | bool] = {}
    for name, value in params.items():
        coerced[name] = _coerce_param_value(name, value, typed_params[name])

    try:
        compiled = compile(tree, filename="<indicator>", mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise IndicatorValidationError(
            f"failed to compile indicator: {exc}"
        ) from exc

    safe_globals = _build_safe_globals()
    safe_locals: dict[str, Any] = {}

    with _TimeoutContext(timeout):
        try:
            exec(compiled, safe_globals, safe_locals)
        except IndicatorRuntimeError:
            raise
        except MemoryError:
            raise
        except Exception as exc:
            # Why: any user-code exception (TypeError, ValueError, NameError,
            # etc.) during module-level exec should be reported as a runtime
            # error rather than crashing the worker.  MemoryError is re-raised
            # above because exhausting memory is a process-level condition, not
            # a user indicator bug.  KeyboardInterrupt and SystemExit are
            # BaseException subclasses and are therefore not caught here.
            raise IndicatorRuntimeError(
                f"error loading indicator: {type(exc).__name__}: "
                f"{_sanitize_message(str(exc))}",
                user_traceback=_sanitize_traceback(exc),
            ) from exc

        compute_fn = safe_locals.get("compute")
        if compute_fn is None:
            compute_fn = safe_globals.get("compute")
        if compute_fn is None or not callable(compute_fn):
            raise IndicatorValidationError(
                "code must define a top-level function named 'compute'"
            )

        try:
            result = compute_fn(series_dict, **coerced)
        except IndicatorRuntimeError:
            raise
        except MemoryError:
            raise
        except Exception as exc:
            # Why: any user-code exception raised inside compute() should
            # surface as a structured runtime error with the sanitized
            # traceback.  MemoryError is re-raised above.
            raise IndicatorRuntimeError(
                f"indicator raised {type(exc).__name__}: "
                f"{_sanitize_message(str(exc))}",
                user_traceback=_sanitize_traceback(exc),
            ) from exc

    return _coerce_to_float_array(result, expected_length)


__all__ = [
    "IndicatorRuntimeError",
    "IndicatorValidationError",
    "TIMEOUT_SECONDS",
    "run_indicator",
    "validate_code",
]
