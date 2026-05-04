"""Tests for tcg.core.agent.tools -- tool executors and factory.

These tests exercise the file-system tools (read, write, execute_python,
write_assumptions) without requiring a live MongoDB connection.  MongoDB
tools are tested via the endpoint/integration layer.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tcg.core.agent.tools import (
    TOOL_DEFINITIONS,
    _read_file,
    _safe_resolve,
    _write_file,
    _execute_python,
    _write_assumptions,
    create_tools,
)
from tcg.core.agent.workspace import AgentWorkspace


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> AgentWorkspace:
    return AgentWorkspace(root=tmp_path / "workspaces")


@pytest.fixture()
def session(workspace: AgentWorkspace) -> dict[str, Any]:
    return workspace.create_session(name="test-tools")


@pytest.fixture()
def ws_path(session: dict[str, Any]) -> Path:
    return Path(session["workspace_path"])


# ------------------------------------------------------------------
# _safe_resolve
# ------------------------------------------------------------------


class TestSafeResolve:
    def test_valid_relative_path(self, ws_path: Path) -> None:
        resolved = _safe_resolve(ws_path, "scripts/test.py")
        assert str(resolved).startswith(str(ws_path.resolve()))

    def test_escaping_path_raises(self, ws_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes workspace"):
            _safe_resolve(ws_path, "../../etc/passwd")

    def test_absolute_path_outside_raises(self, ws_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes workspace"):
            _safe_resolve(ws_path, "/tmp/evil.txt")

    def test_nested_dot_dot_raises(self, ws_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes workspace"):
            _safe_resolve(ws_path, "scripts/../../../../../../etc/passwd")


# ------------------------------------------------------------------
# _read_file
# ------------------------------------------------------------------


class TestReadFile:
    async def test_read_existing_file(self, ws_path: Path) -> None:
        target = ws_path / "hello.txt"
        target.write_text("hello world", encoding="utf-8")
        result = await _read_file({"path": "hello.txt"}, workspace=ws_path)
        assert result == "hello world"

    async def test_read_missing_file(self, ws_path: Path) -> None:
        result = await _read_file({"path": "nope.txt"}, workspace=ws_path)
        assert isinstance(result, dict)
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_read_missing_path_param(self, ws_path: Path) -> None:
        result = await _read_file({}, workspace=ws_path)
        assert isinstance(result, dict)
        assert "error" in result

    async def test_read_escaping_path(self, ws_path: Path) -> None:
        result = await _read_file({"path": "../../etc/passwd"}, workspace=ws_path)
        assert isinstance(result, dict)
        assert "error" in result
        assert "escapes" in result["error"].lower()

    async def test_large_file_truncated(self, ws_path: Path) -> None:
        target = ws_path / "big.txt"
        target.write_text("x" * 100_000, encoding="utf-8")
        result = await _read_file({"path": "big.txt"}, workspace=ws_path)
        assert isinstance(result, dict)
        assert result["truncated"] is True
        assert "warning" in result


# ------------------------------------------------------------------
# _write_file
# ------------------------------------------------------------------


class TestWriteFile:
    async def test_write_creates_file(self, ws_path: Path) -> None:
        result = await _write_file(
            {"path": "output.txt", "content": "hello"}, workspace=ws_path
        )
        assert result["status"] == "written"
        assert (ws_path / "output.txt").read_text() == "hello"

    async def test_write_creates_parent_dirs(self, ws_path: Path) -> None:
        result = await _write_file(
            {"path": "deep/nested/file.txt", "content": "nested"},
            workspace=ws_path,
        )
        assert result["status"] == "written"
        assert (ws_path / "deep" / "nested" / "file.txt").read_text() == "nested"

    async def test_write_escaping_path(self, ws_path: Path) -> None:
        result = await _write_file(
            {"path": "../../evil.txt", "content": "pwned"},
            workspace=ws_path,
        )
        assert "error" in result

    async def test_write_returns_byte_count(self, ws_path: Path) -> None:
        result = await _write_file(
            {"path": "test.txt", "content": "abc"}, workspace=ws_path
        )
        assert result["bytes"] == 3


# ------------------------------------------------------------------
# _write_assumptions
# ------------------------------------------------------------------


class TestWriteAssumptions:
    async def test_creates_assumptions(
        self, session: dict[str, Any], workspace: AgentWorkspace
    ) -> None:
        sid = session["id"]
        result = await _write_assumptions(
            {
                "assumptions": [
                    {
                        "field": "execution.fees_bps",
                        "value": 5,
                        "source": "default",
                        "confidence": "high",
                        "rationale": "Default.",
                        "group": "execution",
                    }
                ]
            },
            session_id=sid,
            workspace_manager=workspace,
        )
        assert isinstance(result, dict)
        assert len(result["assumptions"]) == 1
        assert result["assumptions"][0]["field"] == "execution.fees_bps"

    async def test_merge_semantics(
        self, session: dict[str, Any], workspace: AgentWorkspace
    ) -> None:
        sid = session["id"]
        # First write
        await _write_assumptions(
            {
                "assumptions": [
                    {
                        "field": "execution.fees_bps",
                        "value": 5,
                        "source": "default",
                        "confidence": "high",
                        "rationale": "Default.",
                        "group": "execution",
                    }
                ]
            },
            session_id=sid,
            workspace_manager=workspace,
        )
        # Second write with same field (update) + new field
        result = await _write_assumptions(
            {
                "assumptions": [
                    {
                        "field": "execution.fees_bps",
                        "value": 10,
                        "source": "user",
                        "confidence": "high",
                        "rationale": "User override.",
                        "group": "execution",
                    },
                    {
                        "field": "sizing.fraction",
                        "value": 1.0,
                        "source": "default",
                        "confidence": "medium",
                        "rationale": "Single instrument.",
                        "group": "sizing",
                    },
                ]
            },
            session_id=sid,
            workspace_manager=workspace,
        )
        assert len(result["assumptions"]) == 2
        fees = next(
            a for a in result["assumptions"] if a["field"] == "execution.fees_bps"
        )
        assert fees["value"] == 10
        assert fees["source"] == "user"


# ------------------------------------------------------------------
# _execute_python
# ------------------------------------------------------------------


class TestExecutePython:
    async def test_execute_inline_code(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"code": "print('hello from python')"},
            workspace=ws_path,
        )
        assert result["returncode"] == 0
        assert "hello from python" in result["stdout"]

    async def test_execute_script_file(self, ws_path: Path) -> None:
        scripts_dir = ws_path / "scripts"
        scripts_dir.mkdir(parents=True)
        script = scripts_dir / "test_script.py"
        script.write_text("import sys; print(sys.version_info.major)", encoding="utf-8")
        result = await _execute_python(
            {"script_path": "scripts/test_script.py"},
            workspace=ws_path,
        )
        assert result["returncode"] == 0
        assert result["stdout"].strip().isdigit()

    async def test_execute_captures_stderr(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"code": "import sys; print('err', file=sys.stderr)"},
            workspace=ws_path,
        )
        assert "err" in result["stderr"]

    async def test_execute_nonzero_exit(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"code": "import sys; sys.exit(42)"},
            workspace=ws_path,
        )
        assert result["returncode"] == 42

    async def test_execute_missing_script(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"script_path": "nonexistent.py"},
            workspace=ws_path,
        )
        assert "error" in result

    async def test_execute_both_code_and_path_errors(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"code": "pass", "script_path": "x.py"},
            workspace=ws_path,
        )
        assert "error" in result

    async def test_execute_neither_code_nor_path_errors(self, ws_path: Path) -> None:
        result = await _execute_python({}, workspace=ws_path)
        assert "error" in result

    async def test_working_directory_is_workspace(self, ws_path: Path) -> None:
        result = await _execute_python(
            {"code": "from pathlib import Path; print(Path.cwd())"},
            workspace=ws_path,
        )
        assert result["returncode"] == 0
        # The cwd should be the workspace path
        assert str(ws_path.resolve()) in result["stdout"].strip()


# ------------------------------------------------------------------
# create_tools factory
# ------------------------------------------------------------------


class TestCreateTools:
    def test_factory_returns_matching_defs_and_executors(
        self, session: dict[str, Any], workspace: AgentWorkspace
    ) -> None:
        defs, execs = create_tools(
            workspace_path=Path(session["workspace_path"]),
            mongo_uri="mongodb://localhost:27017",
            mongo_db_name="test",
            session_id=session["id"],
            workspace_manager=workspace,
        )
        assert len(defs) == len(TOOL_DEFINITIONS)
        for td in defs:
            assert td["name"] in execs, f"Missing executor for {td['name']}"

    def test_all_tool_defs_have_required_fields(self) -> None:
        for td in TOOL_DEFINITIONS:
            assert "name" in td
            assert "description" in td
            assert "input_schema" in td
            assert td["input_schema"]["type"] == "object"


# ------------------------------------------------------------------
# TOOL_DEFINITIONS structure
# ------------------------------------------------------------------


class TestToolDefinitions:
    def test_seven_tools_defined(self) -> None:
        assert len(TOOL_DEFINITIONS) == 7

    def test_tool_names(self) -> None:
        names = {td["name"] for td in TOOL_DEFINITIONS}
        expected = {
            "query_mongodb",
            "list_collections",
            "read_file",
            "write_file",
            "write_assumptions",
            "execute_python",
            "compile_notebook",
        }
        assert names == expected
