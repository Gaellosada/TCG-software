"""Tests for the workspace-local ``.env`` write at session creation.

Issue 17: ``AgentWorkspace.create_session`` writes a ``.env`` file into
each session directory so that scripts the agent runs from the session
workspace can resolve ``MONGO_URI`` via the file-walk path of
``tcg.backtester.lib.mongo.resolve_env()``, not just inherited
``os.environ``. Reuses the SAME ``mongo_uri`` value resolved for the
session's ``.mcp.json`` so both files agree at session-create time.

This file is separate from ``tests/unit/test_agent_workspace.py`` per
guardrail G4 (do not modify the existing test_agent_workspace.py beyond
strict fixture cleanup).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tcg.core.agent.workspace import AgentWorkspace


@pytest.fixture
def workspace(tmp_path: Path) -> AgentWorkspace:
    return AgentWorkspace(root=tmp_path)


class TestWorkspaceDotenvWrite:
    """Issue 17: session_workspace/.env exists and contains MONGO_URI."""

    def test_create_session_writes_dotenv(
        self, workspace: AgentWorkspace
    ) -> None:
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://test-host:27017/?replicaSet=rs0",
        ):
            session = workspace.create_session(name="dotenv-test")

        env_path = Path(session["workspace_path"]) / ".env"
        assert env_path.exists(), (
            "Issue 17: create_session must write a workspace-local"
            " .env file so resolve_env() finds MONGO_URI via the"
            " file-walk path, not just os.environ"
        )
        content = env_path.read_text(encoding="utf-8")
        assert "MONGO_URI=mongodb://test-host:27017/?replicaSet=rs0" in content, (
            f"Issue 17: .env must contain MONGO_URI=<resolved_uri>;"
            f" got {content!r}"
        )

    def test_dotenv_uri_matches_mcp_json_uri(
        self, workspace: AgentWorkspace
    ) -> None:
        """Single source of truth: the .env URI must equal the .mcp.json
        URI so a script reading the workspace .env and the MCP server
        reading .mcp.json see the SAME database at session-create time."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://shared-uri:27017/?authSource=admin",
        ):
            session = workspace.create_session(name="parity-test")

        session_dir = Path(session["workspace_path"])
        env_content = (session_dir / ".env").read_text(encoding="utf-8")

        import json
        mcp_content = json.loads(
            (session_dir / ".mcp.json").read_text(encoding="utf-8")
        )
        mcp_uri = (
            mcp_content["mcpServers"]["mongodb"]["env"][
                "MDB_MCP_CONNECTION_STRING"
            ]
        )

        assert mcp_uri == "mongodb://shared-uri:27017/?authSource=admin"
        assert f"MONGO_URI={mcp_uri}" in env_content, (
            f".env's MONGO_URI must equal .mcp.json's"
            f" MDB_MCP_CONNECTION_STRING (single source of truth);"
            f" .env={env_content!r}, mcp_uri={mcp_uri!r}"
        )

    def test_dotenv_resolves_real_uri_when_unmocked(
        self, workspace: AgentWorkspace
    ) -> None:
        """Smoke: even without monkeypatching _get_mongo_uri, the .env
        file is written. Content depends on the test runner's env/.env
        but presence is invariant."""
        session = workspace.create_session(name="smoke-test")
        env_path = Path(session["workspace_path"]) / ".env"
        assert env_path.exists()
        assert env_path.read_text(encoding="utf-8").startswith("MONGO_URI=")

    def test_dotenv_trailing_newline(
        self, workspace: AgentWorkspace
    ) -> None:
        """POSIX convention: text files end with a newline. python-dotenv
        and bash-style env loaders expect this; without it, appending
        future lines (e.g. MONGO_DB_NAME) would join onto the same line."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://localhost:27017",
        ):
            session = workspace.create_session(name="newline-test")

        env_path = Path(session["workspace_path"]) / ".env"
        content = env_path.read_text(encoding="utf-8")
        assert content.endswith("\n"), (
            f".env must end with a newline; got {content!r}"
        )
