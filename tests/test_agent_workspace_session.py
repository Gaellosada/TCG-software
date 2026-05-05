"""Test that new agent sessions get PIPELINE_GUIDE.md and snippets."""

import shutil
import tempfile
from pathlib import Path

import pytest
from tcg.core.agent.workspace import AgentWorkspace


@pytest.fixture
def workspace():
    root = Path(tempfile.mkdtemp())
    ws = AgentWorkspace(root)
    yield ws
    shutil.rmtree(root)


class TestSessionCreation:
    def test_pipeline_guide_written(self, workspace):
        session = workspace.create_session("test")
        guide = Path(session["workspace_path"]) / "PIPELINE_GUIDE.md"
        assert guide.exists()
        content = guide.read_text()
        assert "Pipeline Guide" in content
        assert "STRATEGY.yaml" in content

    def test_snippets_copied(self, workspace):
        session = workspace.create_session("test")
        snippets_dir = Path(session["workspace_path"]) / "snippets"
        assert snippets_dir.exists()
        py_files = list(snippets_dir.glob("*.py"))
        assert len(py_files) >= 15  # At least 15 snippet files

    def test_snippets_are_valid_python(self, workspace):
        """All copied snippets should be parseable Python."""
        import ast

        session = workspace.create_session("test")
        snippets_dir = Path(session["workspace_path"]) / "snippets"
        for f in snippets_dir.glob("*.py"):
            try:
                ast.parse(f.read_text())
            except SyntaxError:
                pytest.fail(f"Snippet {f.name} has invalid Python syntax")
