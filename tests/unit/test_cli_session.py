"""Tests for tcg.core.agent.session -- CLISession subprocess management."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tcg.core.agent.session import CLISession, cli_available, _cli_model_arg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_lines(*events: dict[str, Any]) -> bytes:
    """Encode a sequence of JSON events as newline-delimited bytes (simulating CLI stdout)."""
    lines = [json.dumps(e) for e in events]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _text_delta_event(text: str, index: int = 0) -> dict[str, Any]:
    """Build a stream_event with a text_delta."""
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _content_block_start_text(index: int = 0) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text"},
        },
    }


def _content_block_start_tool(index: int, tool_id: str, name: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": {},
            },
        },
    }


def _input_json_delta(index: int, partial: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    }


def _content_block_stop(index: int) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


def _result_success(text: str = "") -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "result": text}


def _result_error(message: str) -> dict[str, Any]:
    return {"type": "result", "subtype": "error", "is_error": True, "result": message}


def _result_budget_exceeded() -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "error_max_budget_usd",
        "is_error": True,
        "result": "",
    }


# ---------------------------------------------------------------------------
# Mock subprocess helper
# ---------------------------------------------------------------------------


class FakeProcess:
    """Simulates asyncio.subprocess.Process with controllable stdout."""

    def __init__(self, stdout_data: bytes, returncode: int = 0) -> None:
        self._stdout_data = stdout_data
        self.returncode = returncode
        self.pid = 99999
        self.stdout = self._make_reader(stdout_data)
        self.stderr = self._make_reader(b"")

    def _make_reader(self, data: bytes):
        reader = AsyncMock()
        lines = data.split(b"\n")
        # readline() returns each line WITH newline, empty bytes at EOF
        line_iter = iter([line + b"\n" if line else b"" for line in lines])
        reader.readline = AsyncMock(side_effect=line_iter)
        reader.read = AsyncMock(return_value=b"")
        return reader

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return b"", b""

    def kill(self):
        pass

    def terminate(self):
        pass


async def _fake_subprocess(stdout_data: bytes, returncode: int = 0):
    """Create a coroutine that returns a FakeProcess."""
    return FakeProcess(stdout_data, returncode)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCliAvailable:
    def test_returns_bool(self) -> None:
        result = cli_available()
        assert isinstance(result, bool)

    def test_detects_claude_on_path(self) -> None:
        with patch(
            "tcg.core.agent.session.shutil.which", return_value="/usr/bin/claude"
        ):
            assert cli_available() is True

    def test_detects_missing_claude(self) -> None:
        with patch("tcg.core.agent.session.shutil.which", return_value=None):
            assert cli_available() is False


class TestModelMapping:
    def test_opus_mapping(self) -> None:
        assert _cli_model_arg("claude-opus-4-6") == "opus"

    def test_sonnet_mapping(self) -> None:
        assert _cli_model_arg("claude-sonnet-4-6") == "sonnet"

    def test_unknown_passes_through(self) -> None:
        assert _cli_model_arg("some-future-model") == "some-future-model"


class TestCLISessionBuildCommand:
    def test_first_turn_uses_session_id(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        session = CLISession("abc-123", tmp_path, on_event)
        cmd = session._build_command("Hello", "opus")
        assert "--session-id" in cmd
        assert "abc-123" in cmd
        assert "--resume" not in cmd

    def test_subsequent_turn_uses_resume(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        session = CLISession("abc-123", tmp_path, on_event)
        session._first_turn = False
        cmd = session._build_command("Follow up", "sonnet")
        assert "--resume" in cmd
        assert "abc-123" in cmd
        assert "--session-id" not in cmd

    def test_command_includes_required_flags(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        session = CLISession("test-id", tmp_path, on_event)
        cmd = session._build_command("Test", "opus")
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd
        assert "--disable-slash-commands" in cmd


class TestCLISessionRunTurnTextResponse:
    async def test_simple_text_response(self, tmp_path: Path) -> None:
        """CLI returns text deltas followed by a success result."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("test-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("Hello "),
            _text_delta_event("world!"),
            _content_block_stop(0),
            _result_success(""),
        )

        fake_proc = FakeProcess(stdout_data, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("Hi", model="opus")

        # Should have: 2 token events + 1 message_complete
        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 2
        assert token_events[0]["content"] == "Hello "
        assert token_events[1]["content"] == "world!"

        complete_events = [e for e in events if e["type"] == "message_complete"]
        assert len(complete_events) == 1
        assert complete_events[0]["content"] == "Hello world!"

        # Conversation history updated
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["role"] == "user"
        assert session.conversation_history[1]["role"] == "assistant"

        # First turn flag switched
        assert session._first_turn is False


class TestCLISessionRunTurnToolUse:
    async def test_tool_call_emitted(self, tmp_path: Path) -> None:
        """CLI emits tool_use content blocks — we should emit tool_call events."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("test-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(
            _content_block_start_tool(0, "tool_abc", "Read"),
            _input_json_delta(0, '{"file_path"'),
            _input_json_delta(0, ': "/tmp/test.py"}'),
            _content_block_stop(0),
            _content_block_start_text(1),
            _text_delta_event("Done.", index=1),
            _content_block_stop(1),
            _result_success(""),
        )

        fake_proc = FakeProcess(stdout_data, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("Read a file", model="sonnet")

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "Read"
        assert tool_calls[0]["id"] == "tool_abc"
        assert tool_calls[0]["input"] == {"file_path": "/tmp/test.py"}


class TestCLISessionErrorHandling:
    async def test_process_crash_emits_error(self, tmp_path: Path) -> None:
        """Non-zero exit code with no content emits error event."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("test-sess", tmp_path, on_event)

        # Empty stdout, non-zero exit
        fake_proc = FakeProcess(b"", returncode=1)
        fake_proc.stderr = AsyncMock()
        fake_proc.stderr.read = AsyncMock(return_value=b"Some error occurred")

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("Crash test", model="opus")

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "CLI process failed" in error_events[0]["message"]

    async def test_budget_exceeded_error(self, tmp_path: Path) -> None:
        """Budget exceeded result emits a specific error."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("test-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(_result_budget_exceeded())
        fake_proc = FakeProcess(stdout_data, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("Expensive query", model="opus")

        error_events = [e for e in events if e["type"] == "error"]
        assert any("Budget exceeded" in e["message"] for e in error_events)

    async def test_cli_error_result(self, tmp_path: Path) -> None:
        """CLI returns an error result event."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("test-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(_result_error("Authentication failed"))
        fake_proc = FakeProcess(stdout_data, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("Should fail", model="opus")

        error_events = [e for e in events if e["type"] == "error"]
        assert any("Authentication failed" in e["message"] for e in error_events)


class TestCLISessionMultiTurn:
    async def test_second_turn_uses_resume(self, tmp_path: Path) -> None:
        """After first turn, subsequent turns use --resume."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("multi-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("First reply"),
            _content_block_stop(0),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)

        commands_used: list[list[str]] = []

        async def capture_subprocess(*args, **kwargs):
            commands_used.append(list(args))
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_subprocess):
            await session.run_turn("Turn 1", model="opus")
            # Reset events for turn 2
            events.clear()
            await session.run_turn("Turn 2", model="opus")

        # First command should have --session-id
        first_cmd = commands_used[0]
        assert "--session-id" in first_cmd

        # Second command should have --resume
        second_cmd = commands_used[1]
        assert "--resume" in second_cmd


class TestCLISessionCancel:
    async def test_cancel_terminates_process(self, tmp_path: Path) -> None:
        """cancel() should kill the process group via os.killpg."""
        on_event = AsyncMock()
        session = CLISession("cancel-sess", tmp_path, on_event)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        session._process = mock_proc

        with patch("tcg.core.agent.session.os.killpg") as mock_killpg:
            await session.cancel()
            mock_killpg.assert_called_once_with(12345, __import__("signal").SIGTERM)


# ---------------------------------------------------------------------------
# Bug 2 regression — cancellation must not leave the next turn re-using
# the same `--session-id` (which the CLI would reject as "already in use"
# because the per-id transcript file persists after SIGKILL).
# ---------------------------------------------------------------------------


class TestCLISessionCancelRefreshesId:
    """Bug 2: after cancel(), the next CLI invocation must NOT pass
    ``--session-id <same-uuid>`` because the CLI 2.1.85 keeps the
    ``<id>.jsonl`` transcript on disk and rejects re-use of that id.
    """

    async def test_cancel_mints_fresh_session_id(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        original_id = "tainted-uuid-0001"
        session = CLISession(original_id, tmp_path, on_event)

        # Simulate a turn that started but was cancelled mid-stream
        # (so _first_turn was never flipped to False).
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        session._process = mock_proc
        assert session._first_turn is True

        with patch("tcg.core.agent.session.os.killpg"):
            await session.cancel()

        # After cancel, the session id must have changed so the next
        # spawn does NOT collide with the on-disk <id>.jsonl that the
        # CLI left behind.
        assert session.session_id != original_id, (
            "cancel() must mint a fresh session_id to avoid CLI"
            " 'Session ID already in use' on retry"
        )

        # And the next _build_command must use the NEW id with --session-id
        # (since this is effectively a new CLI session).
        cmd = session._build_command("retry", "opus")
        assert "--session-id" in cmd
        assert session.session_id in cmd
        assert original_id not in cmd, (
            "the old (tainted) session id must not appear in the next argv"
        )

    async def test_cancel_resets_first_turn_flag(self, tmp_path: Path) -> None:
        """After a cancel mid-stream, _first_turn must be True so that the
        next spawn opens a fresh CLI session rather than --resume'ing into
        a partially-written transcript."""
        on_event = AsyncMock()
        session = CLISession("uuid-A", tmp_path, on_event)
        # Imagine a previous successful turn flipped it to False
        session._first_turn = False

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 5555
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        session._process = mock_proc

        with patch("tcg.core.agent.session.os.killpg"):
            await session.cancel()

        assert session._first_turn is True, (
            "cancel() must reset _first_turn so the next turn opens a"
            " new CLI session (with the freshly-minted uuid)"
        )


# ---------------------------------------------------------------------------
# Bug 3 regression — the stream parser must NOT hang forever when the
# subprocess stops emitting bytes but keeps stdout open. It must emit a
# visible idle-warning event and continue looping (no kill).
# ---------------------------------------------------------------------------


class TestCLISessionIdleWarning:
    async def test_idle_timeout_emits_warning_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bug 3 (Option B): if readline() blocks for IDLE_TIMEOUT seconds,
        _parse_stream emits {type:status, status:idle_warning, seconds:N}
        and KEEPS LOOPING. It does NOT break and does NOT kill the subprocess.
        Only when bytes finally arrive (or EOF) does the parser proceed.
        """
        # Override the module constant to keep the test fast.
        import tcg.core.agent.session as session_module

        monkeypatch.setattr(session_module, "IDLE_TIMEOUT", 0.1)

        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("idle-sess", tmp_path, on_event)

        # Build a fake stdout reader that:
        #   call 1: hangs (sleep > IDLE_TIMEOUT) -> wait_for must time out
        #   call 2: returns one line of stream-json text_delta
        #   call 3: returns empty bytes (EOF)
        readline_call_count = {"n": 0}

        async def fake_readline() -> bytes:
            readline_call_count["n"] += 1
            n = readline_call_count["n"]
            if n == 1:
                # Hang for longer than IDLE_TIMEOUT to trigger the watchdog
                await asyncio.sleep(1.0)
                return b""  # never reached under wait_for timeout
            if n == 2:
                # After the warning, deliver one real line
                ev = {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "hi"},
                    },
                }
                return (json.dumps(ev) + "\n").encode("utf-8")
            return b""  # EOF

        kill_called = {"n": 0}

        class _Proc:
            returncode = 0
            pid = 1

            def __init__(self) -> None:
                self.stdout = MagicMock()
                self.stdout.readline = fake_readline
                self.stderr = MagicMock()

            def kill(self) -> None:
                kill_called["n"] += 1

            def terminate(self) -> None:
                kill_called["n"] += 1

            async def wait(self) -> int:
                return 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        proc = _Proc()
        session._process = proc  # type: ignore[assignment]

        # Run the parser. Bound it with wait_for so a regression doesn't
        # hang the test suite (the WHOLE point of the fix).
        await asyncio.wait_for(session._parse_stream(), timeout=5.0)

        # Must have emitted at least one idle_warning status event.
        idle_warnings = [
            e
            for e in events
            if e.get("type") == "status" and e.get("status") == "idle_warning"
        ]
        assert len(idle_warnings) >= 1, (
            f"expected at least one idle_warning event, got: {events!r}"
        )
        # The warning must carry an integer/float 'seconds' field
        assert "seconds" in idle_warnings[0]

        # The token event from call 2 must have been processed -- proving
        # the loop CONTINUED past the timeout (not broke out).
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) >= 1, (
            "parser must continue after idle_warning, not break"
        )

        # G8: the watchdog must NOT have killed the subprocess
        assert kill_called["n"] == 0, (
            "G8 violation: idle warning path must not kill the subprocess"
        )
