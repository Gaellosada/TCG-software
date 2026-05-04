"""Tests for tcg.core.agent.workspace -- AgentWorkspace session management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcg.core.agent.workspace import AgentWorkspace


@pytest.fixture()
def workspace(tmp_path: Path) -> AgentWorkspace:
    """Create a workspace rooted in a temp directory."""
    return AgentWorkspace(root=tmp_path / "workspaces")


class TestCreateSession:
    def test_creates_directory_and_files(self, workspace: AgentWorkspace) -> None:
        result = workspace.create_session(name="Test Session")
        session_dir = Path(result["workspace_path"])
        assert session_dir.exists()
        assert (session_dir / "meta.json").exists()
        assert (session_dir / "conversation.json").exists()
        assert (session_dir / "ASSUMPTIONS.json").exists()

    def test_returns_expected_keys(self, workspace: AgentWorkspace) -> None:
        result = workspace.create_session(name="My Session")
        assert "id" in result
        assert result["name"] == "My Session"
        assert "created_at" in result
        assert "workspace_path" in result

    def test_default_name_when_none(self, workspace: AgentWorkspace) -> None:
        result = workspace.create_session()
        assert result["name"].startswith("Session ")

    def test_conversation_initialised_empty(self, workspace: AgentWorkspace) -> None:
        result = workspace.create_session()
        conv_path = Path(result["workspace_path"]) / "conversation.json"
        assert json.loads(conv_path.read_text()) == []

    def test_assumptions_has_template_structure(
        self, workspace: AgentWorkspace
    ) -> None:
        result = workspace.create_session()
        assumptions_path = Path(result["workspace_path"]) / "ASSUMPTIONS.json"
        data = json.loads(assumptions_path.read_text())
        assert data["version"] == 1
        assert data["assumptions"] == []

    def test_unique_ids(self, workspace: AgentWorkspace) -> None:
        ids = {workspace.create_session()["id"] for _ in range(10)}
        assert len(ids) == 10


class TestListSessions:
    def test_empty_initially(self, workspace: AgentWorkspace) -> None:
        assert workspace.list_sessions() == []

    def test_lists_created_sessions(self, workspace: AgentWorkspace) -> None:
        workspace.create_session(name="A")
        workspace.create_session(name="B")
        sessions = workspace.list_sessions()
        assert len(sessions) == 2
        names = {s["name"] for s in sessions}
        assert names == {"A", "B"}

    def test_newest_first(self, workspace: AgentWorkspace) -> None:
        s1 = workspace.create_session(name="First")
        s2 = workspace.create_session(name="Second")
        sessions = workspace.list_sessions()
        # Second was created after First, so it should appear first
        assert sessions[0]["name"] == "Second"
        assert sessions[1]["name"] == "First"

    def test_ignores_non_session_directories(self, workspace: AgentWorkspace) -> None:
        workspace.create_session()
        # Create a stray directory without meta.json
        stray = workspace.root / "stray_dir"
        stray.mkdir()
        assert len(workspace.list_sessions()) == 1


class TestGetSession:
    def test_returns_session(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session(name="Found")
        result = workspace.get_session(created["id"])
        assert result is not None
        assert result["name"] == "Found"

    def test_returns_none_for_missing(self, workspace: AgentWorkspace) -> None:
        assert workspace.get_session("nonexistent") is None


class TestDeleteSession:
    def test_deletes_existing(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session(name="Doomed")
        assert workspace.delete_session(created["id"]) is True
        assert workspace.get_session(created["id"]) is None
        assert not Path(created["workspace_path"]).exists()

    def test_returns_false_for_missing(self, workspace: AgentWorkspace) -> None:
        assert workspace.delete_session("nonexistent") is False


class TestConversationPersistence:
    def test_save_and_load(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
        ]
        workspace.save_conversation(created["id"], messages)
        loaded = workspace.load_conversation(created["id"])
        assert loaded == messages

    def test_load_empty_for_new_session(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session()
        loaded = workspace.load_conversation(created["id"])
        assert loaded == []

    def test_load_returns_empty_for_missing(self, workspace: AgentWorkspace) -> None:
        loaded = workspace.load_conversation("nonexistent")
        assert loaded == []


class TestAssumptions:
    def test_save_and_load(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session()
        assumptions = {
            "version": 1,
            "assumptions": [{"text": "Prices are in USD", "confidence": "high"}],
        }
        workspace.save_assumptions(created["id"], assumptions)
        loaded = workspace.load_assumptions(created["id"])
        assert loaded == assumptions

    def test_default_template_for_new_session(self, workspace: AgentWorkspace) -> None:
        created = workspace.create_session()
        loaded = workspace.load_assumptions(created["id"])
        assert loaded["version"] == 1
        assert loaded["assumptions"] == []
