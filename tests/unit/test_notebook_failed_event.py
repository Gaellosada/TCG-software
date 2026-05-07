"""R7 (Issue 27 F2): ``notebook_failed`` WS event contract.

Contract source:
``workspace/tasks/agent-stops-recur-and-notebook-pipeline-and-notify/notebook_failed_contract.md``

The agent occasionally bypasses ``compile_workspace`` (direct
``Write`` + ``NotebookEdit``, or a custom build script that calls
``nbformat.write()`` without ``nbclient.execute()``) -- producing a
notebook with ``outputs: []`` and ``execution_count: null`` on every
code cell. The R7 fix emits a structured ``notebook_failed`` event
distinguishing ``no_outputs`` from ``parse_error``, mutually exclusive
with ``notebook_ready`` per turn.

Tested invariants:
- 0-output notebook -> ``notebook_failed`` with ``reason="no_outputs"``
  emitted exactly once per turn.
- Malformed JSON -> ``notebook_failed`` with ``reason="parse_error"``.
- With-outputs notebook -> ``notebook_ready`` only (no failed event).
- Subsequent post-turn poll (same path, no change) -> no re-emit.
- Subsequent turn that produces outputs -> ``notebook_ready`` emits
  AND ``_notebook_failed_emitted_paths`` gets cleared (so a future
  retry that fails can re-emit).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tcg.core.agent.session import CLISession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _zero_output_notebook() -> dict[str, Any]:
    """Notebook with code cells but no outputs -- the bypass shape."""
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
    nb = _zero_output_notebook()
    nb["cells"][2]["execution_count"] = 1
    nb["cells"][2]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": "1\n"}
    ]
    return nb


def _make_session(tmp_path: Path) -> tuple[CLISession, list[dict[str, Any]]]:
    """Build a CLISession + a captured-events list."""
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    session = CLISession("sess-r7", tmp_path, on_event)
    return session, events


def _write_notebook(tmp_path: Path, nb: dict[str, Any] | str) -> Path:
    nb_dir = tmp_path / "results"
    nb_dir.mkdir(exist_ok=True)
    nb_path = nb_dir / "notebook.ipynb"
    payload = nb if isinstance(nb, str) else json.dumps(nb)
    nb_path.write_text(payload, encoding="utf-8")
    return nb_path


# ---------------------------------------------------------------------------
# Core contract tests
# ---------------------------------------------------------------------------


class TestNotebookFailedNoOutputs:
    async def test_zero_output_notebook_emits_failed_no_outputs(
        self, tmp_path: Path
    ) -> None:
        """Zero-output notebook -> ``notebook_failed`` once with no_outputs."""
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _zero_output_notebook())

        await session._check_file_changes()

        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed) == 1
        assert failed[0]["reason"] == "no_outputs"
        assert failed[0]["session_id"] == "sess-r7"
        assert "timestamp" in failed[0]

        # Mutex: notebook_ready must NOT have fired.
        ready = [e for e in events if e.get("type") == "notebook_ready"]
        assert ready == []

    async def test_subsequent_poll_same_turn_does_not_reemit(
        self, tmp_path: Path
    ) -> None:
        """Once-per-turn idempotency: 2nd ``_check_file_changes`` -> no re-emit."""
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _zero_output_notebook())

        await session._check_file_changes()
        await session._check_file_changes()  # same turn, same path
        await session._check_file_changes()

        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed) == 1, (
            "notebook_failed must fire only ONCE per (turn, path)"
        )


class TestNotebookFailedParseError:
    async def test_malformed_notebook_emits_failed_parse_error(
        self, tmp_path: Path
    ) -> None:
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, "{ this is not valid json")

        await session._check_file_changes()

        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed) == 1
        assert failed[0]["reason"] == "parse_error"
        assert failed[0]["session_id"] == "sess-r7"

        ready = [e for e in events if e.get("type") == "notebook_ready"]
        assert ready == []

    async def test_truncated_notebook_emits_parse_error(
        self, tmp_path: Path
    ) -> None:
        """Truncated JSON (mid-write) -> parse_error."""
        session, events = _make_session(tmp_path)
        _write_notebook(
            tmp_path,
            '{"nbformat":4, "cells":[{"cell_type":"code", "outputs":'
        )

        await session._check_file_changes()

        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed) == 1
        assert failed[0]["reason"] == "parse_error"


class TestNotebookFailedMutexWithReady:
    async def test_with_outputs_emits_ready_not_failed(
        self, tmp_path: Path
    ) -> None:
        """Notebook with outputs -> notebook_ready only; no notebook_failed."""
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _executed_notebook())

        await session._check_file_changes()

        ready = [e for e in events if e.get("type") == "notebook_ready"]
        assert len(ready) == 1
        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert failed == []


class TestNotebookFailedTurnReset:
    async def test_subsequent_turn_clears_failed_emit_set(
        self, tmp_path: Path
    ) -> None:
        """A new turn (``_snapshot_file_state`` called) clears the per-turn set.

        If the agent retries and STILL produces a 0-output notebook on
        the next turn, the FE should be re-warned (the previous warn
        may have been dismissed or the user may have switched tabs).
        """
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _zero_output_notebook())

        # Turn 1: first poll emits, second poll suppressed.
        await session._check_file_changes()
        await session._check_file_changes()
        failed_after_t1 = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed_after_t1) == 1

        # Turn 2 starts: snapshot resets per-turn state.
        session._snapshot_file_state()
        await session._check_file_changes()

        failed_after_t2 = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed_after_t2) == 2, (
            "second turn observing the same bad notebook must re-emit"
        )

    async def test_recovery_emits_ready_after_failed(
        self, tmp_path: Path
    ) -> None:
        """Bad notebook on turn 1, retry produces outputs on turn 2.

        Expected:
        - Turn 1: notebook_failed once.
        - Turn 2 (after re-write with outputs + new snapshot):
          notebook_ready fires.
        """
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _zero_output_notebook())

        await session._check_file_changes()
        assert any(e.get("type") == "notebook_failed" for e in events)

        # New turn: re-snapshot, agent re-runs compile_workspace, now
        # the notebook has outputs.
        session._snapshot_file_state()
        _write_notebook(tmp_path, _executed_notebook())
        await session._check_file_changes()

        ready = [e for e in events if e.get("type") == "notebook_ready"]
        assert len(ready) == 1, "notebook_ready must fire after recovery"


class TestNotebookEventContract:
    async def test_failed_event_shape(self, tmp_path: Path) -> None:
        """Event shape per contract: type, session_id, reason, timestamp."""
        session, events = _make_session(tmp_path)
        _write_notebook(tmp_path, _zero_output_notebook())

        await session._check_file_changes()

        failed = [e for e in events if e.get("type") == "notebook_failed"]
        assert len(failed) == 1
        ev = failed[0]
        # Required keys per contract.
        assert ev["type"] == "notebook_failed"
        assert ev["session_id"] == "sess-r7"
        assert ev["reason"] in ("no_outputs", "parse_error")
        assert isinstance(ev["timestamp"], str)
        assert "T" in ev["timestamp"]  # ISO-8601 separator
