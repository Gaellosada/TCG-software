"""Agent tool definitions and executors for the MongoDB backtester agent.

Each tool is implemented as an async function conforming to the ``ToolExecutor``
protocol: ``async (dict) -> str | dict``.  The module also exports the Anthropic
API tool-definition dicts consumed by ``AgentSession``.

The ``create_tools`` factory wires everything together, returning a
``(tool_definitions, tool_executors)`` tuple ready for injection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from tcg.core.agent.session import ToolExecutor
from tcg.core.agent.workspace import AgentWorkspace

logger = logging.getLogger(__name__)

# Maximum number of documents returned by find queries.
_MAX_FIND_LIMIT = 100
# Maximum file size (bytes) for read_file before truncation.
_MAX_READ_BYTES = 50 * 1024
# Python execution timeout in seconds.
_EXEC_TIMEOUT_S = 120

# ------------------------------------------------------------------
# BSON / JSON serialisation helpers
# ------------------------------------------------------------------


def _serialise_mongo_value(value: Any) -> Any:
    """Recursively convert BSON types to JSON-serialisable equivalents."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: _serialise_mongo_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialise_mongo_value(v) for v in value]
    return value


# ------------------------------------------------------------------
# Path safety
# ------------------------------------------------------------------


def _safe_resolve(workspace: Path, relative: str) -> Path:
    """Resolve *relative* against *workspace*, raising if the result escapes."""
    resolved = (workspace / relative).resolve()
    workspace_resolved = workspace.resolve()
    if not resolved.is_relative_to(workspace_resolved):
        raise ValueError(f"Path escapes workspace: {relative!r} resolves to {resolved}")
    return resolved


# ------------------------------------------------------------------
# Tool implementations
# ------------------------------------------------------------------


async def _query_mongodb(
    tool_input: dict[str, Any],
    *,
    db: Any,
) -> dict[str, Any]:
    """Execute a read-only MongoDB query using a shared database handle."""
    collection_name = tool_input["collection"]
    operation = tool_input.get("operation", "find")
    query = tool_input.get("query", {})
    projection = tool_input.get("projection")
    limit = min(tool_input.get("limit", _MAX_FIND_LIMIT), _MAX_FIND_LIMIT)
    sort = tool_input.get("sort")

    allowed_ops = {"find", "aggregate", "count", "distinct"}
    if operation not in allowed_ops:
        return {
            "error": f"Operation {operation!r} not allowed. Use one of: {allowed_ops}"
        }

    coll = db[collection_name]

    if operation == "find":
        kwargs: dict[str, Any] = {}
        if projection:
            kwargs["projection"] = projection
        cursor = coll.find(query, **kwargs)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        cursor = cursor.limit(limit)
        docs = await cursor.to_list(length=limit)
        return {
            "count": len(docs),
            "documents": _serialise_mongo_value(docs),
            "truncated": len(docs) == limit,
        }

    elif operation == "aggregate":
        pipeline = query if isinstance(query, list) else [query]
        # Safety: inject a $limit stage if none present
        has_limit = any(isinstance(s, dict) and "$limit" in s for s in pipeline)
        if not has_limit:
            pipeline.append({"$limit": limit})
        cursor = coll.aggregate(pipeline)
        docs = await cursor.to_list(length=limit)
        return {
            "count": len(docs),
            "documents": _serialise_mongo_value(docs),
        }

    elif operation == "count":
        n = await coll.count_documents(query)
        return {"count": n}

    elif operation == "distinct":
        field = tool_input.get("field", "")
        if not field:
            return {"error": "distinct requires a 'field' parameter"}
        values = await coll.distinct(field, query)
        return {
            "count": len(values),
            "values": _serialise_mongo_value(values[:limit]),
            "truncated": len(values) > limit,
        }

    return {"error": f"Unhandled operation: {operation}"}


async def _list_collections(
    _tool_input: dict[str, Any],
    *,
    db: Any,
) -> dict[str, Any]:
    """List all collections in the database."""
    names = await db.list_collection_names()
    return {"collections": sorted(names), "count": len(names)}


async def _read_file(
    tool_input: dict[str, Any],
    *,
    workspace: Path,
) -> dict[str, Any] | str:
    """Read a file from the session workspace."""
    rel_path = tool_input.get("path", "")
    if not rel_path:
        return {"error": "Missing 'path' parameter"}

    try:
        target = _safe_resolve(workspace, rel_path)
    except ValueError as e:
        return {"error": str(e)}

    if not target.exists():
        return {"error": f"File not found: {rel_path}"}
    if not target.is_file():
        return {"error": f"Not a file: {rel_path}"}

    raw = target.read_bytes()
    if len(raw) > _MAX_READ_BYTES:
        text = raw[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
        return {
            "content": text,
            "truncated": True,
            "total_bytes": len(raw),
            "warning": f"File truncated at {_MAX_READ_BYTES} bytes (total: {len(raw)})",
        }
    return raw.decode("utf-8", errors="replace")


async def _write_file(
    tool_input: dict[str, Any],
    *,
    workspace: Path,
) -> dict[str, Any]:
    """Write a file to the session workspace."""
    rel_path = tool_input.get("path", "")
    content = tool_input.get("content", "")
    if not rel_path:
        return {"error": "Missing 'path' parameter"}

    try:
        target = _safe_resolve(workspace, rel_path)
    except ValueError as e:
        return {"error": str(e)}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "status": "written",
        "path": rel_path,
        "bytes": len(content.encode("utf-8")),
    }


async def _write_assumptions(
    tool_input: dict[str, Any],
    *,
    session_id: str,
    workspace_manager: AgentWorkspace,
) -> dict[str, Any]:
    """Update ASSUMPTIONS.json for the session (merge semantics)."""
    new_assumptions = tool_input.get("assumptions", [])
    if not isinstance(new_assumptions, list):
        return {"error": "'assumptions' must be a list of assumption objects"}

    existing = workspace_manager.load_assumptions(session_id)
    existing_list: list[dict[str, Any]] = existing.get("assumptions", [])

    # Index by field name for merge
    by_field: dict[str, dict[str, Any]] = {
        a["field"]: a for a in existing_list if isinstance(a, dict) and "field" in a
    }

    now = datetime.now(timezone.utc).isoformat()
    for entry in new_assumptions:
        if not isinstance(entry, dict) or "field" not in entry:
            continue
        entry.setdefault("applied_at", now)
        by_field[entry["field"]] = entry

    existing["assumptions"] = list(by_field.values())
    existing.setdefault("metadata", {})
    existing["metadata"]["last_updated"] = now

    workspace_manager.save_assumptions(session_id, existing)
    return existing


async def _execute_python(
    tool_input: dict[str, Any],
    *,
    workspace: Path,
) -> dict[str, Any]:
    """Run a Python script in the workspace via subprocess."""
    code = tool_input.get("code")
    script_path_rel = tool_input.get("script_path")

    if code and script_path_rel:
        return {"error": "Provide 'code' or 'script_path', not both"}
    if not code and not script_path_rel:
        return {"error": "Provide 'code' or 'script_path'"}

    if code:
        scripts_dir = workspace / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in scripts/
        fd, tmp_path = tempfile.mkstemp(
            suffix=".py", prefix="agent_", dir=str(scripts_dir)
        )
        os.close(fd)
        script_path = Path(tmp_path)
        script_path.write_text(code, encoding="utf-8")
    else:
        try:
            script_path = _safe_resolve(workspace, script_path_rel)
        except ValueError as e:
            return {"error": str(e)}
        if not script_path.exists():
            return {"error": f"Script not found: {script_path_rel}"}

    env = os.environ.copy()
    # Ensure the project root is importable
    project_root = Path(__file__).resolve().parents[3]
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{project_root}{os.pathsep}{existing_pp}" if existing_pp else str(project_root)
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_EXEC_TIMEOUT_S
        )
        return {
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        proc.kill()  # type: ignore[union-attr]
        return {
            "error": f"Script timed out after {_EXEC_TIMEOUT_S}s",
            "returncode": -1,
        }
    except Exception as e:
        return {"error": f"Failed to run script: {e}"}


async def _compile_notebook(
    tool_input: dict[str, Any],
    *,
    workspace: Path,
) -> dict[str, Any]:
    """Compile workspace scripts into a Jupyter notebook.

    Scans ``scripts/*.py`` in numeric order, creates notebook cells from each
    file, optionally executes them via nbclient, and writes the result to
    ``results/notebook.ipynb``.
    """
    execute = tool_input.get("execute", True)

    # Build the compile script dynamically and run it via subprocess,
    # so nbclient/nbformat are only imported in the subprocess.
    compile_code = f"""\
import json, sys
from pathlib import Path

try:
    import nbformat
except ImportError:
    print(json.dumps({{"error": "nbformat not installed"}}))
    sys.exit(0)

WS = Path({str(workspace)!r})
scripts_dir = WS / "scripts"
results_dir = WS / "results"
results_dir.mkdir(parents=True, exist_ok=True)

# Gather scripts in sorted order
py_files = sorted(scripts_dir.glob("*.py")) if scripts_dir.exists() else []
if not py_files:
    print(json.dumps({{"error": "No scripts found in scripts/"}}))
    sys.exit(0)

nb = nbformat.v4.new_notebook()
nb.metadata["kernelspec"] = {{
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}}

for pf in py_files:
    code = pf.read_text(encoding="utf-8")
    cell = nbformat.v4.new_code_cell(source=code)
    cell.metadata["source_file"] = pf.name
    nb.cells.append(cell)

execute = {execute!r}
if execute:
    try:
        from nbclient import NotebookClient
        client = NotebookClient(
            nb,
            timeout=600,
            kernel_name="python3",
            resources={{"metadata": {{"path": str(WS)}}}},
        )
        client.execute()
    except Exception as exc:
        partial_path = results_dir / "notebook.partial.ipynb"
        nbformat.write(nb, str(partial_path))
        print(json.dumps({{
            "status": "failed",
            "error": str(exc),
            "partial": str(partial_path.relative_to(WS)),
        }}))
        sys.exit(0)

out_path = results_dir / "notebook.ipynb"
nbformat.write(nb, str(out_path))
print(json.dumps({{
    "status": "compiled",
    "path": str(out_path.relative_to(WS)),
    "cells": len(nb.cells),
}}))
"""

    # Write compile script to a temp file
    scripts_dir = workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    compile_script = scripts_dir / "_compile_notebook.py"
    compile_script.write_text(compile_code, encoding="utf-8")

    env = os.environ.copy()
    project_root = Path(__file__).resolve().parents[3]
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{project_root}{os.pathsep}{existing_pp}" if existing_pp else str(project_root)
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(compile_script),
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=600
        )
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if stdout_text:
            try:
                return json.loads(stdout_text)
            except json.JSONDecodeError:
                pass
        return {
            "status": "failed",
            "error": stderr_bytes.decode("utf-8", errors="replace") or "No output",
        }
    except asyncio.TimeoutError:
        proc.kill()  # type: ignore[union-attr]
        return {"status": "failed", "error": "Notebook compilation timed out"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        # Clean up the compile script
        if compile_script.exists():
            compile_script.unlink()


# ------------------------------------------------------------------
# Anthropic API tool definitions
# ------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_mongodb",
        "description": (
            "Execute a read-only MongoDB query against the platform database. "
            "Supports find, aggregate, count, and distinct operations. "
            "Results are limited to prevent context overflow. "
            "Use this to explore instrument metadata, price data, option chains, "
            "and any other collections in the database."
        ),
        "input_schema": {
            "type": "object",
            "required": ["collection"],
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "MongoDB collection name (e.g. 'YAHOO_INDEX', 'OPT_SP_500', 'FUT_VIX').",
                },
                "operation": {
                    "type": "string",
                    "enum": ["find", "aggregate", "count", "distinct"],
                    "description": "Query operation. Default: 'find'.",
                },
                "query": {
                    "type": "object",
                    "description": "MongoDB query filter for find/count/distinct, or aggregation pipeline (as a list) for aggregate.",
                },
                "projection": {
                    "type": "object",
                    "description": "Fields to include/exclude (find only). E.g. {'close': 1, 'date': 1, '_id': 0}.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max documents to return (default 100, max 100).",
                },
                "sort": {
                    "type": "object",
                    "description": "Sort specification. E.g. {'date': -1} for newest first.",
                },
                "field": {
                    "type": "string",
                    "description": "Field name for distinct operation.",
                },
            },
        },
    },
    {
        "name": "list_collections",
        "description": (
            "List all MongoDB collections in the platform database. "
            "Use this first to discover what data is available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the session workspace. "
            "Paths are relative to the workspace root. "
            "Files over 50KB are truncated with a warning."
        ),
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace (e.g. 'STRATEGY.yaml', 'results/metrics.json').",
                },
            },
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a file to the session workspace. "
            "Creates parent directories if needed. "
            "Use for STRATEGY.yaml, scripts, data summaries, etc."
        ),
        "input_schema": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write.",
                },
            },
        },
    },
    {
        "name": "write_assumptions",
        "description": (
            "Update the ASSUMPTIONS.json for this session. "
            "Merges new assumptions into the existing file (by field name). "
            "Every inferred default or user-confirmed value should be logged here. "
            "This also triggers an assumptions_update event on the WebSocket."
        ),
        "input_schema": {
            "type": "object",
            "required": ["assumptions"],
            "properties": {
                "assumptions": {
                    "type": "array",
                    "description": "List of assumption records to merge.",
                    "items": {
                        "type": "object",
                        "required": [
                            "field",
                            "value",
                            "source",
                            "confidence",
                            "rationale",
                            "group",
                        ],
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": "Dotted path into STRATEGY.yaml (e.g. 'execution.fees_bps').",
                            },
                            "value": {
                                "description": "The applied value (any JSON type).",
                            },
                            "source": {
                                "type": "string",
                                "enum": ["default", "inferred", "user"],
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "rationale": {
                                "type": "string",
                                "description": "One-line justification.",
                            },
                            "group": {
                                "type": "string",
                                "enum": [
                                    "meta",
                                    "universe",
                                    "date_range",
                                    "execution",
                                    "signals",
                                    "sizing",
                                    "benchmark",
                                    "reporting",
                                ],
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "name": "execute_python",
        "description": (
            "Run a Python script in the session workspace. "
            "Provide either inline code or a path to an existing script. "
            "The working directory is the session workspace root. "
            "Timeout: 120 seconds. stdout and stderr are captured and returned. "
            "Use this for data loading, backtesting, analysis, and plot generation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Written to a temp file and run.",
                },
                "script_path": {
                    "type": "string",
                    "description": "Relative path to an existing .py script in the workspace.",
                },
            },
        },
    },
    {
        "name": "compile_notebook",
        "description": (
            "Compile all scripts in scripts/ into a Jupyter notebook at results/notebook.ipynb. "
            "Scripts are sorted by filename and concatenated as cells. "
            "Set execute=true (default) to run the cells and capture outputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "execute": {
                    "type": "boolean",
                    "description": "Whether to execute the notebook cells (default: true).",
                },
            },
        },
    },
]


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def create_tools(
    workspace_path: Path,
    mongo_uri: str,
    mongo_db_name: str,
    session_id: str,
    workspace_manager: AgentWorkspace,
) -> tuple[list[dict[str, Any]], dict[str, ToolExecutor]]:
    """Build tool definitions and executors for an agent session.

    Returns
    -------
    (tool_definitions, tool_executors)
        Ready for injection into ``AgentSession``.
    """
    workspace = Path(workspace_path)

    # Shared Motor client + database for all MongoDB tools in this session.
    # Motor clients are thread-safe and manage their own connection pool.
    _mongo_client = AsyncIOMotorClient(mongo_uri)
    _mongo_db = _mongo_client[mongo_db_name]

    async def query_mongodb(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _query_mongodb(inp, db=_mongo_db)

    async def list_collections(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _list_collections(inp, db=_mongo_db)

    async def read_file(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _read_file(inp, workspace=workspace)

    async def write_file(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _write_file(inp, workspace=workspace)

    async def write_assumptions(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _write_assumptions(
            inp, session_id=session_id, workspace_manager=workspace_manager
        )

    async def execute_python(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _execute_python(inp, workspace=workspace)

    async def compile_notebook(inp: dict[str, Any]) -> str | dict[str, Any]:
        return await _compile_notebook(inp, workspace=workspace)

    executors: dict[str, ToolExecutor] = {
        "query_mongodb": query_mongodb,
        "list_collections": list_collections,
        "read_file": read_file,
        "write_file": write_file,
        "write_assumptions": write_assumptions,
        "execute_python": execute_python,
        "compile_notebook": compile_notebook,
    }

    return list(TOOL_DEFINITIONS), executors
