"""Compile workspace scripts -> Jupyter notebook + emit JSON manifest."""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np


_PREFIX_RE = re.compile(r"^(\d{2})_")

# Sentinel env var set inside the cell-execution kernel so a recursive call to
# `compile_workspace` from inside a compiled cell raises a clear error instead
# of running the asyncio loop into a cryptic re-entry / nested-event-loop
# crash. See `pipeline/05-report.md` § "No recursive compile".
_REENTRY_ENV_VAR = "TCG_BACKTESTER_COMPILE_ACTIVE"


_PROJECT_KERNEL_NAME = "tcg-backtester"
_PROJECT_KERNEL_DISPLAY = "Python (tcg-backtester venv)"


def _resolve_kernel() -> tuple[str, str]:
    """Prefer the project venv kernel `tcg-backtester` if registered; else fall back to `python3`.

    The project kernel is registered once per machine via:
        <venv>/bin/python -m ipykernel install --user --name tcg-backtester --display-name "Python (tcg-backtester venv)"
    See README. Fallback keeps fresh clones working before that one-time setup.
    """
    try:
        from jupyter_client.kernelspec import KernelSpecManager  # type: ignore[import-untyped]
    except Exception:
        return "python3", "Python 3"
    try:
        specs = KernelSpecManager().find_kernel_specs()
    except Exception:
        return "python3", "Python 3"
    if _PROJECT_KERNEL_NAME in specs:
        return _PROJECT_KERNEL_NAME, _PROJECT_KERNEL_DISPLAY
    return "python3", "Python 3"


_SYS_PATH_BOOTSTRAP = '''\
# --- notebook bootstrap: makes this notebook robust to kernel cwd / env state ---
# 1. Adds the backtester repo root to sys.path so `from tcg_backtester.lib import …`
#    works regardless of whether the package is editable-installed in this kernel.
# 2. Changes cwd to the strategy workspace dir (containing strategy.py) so
#    `Path(".") / "data" / "X.npz"` resolves the same way under Jupyter as under nbclient.
# 3. Applies nest_asyncio so `sync_run` / `asyncio.run` work inside Jupyter\'s
#    already-running event loop. Skipped if nest_asyncio is unavailable.
import os
import sys
from pathlib import Path

_here = Path.cwd().resolve()
for _p in [_here, *_here.parents]:
    if (_p / "tcg_backtester" / "__init__.py").is_file() and (_p / "lib").is_dir():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

_search = []
_nb = globals().get("__vsc_ipynb_file__") or globals().get("__file__")
if _nb:
    _search.append(Path(_nb).resolve().parent)
_search.append(_here)
for _start in _search:
    for _q in [_start, *_start.parents]:
        if (_q / "strategy.py").is_file() or (_q / "strategy" / "__init__.py").is_file():
            if Path.cwd().resolve() != _q:
                os.chdir(_q)
            break
    else:
        continue
    break

try:
    import nest_asyncio  # type: ignore[import-untyped]

    nest_asyncio.apply()
except ImportError:
    pass

# 4. Provide a `__file__` stub for cells copied from `scripts/*.py`. Compiled
#    notebook cells have no inherent `__file__`, so any `Path(__file__).parent`
#    pattern crashes with NameError. We expose a synthetic value pointing at a
#    pseudo `scripts/notebook_cell.py` inside the workspace cwd, so anchors
#    like `Path(__file__).resolve().parent.parent` still resolve to the
#    workspace dir. Prefer `Path.cwd()` in new scripts.
if "__file__" not in globals():
    __file__ = str(Path.cwd() / "scripts" / "notebook_cell.py")
'''


# ---------------------------------------------------------------------------
# Hand-rolled report-schema validator (P0-K).
#
# Rationale: `jsonschema` is not a project dependency. We walk
# `templates/report-schema.json` for `required` + `type` checks at the levels
# the schema actually uses (top-level, nested object properties, array item
# objects). We do NOT support `oneOf` / `anyOf` / `pattern` / `format` — those
# are richer checks served by the test-suite-only `pytest.importorskip`
# hook in `tests/test_compile.py`.
# ---------------------------------------------------------------------------

_REPORT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "templates" / "report-schema.json"
_REPORT_SCHEMA: dict[str, Any] | None = None


def _load_report_schema() -> dict[str, Any]:
    """Cache and return the report schema loaded from `templates/report-schema.json`."""
    global _REPORT_SCHEMA
    if _REPORT_SCHEMA is None:
        _REPORT_SCHEMA = json.loads(_REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _REPORT_SCHEMA


_PY_BY_JSON_TYPE: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a single-step `$ref` like `#/properties/metrics`. Returns the schema unchanged
    when no $ref is present."""
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if not ref or not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    cur: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            raise ValueError(f"manifest schema violation: cannot resolve $ref {ref!r}")
        cur = cur[part]
    if not isinstance(cur, dict):
        raise ValueError(f"manifest schema violation: $ref {ref!r} did not resolve to an object")
    return cur


def _check_type(value: Any, type_spec: Any, path: str) -> str | None:
    """Return an error message if `value` violates `type_spec`, else None."""
    if type_spec is None:
        return None
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    for t in types:
        if t not in _PY_BY_JSON_TYPE:
            # Unknown type label: treat as pass; stricter checks live in the
            # optional jsonschema-backed test.
            return None
    # numbers vs booleans: exclude bool from number/integer
    for t in types:
        py_types = _PY_BY_JSON_TYPE[t]
        if t in ("number", "integer"):
            if isinstance(value, bool):
                continue  # bool is not number per JSON Schema
            if isinstance(value, py_types):
                return None
        else:
            if isinstance(value, py_types):
                return None
    return f"type mismatch: expected {type_spec!r}, got {type(value).__name__!r} at {path}"


def _walk(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> str | None:
    """Recursively check `value` against `schema`. First failure returns its message."""
    schema = _resolve_ref(schema, root)
    type_spec = schema.get("type")
    err = _check_type(value, type_spec, path)
    if err:
        return err

    # Object branch — required + properties recursion.
    if isinstance(value, dict):
        for req in schema.get("required", []) or []:
            if req not in value:
                return f"missing required field {req!r} at {path or '/'}"
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in value:
                child_path = f"{path}.{k}" if path else k
                err = _walk(value[k], sub, root, child_path)
                if err:
                    return err
        # additionalProperties: when defined as a schema, validate every key
        # not already covered by `properties`. The schema uses this for
        # leg_metrics, leg_equities, raw_leg_equities, plot_paths, etc.,
        # often with a $ref — _resolve_ref handles that on the recursive call.
        add_props = schema.get("additionalProperties")
        if isinstance(add_props, dict):
            declared = set(schema.get("properties") or {})
            for k, v in value.items():
                if k in declared:
                    continue
                child_path = f"{path}.{k}" if path else k
                err = _walk(v, add_props, root, child_path)
                if err:
                    return err

    # Array branch — items recursion (only when items has a schema).
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                err = _walk(item, items, root, f"{path}[{i}]")
                if err:
                    return err

    return None


def _validate_manifest_or_raise(manifest: dict) -> None:
    """Raise `ValueError` if the manifest fails `templates/report-schema.json`.

    Walks `required` + `type` at every level; nested objects via
    `properties`, arrays via `items`. First failure raises.
    """
    schema = _load_report_schema()
    err = _walk(manifest, schema, schema, "")
    if err:
        raise ValueError(f"manifest schema violation: {err}")


# ---------------------------------------------------------------------------
# Plot-set declarations.
#
# Closed strategy_class -> plot-set mapping is gone (Sign 1). The code-first
# surface uses ``lib.plotting.BASELINE_PLOTS`` plus the strategy module's
# optional ``EXTRA_PLOTS`` list, enumerated by :func:`render_extra_plots`.
# ---------------------------------------------------------------------------


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if is_dataclass(obj) and not isinstance(obj, type):
        return _scrub(asdict(obj))
    return obj


def _yyyymmdd_to_iso(d: int) -> str:
    n = int(d)
    return f"{n // 10000:04d}-{(n // 100) % 100:02d}-{n % 100:02d}"


def _build_plot_render_cells(
    workspace_dir: Path,
    presentation_sources: list[str],
) -> list:
    """Append a markdown header + one ``pio.from_json(...).show()`` cell for
    every ``results/plots/*.json`` not already referenced in the presentation
    scripts. Idempotent: cells the agent already wrote take priority.

    The agent's manifest-emit script frequently doesn't render plots inline,
    leaving the user-facing notebook with raw file paths. Auto-injecting these
    cells is a safety net so the deliverable always shows the plots.
    """
    import nbformat

    plots_dir = workspace_dir / "results" / "plots"
    if not plots_dir.is_dir():
        return []
    joined = "\n".join(presentation_sources)
    new_cells: list = []
    seen_header = False
    for plot_path in sorted(plots_dir.glob("*.json")):
        rel = f"results/plots/{plot_path.name}"
        # Skip if any presentation cell already references this plot file.
        if rel in joined or plot_path.stem in _extract_plot_ids(joined):
            continue
        if not seen_header:
            new_cells.append(nbformat.v4.new_markdown_cell(
                source=f"## Plots\n\nAuto-rendered from `results/plots/`."
            ))
            seen_header = True
        plot_id = plot_path.stem
        new_cells.append(nbformat.v4.new_markdown_cell(source=f"### {plot_id}"))
        new_cells.append(nbformat.v4.new_code_cell(source=(
            f"# <!-- PLOT:{plot_id} -->\n"
            f"import plotly.io as pio\n"
            f"fig = pio.from_json(open({rel!r}).read())\n"
            f"fig.show()\n"
        )))
    return new_cells


_PLOT_MARKER_RE = re.compile(r"<!--\s*PLOT:(\w+)\s*-->")


def _extract_plot_ids(text: str) -> set[str]:
    """Return the set of plot-ids referenced via ``<!-- PLOT:foo -->`` markers."""
    return set(_PLOT_MARKER_RE.findall(text))


def compile_workspace(
    workspace_dir: Path,
    *,
    template_path: Path | None = None,
    execute: bool = True,
    on_error: Literal["abort", "continue"] = "abort",
    timeout_per_cell_s: int = 120,
    presentation_min_prefix: str = "05",
    auto_render_plots: bool = True,
) -> Path:
    """Run pipeline scripts to produce artifacts, then compile the presentation scripts into the notebook.

    Scripts under `scripts/` with 2-digit prefix < ``presentation_min_prefix`` are
    executed once via nbclient on a throwaway notebook (so artifacts like
    ``results/raw_result.pkl``, ``results/metrics.json``, ``results/plots/*.json``
    land on disk) but are NOT included in the final notebook. Scripts with prefix
    >= ``presentation_min_prefix`` are concatenated into the user-facing
    ``results/notebook.ipynb`` — those should load the previously-saved artifacts
    and display them inline (no save logic in the rendered notebook).

    Default ``presentation_min_prefix="05"`` keeps 01–04 as the pipeline (data,
    backtest, analyze) and 05+ as the presentation/report. Pass ``"00"`` to
    restore legacy behavior (everything compiles into the notebook).

    Re-entry: calling ``compile_workspace`` from inside a cell that is itself
    being executed by ``compile_workspace`` is fatal — the inner call would
    spawn a second nbclient kernel inside an already-running asyncio loop. The
    function detects this via the ``TCG_BACKTESTER_COMPILE_ACTIVE`` env-var
    sentinel and raises ``RuntimeError`` with a clear message. The canonical
    pipeline pattern is: ``scripts/05_*.py`` only emits the manifest;
    ``compile_workspace`` is invoked by a top-level driver outside ``scripts/``
    (see ``snippets/compile_notebook.py``).

    Atomic write: the notebook is written to ``results/notebook.ipynb.tmp``
    first, then renamed to ``results/notebook.ipynb`` on successful execution.
    On failure the ``.tmp`` is removed and the canonical path is left
    unchanged (or absent). The previous run's notebook is not clobbered by a
    half-finished compile.

    When ``auto_render_plots=True`` (default), any ``results/plots/*.json``
    not already referenced by a ``<!-- PLOT:<id> -->`` marker or relative path
    in the presentation scripts gets an auto-injected
    ``pio.from_json(...).show()`` cell appended to the notebook. This protects
    against the failure mode where the agent saves plots to disk but forgets
    to add the corresponding render cells.

    Returns path to the written notebook. Lexicographic 2-digit prefix ordering.
    """
    if os.environ.get(_REENTRY_ENV_VAR) == "1":
        raise RuntimeError(
            "compile_workspace re-entry detected: this call originated from "
            "inside a cell that is itself being executed by compile_workspace. "
            "Move the outer call to a top-level driver outside `scripts/` "
            "(e.g. via `snippets/compile_notebook.py`); `scripts/05_*.py` "
            "should only emit the manifest, never call compile_workspace."
        )

    import jupytext
    import nbformat
    from nbclient import NotebookClient

    workspace_dir = Path(workspace_dir)
    scripts_dir = workspace_dir / "scripts"
    if not scripts_dir.is_dir():
        raise FileNotFoundError(f"scripts dir missing: {scripts_dir}")
    scripts = sorted(scripts_dir.glob("*.py"))
    if not scripts:
        raise FileNotFoundError(f"no scripts found in {scripts_dir}")

    seen_prefix: dict[str, Path] = {}
    for s in scripts:
        m = _PREFIX_RE.match(s.name)
        if not m:
            raise ValueError(f"script missing 2-digit prefix: {s.name}")
        if m.group(1) in seen_prefix:
            raise ValueError(f"duplicate script prefix {m.group(1)}: {seen_prefix[m.group(1)].name} and {s.name}")
        seen_prefix[m.group(1)] = s

    pipeline_scripts = [s for s in scripts if _PREFIX_RE.match(s.name).group(1) < presentation_min_prefix]
    presentation_scripts = [s for s in scripts if _PREFIX_RE.match(s.name).group(1) >= presentation_min_prefix]

    # Backward-compat fallback: if no scripts are >= presentation_min_prefix,
    # treat them all as presentation (legacy behavior — every script compiles).
    if not presentation_scripts:
        presentation_scripts = scripts
        pipeline_scripts = []

    kernel_name, kernel_display = _resolve_kernel()

    out = workspace_dir / "results" / "notebook.ipynb"
    out_tmp = out.with_suffix(out.suffix + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out_tmp.exists():
        out_tmp.unlink()

    # Set the re-entry sentinel in the env passed to nbclient kernels. nbclient
    # spawns a fresh process per kernel so we set os.environ here; restored in
    # the finally block below.
    prior_sentinel = os.environ.get(_REENTRY_ENV_VAR)
    os.environ[_REENTRY_ENV_VAR] = "1"
    try:
        # Phase 1: run pipeline scripts on a throwaway notebook so their saves happen.
        if pipeline_scripts and execute:
            pipeline_cells: list = [nbformat.v4.new_code_cell(source=_SYS_PATH_BOOTSTRAP)]
            for s in pipeline_scripts:
                nb = jupytext.read(str(s), fmt="py:percent")
                pipeline_cells.extend(nb.cells)
            pipeline_nb = nbformat.v4.new_notebook()
            pipeline_nb.cells = pipeline_cells
            pipeline_nb.metadata["kernelspec"] = {
                "name": kernel_name,
                "display_name": kernel_display,
                "language": "python",
            }
            pipeline_client = NotebookClient(
                pipeline_nb,
                timeout=timeout_per_cell_s,
                kernel_name=kernel_name,
                allow_errors=(on_error == "continue"),
                resources={"metadata": {"path": str(workspace_dir)}},
            )
            try:
                pipeline_client.execute()
            except Exception:
                partial = workspace_dir / "results" / "pipeline.partial.ipynb"
                partial.parent.mkdir(parents=True, exist_ok=True)
                nbformat.write(pipeline_nb, str(partial))
                raise

        # Phase 2: compile presentation scripts into the user-facing notebook.
        cells: list = [nbformat.v4.new_code_cell(source=_SYS_PATH_BOOTSTRAP)]
        # Embed strategy.py source verbatim (code-first audit trail).
        cells.extend(_strategy_source_cells(workspace_dir, nbformat))
        presentation_sources: list[str] = []
        for s in presentation_scripts:
            nb = jupytext.read(str(s), fmt="py:percent")
            cells.extend(nb.cells)
            for c in nb.cells:
                src = c.source if isinstance(c.source, str) else "".join(c.source or [])
                presentation_sources.append(src)

        # Auto-inject plot rendering cells for any plot file not already
        # referenced by the presentation scripts. Robust against agents that
        # save plots in P4 but forget to render them in P5.
        if auto_render_plots:
            cells.extend(_build_plot_render_cells(workspace_dir, presentation_sources))

        notebook = nbformat.v4.new_notebook()
        notebook.cells = cells
        notebook.metadata["kernelspec"] = {"name": kernel_name, "display_name": kernel_display, "language": "python"}
        notebook.metadata["tcg_backtester"] = {
            "compiled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "scripts": [s.name for s in scripts],
            "pipeline_scripts": [s.name for s in pipeline_scripts],
            "presentation_scripts": [s.name for s in presentation_scripts],
            "on_error": on_error,
        }

        if execute:
            client = NotebookClient(
                notebook,
                timeout=timeout_per_cell_s,
                kernel_name=kernel_name,
                allow_errors=(on_error == "continue"),
                resources={"metadata": {"path": str(workspace_dir)}},
            )
            try:
                client.execute()
            except Exception:
                # Atomic-write contract: don't clobber the canonical path.
                # Drop a `.partial.ipynb` for inspection, clean up the tmp,
                # and re-raise.
                partial = workspace_dir / "results" / "notebook.partial.ipynb"
                partial.parent.mkdir(parents=True, exist_ok=True)
                nbformat.write(notebook, str(partial))
                if out_tmp.exists():
                    out_tmp.unlink()
                raise

        # Atomic write: stage to .tmp, then rename. os.replace is atomic on
        # POSIX and Windows when source and dest are on the same filesystem.
        nbformat.write(notebook, str(out_tmp))
        os.replace(str(out_tmp), str(out))
        return out
    finally:
        # Restore prior sentinel state.
        if prior_sentinel is None:
            os.environ.pop(_REENTRY_ENV_VAR, None)
        else:
            os.environ[_REENTRY_ENV_VAR] = prior_sentinel
        # Defensive cleanup: a half-written tmp from a write-time failure must
        # not survive the call.
        if out_tmp.exists():
            try:
                out_tmp.unlink()
            except OSError:
                pass


def _discover_iteration_snapshots(workspace_dir: Path) -> list[dict[str, Any]]:
    """Find prior `results/iter_<N>/manifest.json` snapshots and emit
    iteration records keyed by their on-disk path. The current run
    (`results/manifest.json`) is NOT included here — callers append the
    current iteration record separately, with `path: null` until a snapshot
    is later taken.
    """
    results_dir = Path(workspace_dir) / "results"
    if not results_dir.is_dir():
        return []
    records: list[tuple[int, dict[str, Any]]] = []
    for sub in results_dir.iterdir():
        if not sub.is_dir():
            continue
        m = re.fullmatch(r"iter_(\d+)", sub.name)
        if not m:
            continue
        snap = sub / "manifest.json"
        if not snap.is_file():
            continue
        n = int(m.group(1))
        rel_path = f"results/{sub.name}/manifest.json"
        ts: str | None = None
        summary = ""
        scope: list[str] = []
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
            ts = data.get("run_timestamp")
            # Prefer prior-recorded summary/scope if present in the snapshot
            its = data.get("iterations") or []
            if its:
                last = its[-1]
                summary = last.get("summary", summary) or summary
                scope = last.get("scope", scope) or scope
                ts = last.get("timestamp", ts) or ts
        except Exception:
            pass
        records.append((n, {
            "timestamp": ts or "",
            "summary": summary,
            "scope": scope,
            "path": rel_path,
        }))
    records.sort(key=lambda r: r[0])
    return [r[1] for r in records]


def emit_manifest(
    workspace_dir: Path,
    result: Any,
    metrics: Any,
    plot_paths: dict[str, str | Path] | None = None,
    *,
    assumptions_path: Path | None = None,
    iteration_summary: str | None = None,
    iteration_scope: list[str] | None = None,
) -> Path:
    """Emit results/manifest.json matching the report-schema contract.

    The `iterations` field is populated as follows:
    - One entry per discovered `results/iter_<N>/manifest.json` snapshot,
      each with `path: "results/iter_<N>/manifest.json"`.
    - Plus one trailing entry for the *current* run, with `path: null`
      (no snapshot taken yet — the current manifest IS results/manifest.json).
      `summary` and `scope` come from the optional kwargs; the timestamp is
      the same `run_timestamp` written elsewhere in the manifest.
    """
    workspace_dir = Path(workspace_dir)
    out_dir = workspace_dir / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "manifest.json"

    dates_arr_raw = getattr(result, "dates", None)
    dates_arr = np.asarray(dates_arr_raw, dtype=np.int64) if dates_arr_raw is not None else np.zeros(0, dtype=np.int64)
    dates_iso = [_yyyymmdd_to_iso(int(d)) for d in dates_arr]
    equity = getattr(result, "equity_curve", None)
    benchmark = getattr(result, "benchmark_curve", None)
    drawdown = getattr(result, "drawdown_curve", None)
    trades = getattr(result, "trades", []) or []

    trades_out = [
        {
            "date": _yyyymmdd_to_iso(int(t.date)),
            "side": t.side,
            "qty": float(t.qty),
            "price": float(t.price),
            "cost": float(t.cost),
            "pnl": float(t.pnl),
            "leg": t.leg,
        }
        for t in trades
    ]

    spec = (getattr(result, "meta", {}) or {}).get("spec", {})
    sig_hash: str | None = None
    if hasattr(result, "positions") and getattr(result, "positions") is not None:
        try:
            sig_hash = hashlib.sha256(np.asarray(result.positions).tobytes()).hexdigest()
        except Exception:
            sig_hash = None
    if sig_hash is not None:
        spec = dict(spec)
        spec["signals_hash"] = sig_hash

    metrics_dict = _scrub(metrics) if metrics is not None else {}

    # Populate monthly / yearly returns from the equity curve. Schema-required
    # arrays must reflect actual content so the frontend renderer is useful.
    equity_arr = np.asarray(equity, dtype=np.float64) if equity is not None else np.zeros(0, dtype=np.float64)
    if equity_arr.shape[0] > 0 and dates_arr.shape[0] == equity_arr.shape[0]:
        from .metrics import monthly_returns_table, yearly_returns_table

        monthly_returns = monthly_returns_table(equity_arr, dates_arr)
        yearly_returns = yearly_returns_table(equity_arr, dates_arr)
    else:
        monthly_returns = []
        yearly_returns = []

    # Rebalance dates: use the underlying-leg trade dates (the engine emits one
    # entry per change in the target weight on the `underlying` leg). For
    # option-only strategies the underlying leg may be silent — fall back to
    # leg-trade dates so something useful surfaces.
    rebalance_dates: list[str] = []
    if trades:
        seen: set[int] = set()
        for t in trades:
            leg_name = getattr(t, "leg", "")
            if leg_name == "underlying":
                d_int = int(t.date)
                if d_int not in seen:
                    seen.add(d_int)
                    rebalance_dates.append(_yyyymmdd_to_iso(d_int))
        if not rebalance_dates:
            # Pure-options run — record the union of leg trade dates.
            for t in trades:
                d_int = int(t.date)
                if d_int not in seen:
                    seen.add(d_int)
                    rebalance_dates.append(_yyyymmdd_to_iso(d_int))
            rebalance_dates.sort()

    full_range = (
        {"start": dates_iso[0], "end": dates_iso[-1]} if dates_iso else {"start": None, "end": None}
    )

    run_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    iterations_list = _discover_iteration_snapshots(workspace_dir)
    iterations_list.append({
        "timestamp": run_ts,
        "summary": iteration_summary or "",
        "scope": list(iteration_scope) if iteration_scope else [],
        "path": None,
    })

    manifest = {
        "dates": dates_iso,
        "portfolio_equity": _scrub(equity),
        "benchmark_equity": _scrub(benchmark) if benchmark is not None else None,
        "drawdown": _scrub(drawdown),
        "leg_equities": {"underlying": _scrub(equity)},
        "raw_leg_equities": {"underlying": _scrub(equity)},
        "rebalance_dates": rebalance_dates,
        "metrics": metrics_dict,
        "leg_metrics": {},
        "monthly_returns": monthly_returns,
        "yearly_returns": yearly_returns,
        "date_range": full_range,
        "full_date_range": full_range,
        "rebalance": spec.get("rebalance_freq", "bar"),
        "return_type": spec.get("return_type", "normal"),
        "trades": trades_out,
        "assumptions_ref": str(assumptions_path) if assumptions_path else "ASSUMPTIONS.json",
        "notebook_path": "results/notebook.ipynb",
        "plot_paths": {k: str(v) for k, v in (plot_paths or {}).items()},
        "spec": _scrub(spec),
        "plots": {k: str(v) for k, v in (plot_paths or {}).items()},
        "iterations": iterations_list,
        "run_timestamp": run_ts,
    }

    # Plot-set completeness backstop is gone (Sign 1: no strategy_class
    # routing). The code-first surface enumerates BASELINE_PLOTS +
    # strategy.EXTRA_PLOTS via lib.compile.render_extra_plots; missing plots
    # surface in the agent's own dry-run, not via lib gatekeeping.

    # Hand-rolled schema validation (P0-K). Raises before write so a malformed
    # manifest never lands on disk.
    _validate_manifest_or_raise(manifest)

    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out_path


_VOLATILE_KEYS = {"run_timestamp", "compiled_at", "run_id"}


def canonicalize_manifest_for_diff(manifest: dict) -> dict:
    """Strip volatile fields (timestamps, run-ids) so two reruns can be byte-compared."""
    cleaned = deepcopy(manifest)

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        return obj

    return _walk(cleaned)


# ---------------------------------------------------------------------------
# Code-first surface: strategy.py embedding + build_notebook + render_extra_plots
# ---------------------------------------------------------------------------


def _read_strategy_source(workspace_dir: Path) -> tuple[str, str] | None:
    """Return ``(rel_path, source)`` for a workspace's strategy code, or ``None``.

    Looks for ``strategy.py`` first (canonical, single-file), then
    ``strategy/__init__.py`` (allowed directory shape per the locked design).
    """
    candidates = [
        ("strategy.py", workspace_dir / "strategy.py"),
        ("strategy/__init__.py", workspace_dir / "strategy" / "__init__.py"),
    ]
    for rel_path, abs_path in candidates:
        if abs_path.is_file():
            try:
                return rel_path, abs_path.read_text(encoding="utf-8")
            except OSError:
                return rel_path, f"# (failed to read {rel_path})\n"
    return None


def _strategy_source_cells(workspace_dir: Path, nbformat) -> list:
    """Build a markdown header + verbatim code cell for ``strategy.py``.

    Returns an empty list if no strategy file exists in the workspace —
    keeps backward-compat with legacy YAML-driven workspaces while the
    pivot is rolled out.
    """
    embedded = _read_strategy_source(workspace_dir)
    if embedded is None:
        return []
    rel_path, source = embedded
    header = f"# {rel_path} — embedded from workspace at compile time\n"
    return [
        nbformat.v4.new_markdown_cell(source="## Strategy source"),
        nbformat.v4.new_code_cell(source=header + source),
    ]


def build_notebook(workspace_path: Path | str) -> Path:
    """Code-first alias for :func:`compile_workspace`.

    Per the locked design (DESIGN.md §9), the public name in the new
    code-first surface is ``lib.compile.build_notebook``. The legacy
    ``compile_workspace`` name still works (existing pipeline scripts call
    it).
    """
    return compile_workspace(Path(workspace_path))


def render_extra_plots(
    workspace_path: Path | str,
    result: Any,
    *,
    metrics: dict[str, float] | None = None,
) -> dict[str, Path]:
    """Run ``BASELINE_PLOTS`` + strategy ``EXTRA_PLOTS`` and write JSON.

    Imports ``strategy`` from ``workspace_path`` (if present), runs every
    ``PlotJob`` in ``lib.plotting.BASELINE_PLOTS`` plus any in
    ``getattr(strategy, 'EXTRA_PLOTS', [])``, and writes
    ``results/plots/<id>.json`` for each.

    The ``stats_panel`` job is special-cased: it accepts a
    ``BacktestResult`` directly (the mongoDB ``stats_panel`` builder reads
    the metrics off the result) so no caller-side metrics dict is required.

    Returns a dict mapping plot id to written path. Failures of individual
    builders are caught and logged but do not abort the run — a single
    broken EXTRA_PLOTS job should not derail the rest.
    """
    import contextlib
    import importlib
    import logging
    import sys

    log = logging.getLogger(__name__)

    workspace_path = Path(workspace_path).resolve()
    plots_dir = workspace_path / "results" / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    from . import plotting as _plotting

    extra_plots: list[Any] = []
    if (workspace_path / "strategy.py").is_file() or (
        workspace_path / "strategy" / "__init__.py"
    ).is_file():
        added = False
        if str(workspace_path) not in sys.path:
            sys.path.insert(0, str(workspace_path))
            added = True
        sys.modules.pop("strategy", None)
        try:
            strategy_mod = importlib.import_module("strategy")
            extra_plots = list(getattr(strategy_mod, "EXTRA_PLOTS", []) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("could not import strategy from %s: %s", workspace_path, exc)
        finally:
            if added:
                with contextlib.suppress(ValueError):
                    sys.path.remove(str(workspace_path))

    written: dict[str, Path] = {}
    for job in list(_plotting.BASELINE_PLOTS) + extra_plots:
        if not isinstance(job, _plotting.PlotJob):
            log.warning("EXTRA_PLOTS entry is not a PlotJob; skipping: %r", job)
            continue
        out = plots_dir / f"{job.id}.json"
        try:
            fig = job.builder(result, **job.kwargs)
            if fig is None:
                continue
            if hasattr(fig, "write_json"):
                fig.write_json(str(out))
            else:
                out.write_text(json.dumps(fig, default=str), encoding="utf-8")
            written[job.id] = out
        except Exception as exc:  # noqa: BLE001
            log.warning("PlotJob %r builder raised: %s", job.id, exc)
    return written


__all__ = [
    "compile_workspace",
    "build_notebook",
    "render_extra_plots",
    "emit_manifest",
    "canonicalize_manifest_for_diff",
]
