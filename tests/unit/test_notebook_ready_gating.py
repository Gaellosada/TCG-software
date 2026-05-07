"""Issue 24 (Round 6 RCA-1): notebook_ready event gating.

Real-world failure: agent writes ``results/notebook.ipynb`` with every
code cell carrying ``outputs: []`` and ``execution_count: null`` (i.e.
``compile_workspace`` was either skipped, called with ``execute=False``,
or failed silently). Surfacing that empty notebook to the FE shows code
with no results / plots, which is worse than not surfacing it at all.

The fix gates ``notebook_ready`` on at least one code cell having
non-empty ``outputs[]``. These tests validate the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tcg.core.agent.session import CLISession, _notebook_has_outputs


# ---------------------------------------------------------------------------
# Notebook fixtures
# ---------------------------------------------------------------------------


def _empty_notebook() -> dict[str, Any]:
    """Notebook with code cells but no outputs (the failure shape)."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "cell_type": "markdown",
                "source": "# Title",
                "metadata": {},
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "source": "x = 1",
                "outputs": [],
                "metadata": {},
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "source": "print(x)",
                "outputs": [],
                "metadata": {},
            },
        ],
    }


def _executed_notebook() -> dict[str, Any]:
    """Notebook with at least one code cell carrying executed outputs."""
    nb = _empty_notebook()
    nb["cells"][2]["execution_count"] = 1
    nb["cells"][2]["outputs"] = [
        {
            "output_type": "stream",
            "name": "stdout",
            "text": "1\n",
        }
    ]
    return nb


# ---------------------------------------------------------------------------
# _notebook_has_outputs
# ---------------------------------------------------------------------------


class TestNotebookHasOutputs:
    def test_executed_notebook_returns_true(self, tmp_path: Path) -> None:
        path = tmp_path / "notebook.ipynb"
        path.write_text(json.dumps(_executed_notebook()), encoding="utf-8")
        assert _notebook_has_outputs(path) is True

    def test_empty_notebook_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "notebook.ipynb"
        path.write_text(json.dumps(_empty_notebook()), encoding="utf-8")
        assert _notebook_has_outputs(path) is False

    def test_only_markdown_returns_false(self, tmp_path: Path) -> None:
        nb = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {
                    "cell_type": "markdown",
                    "source": "# All text",
                    "metadata": {},
                }
            ],
        }
        path = tmp_path / "notebook.ipynb"
        path.write_text(json.dumps(nb), encoding="utf-8")
        assert _notebook_has_outputs(path) is False

    def test_malformed_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "notebook.ipynb"
        path.write_text("not json at all", encoding="utf-8")
        assert _notebook_has_outputs(path) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.ipynb"
        assert _notebook_has_outputs(path) is False


# ---------------------------------------------------------------------------
# Integration: _check_file_changes gates notebook_ready
# ---------------------------------------------------------------------------


class TestCheckFileChangesGatesNotebookReady:
    async def test_no_event_for_empty_notebook(
        self, tmp_path: Path
    ) -> None:
        """An empty notebook does NOT fire ``notebook_ready``."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("s1", tmp_path, on_event)
        nb_dir = tmp_path / "results"
        nb_dir.mkdir()
        (nb_dir / "notebook.ipynb").write_text(
            json.dumps(_empty_notebook()), encoding="utf-8"
        )

        await session._check_file_changes()

        assert all(e.get("type") != "notebook_ready" for e in events)

    async def test_event_fires_for_executed_notebook(
        self, tmp_path: Path
    ) -> None:
        """An executed notebook DOES fire ``notebook_ready``."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("s1", tmp_path, on_event)
        nb_dir = tmp_path / "results"
        nb_dir.mkdir()
        (nb_dir / "notebook.ipynb").write_text(
            json.dumps(_executed_notebook()), encoding="utf-8"
        )

        await session._check_file_changes()

        assert any(e.get("type") == "notebook_ready" for e in events)

    async def test_event_fires_once_outputs_arrive_late(
        self, tmp_path: Path
    ) -> None:
        """Empty notebook on first check, executed on second -> fires once."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("s1", tmp_path, on_event)
        nb_dir = tmp_path / "results"
        nb_dir.mkdir()
        nb_path = nb_dir / "notebook.ipynb"

        # First check: empty -> no event.
        nb_path.write_text(
            json.dumps(_empty_notebook()), encoding="utf-8"
        )
        await session._check_file_changes()
        assert all(e.get("type") != "notebook_ready" for e in events)

        # Now the agent (or compile_workspace) re-writes with outputs.
        nb_path.write_text(
            json.dumps(_executed_notebook()), encoding="utf-8"
        )
        await session._check_file_changes()
        ready_events = [
            e for e in events if e.get("type") == "notebook_ready"
        ]
        assert len(ready_events) == 1

        # A third check (notebook unchanged) does not re-fire.
        await session._check_file_changes()
        ready_events = [
            e for e in events if e.get("type") == "notebook_ready"
        ]
        assert len(ready_events) == 1
