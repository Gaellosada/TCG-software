"""R7 (R-2 carry-forward): contract test for ``_saw_result`` semantics.

Round 5 (F1) made ``_saw_result`` the single source of truth for "did
the CLI subprocess emit a clean ``result`` event in this turn?". The
auto-continue loop (``tcg.core.api.agent._maybe_auto_continue``) gates
on this flag: a turn that did NOT see ``result`` (silent EOF, user
interrupt, crash) must NOT auto-continue, otherwise the harness loops
on a tainted session.

A25 Phase 2c confirmed empirically that all three cc96f2b4
production failures had ``interrupted=True`` -> no ``result`` event ->
``_saw_result=False`` -> auto-continue correctly skipped (G-INVAR #6).
This test pins the **producer side** of that contract: only an
``event_type == "result"`` CLI event flips ``_saw_result`` to True.
``assistant``, ``system``, ``user``, and unrecognised event types
must NEVER set the flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from tcg.core.agent.session import CLISession


def _make_session(tmp_path: Path) -> CLISession:
    return CLISession(
        session_id="saw-result-contract",
        workspace_path=tmp_path,
        on_event=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Per-event-type contract: only ``result`` flips ``_saw_result`` to True
# ---------------------------------------------------------------------------


class TestSawResultOnlySetOnResultEvent:
    """The R5 F1 contract: ``_saw_result=True`` IFF event_type == 'result'."""

    async def test_default_state_false(self, tmp_path: Path) -> None:
        """Fresh session: ``_saw_result`` is False."""
        session = _make_session(tmp_path)
        assert session._saw_result is False

    async def test_assistant_event_does_not_set(
        self, tmp_path: Path
    ) -> None:
        """An ``assistant`` event must NOT flip the flag.

        Drives the real ``_handle_event`` dispatcher (no mocking) with
        a representative assistant event shape.
        """
        session = _make_session(tmp_path)
        assistant_event: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Working on it."}
                ]
            },
        }
        await session._handle_event(
            assistant_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is False, (
            "assistant event must not set _saw_result"
        )

    async def test_system_event_does_not_set(self, tmp_path: Path) -> None:
        """A ``system`` event (init, status, compacting) must NOT flip the flag."""
        session = _make_session(tmp_path)
        system_event: dict[str, Any] = {
            "type": "system",
            "subtype": "init",
            "model": "sonnet",
        }
        await session._handle_event(
            system_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is False

    async def test_user_event_does_not_set(self, tmp_path: Path) -> None:
        """A ``user`` event (tool_result wrapper) must NOT flip the flag."""
        session = _make_session(tmp_path)
        user_event: dict[str, Any] = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "abc",
                        "content": "ok",
                    }
                ]
            },
        }
        await session._handle_event(
            user_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is False

    async def test_stream_event_does_not_set(self, tmp_path: Path) -> None:
        """A ``stream_event`` (delta) must NOT flip the flag."""
        session = _make_session(tmp_path)
        stream_event: dict[str, Any] = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
        }
        await session._handle_event(
            stream_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is False

    async def test_unknown_event_does_not_set(self, tmp_path: Path) -> None:
        """An unrecognised event type must NOT flip the flag."""
        session = _make_session(tmp_path)
        unknown_event: dict[str, Any] = {"type": "totally_made_up_event"}
        await session._handle_event(
            unknown_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is False

    async def test_result_event_does_set(self, tmp_path: Path) -> None:
        """ONLY ``event_type == 'result'`` flips the flag to True."""
        session = _make_session(tmp_path)
        result_event: dict[str, Any] = {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        await session._handle_event(
            result_event,
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is True

    async def test_sequence_assistant_then_result(
        self, tmp_path: Path
    ) -> None:
        """Realistic ordering: assistant streams, then result event flips flag."""
        session = _make_session(tmp_path)

        # Assistant frames first (multiple in a real turn).
        for _ in range(3):
            await session._handle_event(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "..."}]
                    },
                },
                assistant_content=[],
                full_text_parts=[],
                active_blocks={},
            )
            assert session._saw_result is False

        # Then the result event terminates cleanly.
        await session._handle_event(
            {
                "type": "result",
                "subtype": "success",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            assistant_content=[],
            full_text_parts=[],
            active_blocks={},
        )
        assert session._saw_result is True

    async def test_sequence_no_result_stays_false(
        self, tmp_path: Path
    ) -> None:
        """A turn with NO ``result`` event keeps ``_saw_result=False``.

        This pins the silent-EOF / user-interrupt failure mode: the
        loop sees only assistant + system events and the subprocess
        terminates without flushing the final ``result`` -- the
        canonical signature of ``interrupted=True`` in production
        (cc96f2b4 msg[1], msg[3], msg[5]).
        """
        session = _make_session(tmp_path)

        events: list[dict[str, Any]] = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hi"}]},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "content": "ok",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Almost done"}]
                },
            },
            # NO result event -- subprocess ended silently.
        ]
        for ev in events:
            await session._handle_event(
                ev,
                assistant_content=[],
                full_text_parts=[],
                active_blocks={},
            )
        assert session._saw_result is False, (
            "without a 'result' event, _saw_result must remain False so "
            "the auto-continue loop skips this turn (G-INVAR #6)"
        )
