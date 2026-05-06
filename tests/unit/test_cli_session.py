"""Tests for tcg.core.agent.session -- CLISession subprocess management."""

from __future__ import annotations

import asyncio
import json
import os
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


# ---------------------------------------------------------------------------
# Issue 5 regression -- readline() raising ValueError (LimitOverrunError
# converted to bare ValueError inside asyncio/streams.py) must NOT escape
# _parse_stream. The defensive handler emits an oversized_line warning and
# CONTINUES the loop. No kill, no break, no propagated exception.
# ---------------------------------------------------------------------------


class TestCLISessionOversizedLine:
    """Issue 5: a single stdout line exceeding the StreamReader buffer
    causes ``readline()`` to raise ``ValueError("Separator is found, but
    chunk is longer than limit")``. The parser must catch it, emit a
    visible status event, and keep reading subsequent lines.
    """

    async def test_oversized_line_emits_warning_and_continues(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("oversize-sess", tmp_path, on_event)

        readline_n = {"n": 0}

        async def fake_readline() -> bytes:
            readline_n["n"] += 1
            n = readline_n["n"]
            if n == 1:
                raise ValueError(
                    "Separator is found, but chunk is longer than limit"
                )
            if n == 2:
                # After the warning, deliver one valid stream-json line
                ev = {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "ok"},
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

        session._process = _Proc()  # type: ignore[assignment]

        await asyncio.wait_for(session._parse_stream(), timeout=5.0)

        oversized = [
            e
            for e in events
            if e.get("type") == "status" and e.get("status") == "oversized_line"
        ]
        assert len(oversized) == 1, (
            f"expected exactly one oversized_line event, got: {events!r}"
        )
        # Contract: limit field is the configured StreamReader byte limit.
        assert isinstance(oversized[0].get("limit"), int)
        assert oversized[0]["limit"] >= 64 * 1024  # at least raised above default
        assert "message" in oversized[0]

        # The post-warning token MUST have been processed -- proves continue.
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) >= 1, (
            "parser must continue past the oversized line, not break"
        )

        # G8 / Sign 3: no kill on the defensive path.
        assert kill_called["n"] == 0, (
            "G8 violation: oversized_line path must not kill the subprocess"
        )

    async def test_subprocess_exec_uses_raised_limit(self, tmp_path: Path) -> None:
        """The CLI subprocess must be spawned with limit=STREAM_READER_LIMIT
        so realistic large MCP payloads no longer trip the buffer ceiling.
        """
        import tcg.core.agent.session as session_module

        on_event = AsyncMock()
        session = CLISession("limit-sess", tmp_path, on_event)

        captured_kwargs: dict[str, Any] = {}

        # Minimal stdout that yields immediate EOF so run_turn returns fast.
        async def fake_subprocess(*_args: Any, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return FakeProcess(b"", returncode=0)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await session.run_turn("ping", model="opus")

        assert "limit" in captured_kwargs, (
            "create_subprocess_exec must be called with limit= to raise the"
            " asyncio StreamReader buffer ceiling above the 64 KiB default"
        )
        assert captured_kwargs["limit"] == session_module.STREAM_READER_LIMIT


# ---------------------------------------------------------------------------
# Argv hardening + env injection -- Issues 1 (§4-5) and 4 (§5).
# ---------------------------------------------------------------------------


class TestCLISessionArgvHardening:
    def test_strict_mcp_config_in_command(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        session = CLISession("hard-sess", tmp_path, on_event)
        cmd = session._build_command("hi", "opus")
        assert "--strict-mcp-config" in cmd, (
            "Issue 1 §4: --strict-mcp-config must be passed so the spawned"
            " CLI does NOT merge the user's ~/.claude/settings.json MCP"
            " servers into the agent's context"
        )

    def test_mcp_config_points_to_workspace_file(self, tmp_path: Path) -> None:
        on_event = AsyncMock()
        session = CLISession("hard-sess", tmp_path, on_event)
        cmd = session._build_command("hi", "opus")
        assert "--mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        # The next argv entry must be the absolute path to the workspace's
        # .mcp.json (workspace.py writes this at session-create time).
        expected = str(tmp_path / ".mcp.json")
        assert cmd[idx + 1] == expected, (
            f"--mcp-config must point at workspace .mcp.json; got {cmd[idx + 1]!r}"
        )


class TestCLISessionMongoEnvInjection:
    """Issue 4 §5: the agent's spawned Python (running scripts that import
    ``tcg.backtester.lib.data_load``) needs ``MONGO_URI`` in its env.
    Injecting it via ``env=`` on subprocess spawn sidesteps the .env-walk
    problem (the workspace cwd has no .env)."""

    async def test_subprocess_exec_receives_mongo_uri(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_event = AsyncMock()
        session = CLISession("env-sess", tmp_path, on_event)

        # Force a deterministic MONGO_URI via process env (highest priority
        # in workspace._get_mongo_uri's resolution chain).
        monkeypatch.setenv("MONGO_URI", "mongodb://test-host:27017")

        captured_kwargs: dict[str, Any] = {}

        async def fake_subprocess(*_args: Any, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return FakeProcess(b"", returncode=0)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await session.run_turn("ping", model="opus")

        assert "env" in captured_kwargs, (
            "create_subprocess_exec must be called with env= to inject"
            " MONGO_URI into the spawned CLI's environment"
        )
        env = captured_kwargs["env"]
        assert env.get("MONGO_URI") == "mongodb://test-host:27017"
        # Sanity: PATH (or some other os.environ entry) is preserved -- we
        # MUST NOT strip the parent process env.
        assert "PATH" in env or len(env) > 1


# ---------------------------------------------------------------------------
# Issue 3 -- live mid-turn streaming of ASSUMPTIONS.json. The watchdog
# tick after each parsed CLI event must re-snapshot the file and emit
# ``assumptions_update`` whenever the agent has rewritten it (mtime + sha
# changed).
# ---------------------------------------------------------------------------


def _make_assumptions_payload(*fields: str) -> str:
    """Build a JSON-serialised ASSUMPTIONS.json payload."""
    payload = {
        "version": 1,
        "assumptions": [
            {
                "field": f,
                "value": 1,
                "source": "default",
                "confidence": "high",
                "rationale": "test",
                "group": "execution",
                "editable": True,
            }
            for f in fields
        ],
    }
    return json.dumps(payload)


class TestCLISessionAssumptionsWatchdog:
    """Issue 3: emission cadence changes from post-turn-only to per-event.
    Shape unchanged (full snapshot of ``data["assumptions"]``)."""

    async def test_mid_turn_emit_on_change(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        # Pre-existing ASSUMPTIONS.json (one assumption) so the snapshot
        # tracker has a baseline.
        ap = tmp_path / "ASSUMPTIONS.json"
        ap.write_text(_make_assumptions_payload("a.one"))

        session = CLISession("watchdog-sess", tmp_path, on_event)

        line1 = json.dumps(_text_delta_event("hi ")) + "\n"
        line2 = json.dumps(_text_delta_event("you")) + "\n"
        line3 = json.dumps(_result_success("")) + "\n"

        readline_n = {"n": 0}

        async def fake_readline() -> bytes:
            readline_n["n"] += 1
            n = readline_n["n"]
            if n == 1:
                return line1.encode("utf-8")
            if n == 2:
                # Simulate the agent's mid-turn ASSUMPTIONS.json write
                # right before the next CLI event arrives. Bump mtime
                # by 1 second so it's reliably distinct on FS that
                # coalesce sub-millisecond writes.
                ap.write_text(_make_assumptions_payload("a.one", "b.two"))
                future = ap.stat().st_mtime_ns + 1_000_000_000
                os.utime(ap, ns=(future, future))
                return line2.encode("utf-8")
            if n == 3:
                return line3.encode("utf-8")
            return b""

        class _Proc:
            returncode = 0
            pid = 1

            def __init__(self) -> None:
                self.stdout = MagicMock()
                self.stdout.readline = fake_readline
                self.stderr = MagicMock()

            def kill(self) -> None: ...

            def terminate(self) -> None: ...

            async def wait(self) -> int:
                return 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_subprocess(*_args: Any, **_kwargs: Any) -> Any:
            return _Proc()

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await asyncio.wait_for(
                session.run_turn("hi", model="opus"), timeout=5.0
            )

        au = [e for e in events if e.get("type") == "assumptions_update"]
        # Expect at least 2 emits: mid-turn (watchdog after line 2) and
        # post-turn (existing _check_file_changes safety net).
        assert len(au) >= 2, (
            f"Issue 3: expected >=2 assumptions_update events"
            f" (mid-turn + post-turn), got {len(au)}: {au!r}"
        )
        last_fields = [a["field"] for a in au[-1]["assumptions"]]
        assert "b.two" in last_fields

    async def test_no_emit_on_unchanged(self, tmp_path: Path) -> None:
        """If ASSUMPTIONS.json never changes mid-turn, the watchdog must
        NOT emit any assumptions_update events."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        ap = tmp_path / "ASSUMPTIONS.json"
        ap.write_text(_make_assumptions_payload("a.one"))

        session = CLISession("nochange-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(
            _text_delta_event("hi"),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("hi", model="opus")

        au = [e for e in events if e.get("type") == "assumptions_update"]
        assert au == [], f"Expected no assumptions_update emits, got {au!r}"

    async def test_invalid_json_swallowed(self, tmp_path: Path) -> None:
        """Half-written ASSUMPTIONS.json (partial JSON) must not crash
        the watchdog or emit anything; a subsequent valid write fires
        normally."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        ap = tmp_path / "ASSUMPTIONS.json"
        ap.write_text(_make_assumptions_payload("a.one"))

        session = CLISession("badjson-sess", tmp_path, on_event)
        session._snapshot_file_state()

        # Garbage write + bumped mtime (1s to clear FS coalescing).
        ap.write_text("{not json")
        future = ap.stat().st_mtime_ns + 1_000_000_000
        os.utime(ap, ns=(future, future))

        await session._check_assumptions_changed()
        assert [e for e in events if e.get("type") == "assumptions_update"] == []

        # Valid write afterwards triggers an emit. Bump mtime PAST the
        # last-tracked mtime (NOT past the file's current mtime, which
        # coarse-resolution filesystems may have rounded to the same
        # value as the previous os.utime). The watchdog gate is
        # ``mtime != session._last_assumptions_mtime_ns`` so we make
        # the new mtime monotonically larger than that anchor.
        ap.write_text(_make_assumptions_payload("a.one", "c.three"))
        anchor = session._last_assumptions_mtime_ns or ap.stat().st_mtime_ns
        future2 = anchor + 2_000_000_000
        os.utime(ap, ns=(future2, future2))
        await session._check_assumptions_changed()
        au = [e for e in events if e.get("type") == "assumptions_update"]
        assert len(au) == 1


# ---------------------------------------------------------------------------
# Issue 2 -- BE handling of CLI compaction events.
# ---------------------------------------------------------------------------


class TestCLISessionCompactionEvents:
    async def test_compacting_status_emitted_once(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("compact-sess", tmp_path, on_event)
        ev = {
            "type": "system",
            "subtype": "status",
            "status": "compacting",
            "session_id": "x",
        }
        for _ in range(3):
            await session._handle_event(ev, [], [], {})

        compacting = [
            e
            for e in events
            if e.get("type") == "status" and e.get("status") == "compacting"
        ]
        assert len(compacting) == 1, (
            "compacting must be sticky: only the FIRST occurrence is forwarded"
        )
        assert session._is_compacting is True
        assert session._current_status == "compacting"

    async def test_compact_boundary_emits_compact_done(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("compact-sess", tmp_path, on_event)
        session._is_compacting = True
        session._current_status = "compacting"

        ev = {
            "type": "system",
            "subtype": "compact_boundary",
            "session_id": "x",
            "compact_metadata": {
                "trigger": "auto",
                "pre_tokens": 175296,
                "preserved_segment": {
                    "head_uuid": "h",
                    "anchor_uuid": "a",
                    "tail_uuid": "t",
                },
            },
        }
        await session._handle_event(ev, [], [], {})

        done = [
            e
            for e in events
            if e.get("type") == "status" and e.get("status") == "compact_done"
        ]
        assert len(done) == 1
        assert done[0]["trigger"] == "auto"
        assert done[0]["pre_tokens"] == 175296
        assert done[0]["preserved_segment"]["head_uuid"] == "h"

        assert session._is_compacting is False
        assert session._current_status == "processing"

    async def test_microcompact_boundary_ignored(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("compact-sess", tmp_path, on_event)
        ev = {
            "type": "system",
            "subtype": "microcompact_boundary",
            "session_id": "x",
        }
        await session._handle_event(ev, [], [], {})
        assert events == [], "microcompact_boundary must be silently ignored"

    async def test_synthetic_user_event_ignored(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("compact-sess", tmp_path, on_event)
        ev = {
            "type": "user",
            "isSynthetic": True,
            "session_id": "x",
            "message": {"role": "user", "content": "..."},
        }
        await session._handle_event(ev, [], [], {})
        assert events == []

    async def test_compacting_resets_on_new_turn(self, tmp_path: Path) -> None:
        """If a previous turn ended mid-compaction, the next turn's
        _snapshot_file_state must clear sticky compaction state so a
        fresh compacting event will be re-emitted."""
        on_event = AsyncMock()
        session = CLISession("compact-sess", tmp_path, on_event)
        session._is_compacting = True
        session._current_status = "compacting"
        session._snapshot_file_state()
        assert session._is_compacting is False
        assert session._current_status == "processing"


# ---------------------------------------------------------------------------
# Issue 2 keepalive sticky status -- the WebSocket heartbeat re-emits the
# session's current sticky status string rather than always "processing".
# ---------------------------------------------------------------------------


class TestKeepaliveStickyStatus:
    async def test_keepalive_emits_current_sticky_status(
        self, tmp_path: Path
    ) -> None:
        from tcg.core.api.agent import _keepalive

        sent: list[dict[str, Any]] = []

        class FakeWebSocket:
            async def send_json(self, payload: dict[str, Any]) -> None:
                sent.append(payload)

        on_event = AsyncMock()
        session = CLISession("ka-sess", tmp_path, on_event)
        session._current_status = "compacting"

        task = asyncio.create_task(
            _keepalive(FakeWebSocket(), session=session, interval=0)
        )
        # Drive the loop a few times.
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert sent, "keepalive must have emitted at least one heartbeat"
        for payload in sent:
            assert payload == {"type": "status", "status": "compacting"}, (
                f"keepalive must echo session._current_status,"
                f" got {payload!r}"
            )

    async def test_keepalive_falls_back_to_processing_without_session(
        self,
    ) -> None:
        """Backwards compat: if no session is supplied, behave as before."""
        from tcg.core.api.agent import _keepalive

        sent: list[dict[str, Any]] = []

        class FakeWebSocket:
            async def send_json(self, payload: dict[str, Any]) -> None:
                sent.append(payload)

        task = asyncio.create_task(_keepalive(FakeWebSocket(), interval=0))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert sent
        for payload in sent:
            assert payload == {"type": "status", "status": "processing"}


# ---------------------------------------------------------------------------
# Issue 6 -- silent agent stop. Subprocess EOF mid-turn (no ``result`` event
# was emitted by the CLI) must surface as a visible ``process_exit`` event
# AND must mint a fresh session_id so the next turn does not try to
# ``--resume <tainted_id>`` (which the CLI 2.1.85 rejects as "already in
# use" because the orphaned ``<id>.jsonl`` survives the crashed process).
# ---------------------------------------------------------------------------


def _tool_use_block(index: int = 0, tool_id: str = "t1", name: str = "Write") -> bytes:
    """Encode a complete tool_use stream block PLUS the top-level
    ``assistant`` event the CLI emits at the end of an assistant message
    (mirrors the live transcript). The stream then ends WITHOUT a
    ``result`` event -- simulating a CLI subprocess that crashed
    mid-turn after streaming a tool call."""
    return _make_stream_lines(
        _content_block_start_tool(index, tool_id, name),
        _input_json_delta(index, '{"file_path": "/tmp/x.py", "content": "x"}'),
        _content_block_stop(index),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": {"file_path": "/tmp/x.py", "content": "x"},
                    }
                ]
            },
        },
    )


class TestCLISessionSilentEofProcessExit:
    """Issue 6: subprocess EOF without a ``result`` event must emit
    ``process_exit``, must NOT flip ``_first_turn`` to False, and must
    mint a fresh ``session_id`` so the next turn opens a clean CLI."""

    async def test_silent_eof_emits_process_exit_event(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("eof-sess", tmp_path, on_event)

        # Stream a tool_use block then EOF -- no ``result`` event.
        stdout_data = _tool_use_block()
        # returncode!=0 + content matches the live transcript pattern.
        fake_proc = FakeProcess(stdout_data, returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("draft a script", model="opus")

        process_exits = [e for e in events if e.get("type") == "process_exit"]
        assert len(process_exits) == 1, (
            f"silent EOF must emit exactly one process_exit event;"
            f" got events={events!r}"
        )
        ev = process_exits[0]
        # Contract shape (mandated by orders / A6 §7a):
        assert ev["returncode"] == 1
        assert ev["saw_result"] is False
        assert ev["had_content"] is True
        assert "session_id" in ev
        # stderr_tail is allowed to be None when stderr is empty.
        assert "stderr_tail" in ev

    async def test_silent_eof_refreshes_session_id(self, tmp_path: Path) -> None:
        """The next turn after a silent EOF must NOT use --resume on the
        tainted id. The fresh id is minted, _first_turn stays True so the
        next _build_command emits --session-id <new_id>."""
        on_event = AsyncMock()
        original_id = "tainted-eof-uuid"
        session = CLISession(original_id, tmp_path, on_event)

        # Tool_use streamed, then EOF, no ``result`` event.
        fake_proc = FakeProcess(_tool_use_block(), returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("test", model="opus")

        assert session.session_id != original_id, (
            "silent EOF must mint a fresh session_id (parity with cancel())"
        )
        assert session._first_turn is True, (
            "silent EOF must NOT flip _first_turn -- the next turn opens"
            " a new CLI session, not --resume"
        )

        # Sanity: the next argv emits --session-id <new>, not --resume <old>.
        cmd = session._build_command("next", "opus")
        assert "--session-id" in cmd
        assert "--resume" not in cmd
        assert original_id not in cmd

    async def test_silent_eof_does_not_set_first_turn_false(
        self, tmp_path: Path
    ) -> None:
        """Pre-fix, run_turn fell through to ``_first_turn = False`` even
        on a non-clean exit. This regression guard pins the gating on
        ``_saw_result``."""
        on_event = AsyncMock()
        session = CLISession("eof-sess", tmp_path, on_event)
        assert session._first_turn is True
        assert session._saw_result is False

        fake_proc = FakeProcess(_tool_use_block(), returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("hi", model="opus")

        assert session._saw_result is False, (
            "no ``result`` event was streamed, so _saw_result must be False"
        )
        assert session._first_turn is True, (
            "_first_turn MUST NOT flip on a non-clean exit -- otherwise the"
            " next turn would --resume <tainted_id> and trip 'already in use'"
        )

    async def test_clean_result_event_still_sets_first_turn_false(
        self, tmp_path: Path
    ) -> None:
        """Healthy multi-turn sessions must not regress: a turn that DID
        emit a ``result`` event still flips ``_first_turn`` to False."""
        on_event = AsyncMock()
        session = CLISession("clean-sess", tmp_path, on_event)

        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("hi"),
            _content_block_stop(0),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("ping", model="opus")

        assert session._saw_result is True
        assert session._first_turn is False
        # No process_exit event on the clean path.
        # (events are absorbed by AsyncMock; we re-instrument below.)

    async def test_clean_turn_does_not_emit_process_exit(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("clean-sess", tmp_path, on_event)
        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("ok"),
            _content_block_stop(0),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("ping", model="opus")

        process_exits = [e for e in events if e.get("type") == "process_exit"]
        assert process_exits == [], (
            "clean turns (with ``result``) must NOT emit process_exit"
        )

    async def test_already_in_use_stderr_triggers_retry(
        self, tmp_path: Path
    ) -> None:
        """Issue 6 (§3d): widened retry-guard keyword filter must match
        'already in use' so the CLI 2.1.85 wording on tainted resume is
        recovered as a fresh --session-id retry."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("resume-sess", tmp_path, on_event)
        # Pretend a prior turn succeeded so we are on --resume.
        session._first_turn = False

        # First subprocess: --resume fails with "already in use" stderr,
        # no stdout content, non-zero returncode.
        first_proc = FakeProcess(b"", returncode=1)
        # Use stderr wording that ONLY matches the new "already in use"
        # keyword (not the pre-fix 'session' / 'resume' / 'not found').
        first_proc.communicate = AsyncMock(  # type: ignore[method-assign]
            return_value=(b"", b"Error: id xyz already in use, aborting")
        )
        # Retry subprocess: clean response (would be _retry_as_new_session).
        retry_stdout = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("recovered"),
            _content_block_stop(0),
            _result_success(""),
        )
        retry_proc = FakeProcess(retry_stdout, returncode=0)

        call_count = {"n": 0}

        async def fake_subprocess(*_args: Any, **_kwargs: Any) -> Any:
            call_count["n"] += 1
            return first_proc if call_count["n"] == 1 else retry_proc

        original_id = session.session_id
        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await session.run_turn("retry-me", model="opus")

        # Two spawns total -- the resume failure + the fresh-session retry.
        assert call_count["n"] == 2, (
            "'already in use' stderr must trigger _retry_as_new_session"
        )
        # The retry mints a fresh id and uses --session-id.
        assert session.session_id != original_id
        # Final state is healthy multi-turn (after retry success).
        assert session._first_turn is False
        # Token from retry made it through.
        assert any(
            e.get("type") == "token" and e.get("content") == "recovered"
            for e in events
        )


# ---------------------------------------------------------------------------
# Issue 7 -- conversation lost on session-switch. The user message must be
# appended to ``conversation_history`` and persisted (via on_persist callback)
# BEFORE any cancellable await; on cancel/error mid-turn, whatever assistant
# text streamed must be flushed as a synthetic ``interrupted`` entry.
# ---------------------------------------------------------------------------


class TestCLISessionIncrementalPersistence:
    """Issue 7: incremental conversation save -- user message at turn-start,
    partial assistant on cancel/error. The api/agent.py finally-block save
    becomes a defensive backstop, not the only persistence path."""

    async def test_user_message_appended_before_subprocess_spawn(
        self, tmp_path: Path
    ) -> None:
        """Issue 7 root-cause: pre-fix, the user message was appended only
        on success (session.py:214), so a cancel/disconnect mid-turn lost
        it. Post-fix, it's appended at run_turn entry, before any
        cancellable await. We verify by checking history mid-spawn."""
        on_event = AsyncMock()
        persisted: list[list[dict[str, Any]]] = []

        async def on_persist(msgs: list[dict[str, Any]]) -> None:
            # Capture a deep-ish copy via list() so subsequent mutations
            # don't retroactively change snapshots.
            persisted.append([dict(m) for m in msgs])

        session = CLISession("persist-sess", tmp_path, on_event)
        session._on_persist = on_persist

        captured_history_at_spawn: list[list[dict[str, Any]]] = []

        async def fake_subprocess(*_args: Any, **_kwargs: Any) -> Any:
            # Snapshot conversation_history at the moment of spawn --
            # PROVES the user-append happened BEFORE the subprocess
            # was created (i.e. before any cancellable await).
            captured_history_at_spawn.append(
                [dict(m) for m in session.conversation_history]
            )
            return FakeProcess(
                _make_stream_lines(
                    _content_block_start_text(0),
                    _text_delta_event("ok"),
                    _content_block_stop(0),
                    _result_success(""),
                ),
                returncode=0,
            )

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await session.run_turn("hello world", model="opus")

        assert captured_history_at_spawn, "subprocess must have been spawned"
        snap = captured_history_at_spawn[0]
        assert snap == [{"role": "user", "content": "hello world"}], (
            "the user message MUST be appended to conversation_history"
            " BEFORE the subprocess is spawned (Issue 7 incremental save)"
        )
        # And ``on_persist`` must have fired at least once with that snapshot.
        assert any(
            entry == [{"role": "user", "content": "hello world"}]
            for entry in persisted
        ), (
            "on_persist must be invoked with the user-only history at"
            f" turn start; got {persisted!r}"
        )

    async def test_cancel_mid_turn_flushes_partial_assistant(
        self, tmp_path: Path
    ) -> None:
        """The cancel path must append a synthetic ``interrupted`` entry
        carrying whatever streamed-so-far text, so a session-switch
        navigates away with a complete-enough on-disk record."""
        events: list[dict[str, Any]] = []
        persisted: list[list[dict[str, Any]]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        async def on_persist(msgs: list[dict[str, Any]]) -> None:
            persisted.append([dict(m) for m in msgs])

        session = CLISession("cancel-sess", tmp_path, on_event)
        session._on_persist = on_persist

        # Build a process that streams 2 tokens, then awaits forever
        # (simulating mid-turn) so we can cancel before result.
        readline_n = {"n": 0}
        hang_event = asyncio.Event()

        async def fake_readline() -> bytes:
            readline_n["n"] += 1
            n = readline_n["n"]
            if n == 1:
                return (json.dumps(_content_block_start_text(0)) + "\n").encode()
            if n == 2:
                return (json.dumps(_text_delta_event("partial ")) + "\n").encode()
            if n == 3:
                return (json.dumps(_text_delta_event("response")) + "\n").encode()
            # n>=4: hang until cancelled.
            await hang_event.wait()
            return b""

        class _Proc:
            returncode = None  # alive
            pid = 1

            def __init__(self) -> None:
                self.stdout = MagicMock()
                self.stdout.readline = fake_readline
                self.stderr = MagicMock()

            def kill(self) -> None:
                # Simulating a kill: returncode flips so cleanup can complete.
                _Proc.returncode = -9  # type: ignore[assignment]

            def terminate(self) -> None:
                pass

            async def wait(self) -> int:
                return -9

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_subprocess(*_args: Any, **_kwargs: Any) -> Any:
            return _Proc()

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            task = asyncio.create_task(
                session.run_turn("draft", model="opus")
            )
            # Wait until at least 2 tokens streamed.
            for _ in range(200):
                await asyncio.sleep(0.005)
                token_events = [
                    e for e in events if e.get("type") == "token"
                ]
                if len(token_events) >= 2:
                    break
            assert (
                len([e for e in events if e.get("type") == "token"]) >= 2
            ), f"tokens not streamed; events={events!r}"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # User message must be present (appended at turn start).
        roles = [m.get("role") for m in session.conversation_history]
        assert roles == ["user", "assistant"], (
            f"history must contain user + interrupted assistant;"
            f" got roles={roles!r}, history={session.conversation_history!r}"
        )
        user_entry = session.conversation_history[0]
        assert user_entry == {"role": "user", "content": "draft"}
        assistant_entry = session.conversation_history[1]
        assert assistant_entry.get("interrupted") is True
        # The streamed text was buffered and flushed.
        flat = json.dumps(assistant_entry["content"])
        assert "partial " in flat or "response" in flat, (
            "partial assistant text must be captured;"
            f" got {assistant_entry!r}"
        )

        # on_persist must have been invoked at least twice: once at
        # user-append (start), once at partial-flush (cancel).
        assert len(persisted) >= 2, (
            f"on_persist must fire at user-append AND partial-flush;"
            f" got {len(persisted)} invocations"
        )
        # Last persisted snapshot must include the interrupted assistant.
        last = persisted[-1]
        assert any(
            m.get("role") == "assistant" and m.get("interrupted") is True
            for m in last
        ), f"last persisted snapshot missing interrupted assistant: {last!r}"

    async def test_persist_called_idempotent_at_turn_complete(
        self, tmp_path: Path
    ) -> None:
        """Successful turn: on_persist fires at user-append AND at
        assistant-append. The cancel/error flush path must NOT
        re-append (idempotency)."""
        on_event = AsyncMock()
        persisted: list[list[dict[str, Any]]] = []

        async def on_persist(msgs: list[dict[str, Any]]) -> None:
            persisted.append([dict(m) for m in msgs])

        session = CLISession("idem-sess", tmp_path, on_event)
        session._on_persist = on_persist

        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("ok"),
            _content_block_stop(0),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("hi", model="opus")

        # History: user + assistant (no interrupted entry, no double).
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["role"] == "user"
        assert session.conversation_history[1]["role"] == "assistant"
        assert "interrupted" not in session.conversation_history[1]

        # on_persist fired exactly twice (user, then assistant).
        assert len(persisted) == 2, (
            f"expected exactly 2 on_persist calls (user + assistant);"
            f" got {len(persisted)}: {persisted!r}"
        )

    async def test_persist_failure_does_not_break_turn(
        self, tmp_path: Path
    ) -> None:
        """on_persist exceptions must be swallowed -- a save failure
        (e.g. disk full, permission) must NOT abort the running turn."""
        on_event = AsyncMock()

        async def flaky_persist(_msgs: list[dict[str, Any]]) -> None:
            raise OSError("disk full")

        session = CLISession("flaky-sess", tmp_path, on_event)
        session._on_persist = flaky_persist

        stdout_data = _make_stream_lines(
            _content_block_start_text(0),
            _text_delta_event("ok"),
            _content_block_stop(0),
            _result_success(""),
        )
        fake_proc = FakeProcess(stdout_data, returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await session.run_turn("hi", model="opus")

        # Turn completed despite the failing persist.
        assert session._first_turn is False
        assert len(session.conversation_history) == 2


# ---------------------------------------------------------------------------
# Issue 10 -- subagent_count: emit count-change events as Task/Agent
# tool_uses start and complete. Renders a running-subagent badge on
# the FE (clears at 0).
# ---------------------------------------------------------------------------


def _assistant_tool_use_event(tool_id: str, name: str) -> dict[str, Any]:
    """Top-level ``assistant`` event carrying a single tool_use block.

    Mirrors the live CLI shape (subagent spawn arrives in the
    ``assistant`` message content)."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {"description": "..."},
                }
            ]
        },
    }


def _user_tool_result_event(tool_id: str) -> dict[str, Any]:
    """Top-level ``user`` event carrying a tool_result block.

    Mirrors the CLI's synthetic tool-response message shape."""
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": [{"type": "text", "text": "subagent done"}],
                }
            ]
        },
    }


class TestCLISessionSubagentCount:
    """Issue 10: BE emits ``subagent_count`` events on Task/Agent
    tool_use start (count up) and matching tool_result (count down).
    Only emits on changes; does NOT spam duplicate values."""

    async def test_two_subagents_then_one_resolves(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("subagent-sess", tmp_path, on_event)

        # Two Task tool_uses, then one tool_result -- final count = 1.
        await session._handle_event(
            _assistant_tool_use_event("t1", "Task"), [], [], {}
        )
        await session._handle_event(
            _assistant_tool_use_event("t2", "Task"), [], [], {}
        )
        await session._handle_event(_user_tool_result_event("t1"), [], [], {})

        counts = [
            e["count"]
            for e in events
            if e.get("type") == "subagent_count"
        ]
        assert counts == [1, 2, 1], (
            f"expected count transitions 1 -> 2 -> 1; got {counts!r}"
        )

    async def test_no_emit_on_non_subagent_tool(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("subagent-sess", tmp_path, on_event)
        # Read is not a subagent tool -- must not emit.
        await session._handle_event(
            _assistant_tool_use_event("r1", "Read"), [], [], {}
        )
        assert [e for e in events if e.get("type") == "subagent_count"] == []

    async def test_agent_name_also_tracked(self, tmp_path: Path) -> None:
        """The brief flagged uncertainty whether the CLI uses 'Task' or
        'Agent'. We accept both defensively. CLI 2.1.85 emits 'Task'
        -- this is the future-proofing guard."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("subagent-sess", tmp_path, on_event)
        await session._handle_event(
            _assistant_tool_use_event("a1", "Agent"), [], [], {}
        )
        counts = [
            e["count"]
            for e in events
            if e.get("type") == "subagent_count"
        ]
        assert counts == [1]

    async def test_count_returns_to_zero_on_completion(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("subagent-sess", tmp_path, on_event)
        await session._handle_event(
            _assistant_tool_use_event("t1", "Task"), [], [], {}
        )
        await session._handle_event(_user_tool_result_event("t1"), [], [], {})
        counts = [
            e["count"]
            for e in events
            if e.get("type") == "subagent_count"
        ]
        assert counts == [1, 0], (
            f"badge must clear (count=0) when last subagent finishes;"
            f" got {counts!r}"
        )

    async def test_no_duplicate_emits_for_same_count(
        self, tmp_path: Path
    ) -> None:
        """A spurious tool_result for an unknown id must NOT emit a
        redundant count event."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("subagent-sess", tmp_path, on_event)
        await session._handle_event(
            _assistant_tool_use_event("t1", "Task"), [], [], {}
        )
        # Tool result for an id we never tracked -- no-op.
        await session._handle_event(
            _user_tool_result_event("unknown-id"), [], [], {}
        )
        counts = [
            e["count"]
            for e in events
            if e.get("type") == "subagent_count"
        ]
        assert counts == [1], (
            f"unknown tool_use_id must not emit a redundant count;"
            f" got {counts!r}"
        )


# ---------------------------------------------------------------------------
# Issue 11 -- token_usage: cumulative session totals emitted after each
# ``result`` event. Field shape verified against CLI 2.1.85 stream-json.
# ---------------------------------------------------------------------------


def _result_with_usage(
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> dict[str, Any]:
    """Build a top-level ``result`` event with the exact ``usage`` shape
    the CLI 2.1.85 emits (verified by capturing live stream-json output)."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }


class TestCLISessionTokenUsage:
    """Issue 11: BE accumulates input/output tokens from each ``result``
    event's ``usage`` block and emits a ``token_usage`` event with
    cumulative session totals. Cache creation + cache read fold into
    the input total so the FE footer reflects full billed input."""

    async def test_single_result_emits_token_usage(
        self, tmp_path: Path
    ) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("tok-sess", tmp_path, on_event)
        await session._handle_event(
            _result_with_usage(input_tokens=5, output_tokens=10), [], [], {}
        )

        usage_events = [
            e for e in events if e.get("type") == "token_usage"
        ]
        assert len(usage_events) == 1
        ev = usage_events[0]
        assert ev["session_input"] == 5
        assert ev["session_output"] == 10
        assert ev["session_total"] == 15

    async def test_cumulative_across_two_turns(self, tmp_path: Path) -> None:
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("tok-sess", tmp_path, on_event)
        await session._handle_event(
            _result_with_usage(input_tokens=2, output_tokens=14), [], [], {}
        )
        await session._handle_event(
            _result_with_usage(input_tokens=3, output_tokens=20), [], [], {}
        )

        usage_events = [
            e for e in events if e.get("type") == "token_usage"
        ]
        assert len(usage_events) == 2
        # Monotonic non-decreasing.
        assert usage_events[0]["session_input"] == 2
        assert usage_events[0]["session_output"] == 14
        assert usage_events[1]["session_input"] == 5
        assert usage_events[1]["session_output"] == 34
        assert usage_events[1]["session_total"] == 39

    async def test_cache_tokens_folded_into_input(
        self, tmp_path: Path
    ) -> None:
        """``cache_creation_input_tokens`` and ``cache_read_input_tokens``
        contribute to ``session_input`` -- the FE footer should
        reflect the total billed input volume, not just the
        non-cached portion."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("tok-sess", tmp_path, on_event)
        await session._handle_event(
            _result_with_usage(
                input_tokens=2,
                output_tokens=14,
                cache_creation=9668,
                cache_read=11370,
            ),
            [],
            [],
            {},
        )
        usage_events = [
            e for e in events if e.get("type") == "token_usage"
        ]
        assert len(usage_events) == 1
        # 2 + 9668 + 11370 = 21040
        assert usage_events[0]["session_input"] == 21040
        assert usage_events[0]["session_output"] == 14

    async def test_missing_usage_is_safe(self, tmp_path: Path) -> None:
        """A ``result`` event without ``usage`` field must not crash and
        must not emit a ``token_usage`` with negative/garbage totals.

        The current implementation emits a 0/0/0 token_usage event in
        that case. This is acceptable: it confirms the result event
        was processed AND keeps the FE's cumulative state honest."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("tok-sess", tmp_path, on_event)
        await session._handle_event(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
            },
            [],
            [],
            {},
        )
        # Implementation choice: emit 0/0/0 (cumulative unchanged).
        usage_events = [
            e for e in events if e.get("type") == "token_usage"
        ]
        assert len(usage_events) == 1
        assert usage_events[0]["session_input"] == 0
        assert usage_events[0]["session_output"] == 0


# ---------------------------------------------------------------------------
# Issue 14 -- MDB_MCP_CONNECTION_STRING injection. The agent's spawned CLI
# (and hence the workspace's mongodb-mcp-server child) must see the same
# URI as the Python lib at runtime, regardless of when the .mcp.json was
# written.
# ---------------------------------------------------------------------------


class TestCLISessionMdbMcpConnectionStringInjection:
    """Issue 14: ``MDB_MCP_CONNECTION_STRING`` must be set in the
    spawned subprocess env to mirror ``MONGO_URI``. Closes the
    temporal gap between session-creation-time .mcp.json snapshots
    and runtime .env reads (A14 §3 Path A vs B)."""

    async def test_subprocess_exec_receives_mdb_mcp_connection_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_event = AsyncMock()
        session = CLISession("mdb-sess", tmp_path, on_event)

        monkeypatch.setenv(
            "MONGO_URI", "mongodb://prod-host:27017/?replicaSet=rs0"
        )

        captured_kwargs: dict[str, Any] = {}

        async def fake_subprocess(*_args: Any, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return FakeProcess(b"", returncode=0)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess
        ):
            await session.run_turn("ping", model="opus")

        env = captured_kwargs.get("env", {})
        assert env.get("MONGO_URI") == (
            "mongodb://prod-host:27017/?replicaSet=rs0"
        )
        assert env.get("MDB_MCP_CONNECTION_STRING") == env.get("MONGO_URI"), (
            "Issue 14: MDB_MCP_CONNECTION_STRING must mirror MONGO_URI"
            " so the spawned mongodb-mcp-server reads the live URI"
            " rather than the (potentially stale) .mcp.json snapshot"
        )
