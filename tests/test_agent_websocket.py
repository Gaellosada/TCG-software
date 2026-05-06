"""WebSocket flow-control tests for the agent endpoint.

Covers state-machine behaviours on ``/ws/agent/{session_id}``:

1. ``stop`` cancels the running turn and clears the queue.
2. ``interrupt`` replaces the running turn with new content.
3. Queue rejects messages once it reaches ``MAX_QUEUE_SIZE``.
4. Queue auto-drains after a turn completes.
5. Connection registry rejects duplicate session_id by closing the old WS.
6. Idle status emitted once all queued turns finish.
7. ``message_complete`` is suppressed when a turn is cancelled.

These tests stub ``CLISession`` so the ``claude`` CLI is never spawned.
The receive loop is exercised end-to-end through a FastAPI ``TestClient``
``websocket_connect``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tcg.core.api.agent as agent_module
from tcg.core.agent.session import CLISession
from tcg.core.api.agent import MAX_QUEUE_SIZE, agent_websocket


# ---------------------------------------------------------------------------
# Helpers: stub workspace, controllable session, app factory
# ---------------------------------------------------------------------------


class StubWorkspace:
    """Minimal stand-in for ``AgentWorkspace``.

    Only implements the methods the WebSocket handler calls. ``get_session``
    must return a meta dict so the handler accepts the connection.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self._sessions: dict[str, dict[str, Any]] = {}
        self.saved: list[tuple[str, list[dict[str, Any]]]] = []

    def register(self, session_id: str) -> None:
        ws_path = self.root / session_id
        ws_path.mkdir(parents=True, exist_ok=True)
        self._sessions[session_id] = {"workspace_path": str(ws_path)}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def load_conversation(self, session_id: str) -> list[dict[str, Any]]:
        return []

    def save_conversation(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        self.saved.append((session_id, list(messages)))


class FakeCLISession:
    """Replacement for ``CLISession`` whose ``run_turn`` is externally driven.

    Each call to ``run_turn`` waits on a fresh ``asyncio.Event`` so tests can
    control when a turn finishes. Cancellation propagates via
    ``task.cancel()`` -> the awaited event raises ``CancelledError``.

    The constructor signature mirrors the real ``CLISession``.
    """

    # Class-level registry so tests can reach into the active session(s).
    instances: list[FakeCLISession] = []

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        on_event: Any,
    ) -> None:
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.on_event = on_event
        self._first_turn = True
        self._cancelled = False
        self.conversation_history: list[dict[str, Any]] = []
        # Per-instance test controls
        self.run_calls: list[dict[str, Any]] = []
        self.complete_event = asyncio.Event()
        self.cancel_calls = 0
        FakeCLISession.instances.append(self)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @is_cancelled.setter
    def is_cancelled(self, value: bool) -> None:
        self._cancelled = bool(value)

    async def run_turn(self, user_message: str, model: str = "opus") -> None:
        self.run_calls.append({"content": user_message, "model": model})
        # Reset event for this turn so multiple turns can be controlled.
        self.complete_event = asyncio.Event()
        try:
            await self.complete_event.wait()
        except asyncio.CancelledError:
            # Mimic real behaviour: subprocess kill, then re-raise
            raise
        # Successful completion: append a synthetic assistant turn so we can
        # assert that history was saved.
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append(
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
        )
        self._first_turn = False

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self._cancelled = True


def _build_app(workspace: StubWorkspace) -> FastAPI:
    """Construct a minimal FastAPI app that mounts the agent WebSocket.

    Avoids ``create_app`` so we sidestep the MongoDB lifespan startup.
    """
    app = FastAPI()
    app.state.agent_workspace = workspace
    app.websocket("/ws/agent/{session_id}")(agent_websocket)
    return app


def _drain_until(
    ws: Any, predicate: Any, max_messages: int = 50
) -> list[dict[str, Any]]:
    """Receive JSON messages until ``predicate(msg)`` is True or we hit a cap.

    Returns the full list of received messages (including the matching one).
    """
    seen: list[dict[str, Any]] = []
    for _ in range(max_messages):
        msg = ws.receive_json()
        seen.append(msg)
        if predicate(msg):
            return seen
    raise AssertionError(
        f"Predicate not satisfied after {max_messages} messages: {seen!r}"
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear the module-level connection registry and instance list before each test."""
    agent_module._active_connections.clear()
    FakeCLISession.instances.clear()
    yield
    agent_module._active_connections.clear()
    FakeCLISession.instances.clear()


@pytest.fixture
def patched_env(tmp_path: Path):
    """Patch ``cli_available`` -> True and ``CLISession`` -> ``FakeCLISession``.

    ``cli_available`` is referenced both as the imported binding inside
    ``tcg.core.api.agent`` and inside ``tcg.core.agent.session``; we patch
    the binding the handler uses.
    """
    workspace = StubWorkspace(tmp_path)
    with (
        patch("tcg.core.api.agent.cli_available", return_value=True),
        patch("tcg.core.api.agent.CLISession", FakeCLISession),
    ):
        yield workspace


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStopCancelsAndClearsQueue:
    """Behaviour 1: ``stop`` cancels the running turn and drains the queue."""

    def test_stop_cancels_running_turn_and_clears_queue(self, patched_env):
        workspace = patched_env
        workspace.register("s1")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s1") as ws:
            # Start a turn (will block forever inside FakeCLISession.run_turn)
            ws.send_json({"type": "message", "content": "hello"})
            # Queue two more behind it
            ws.send_json({"type": "message", "content": "queued-1"})
            ws.send_json({"type": "message", "content": "queued-2"})
            # Ack for the two queued
            assert ws.receive_json() == {"type": "queued"}
            assert ws.receive_json() == {"type": "queued"}

            # Now send stop
            ws.send_json({"type": "stop"})
            seen = _drain_until(ws, lambda m: m.get("type") == "stopped")

        # Verify a stopped event arrived
        assert any(m.get("type") == "stopped" for m in seen)

        # The single fake session should have one run_turn call (the queued
        # ones never started because stop cancelled before drain)
        assert len(FakeCLISession.instances) == 1
        fake = FakeCLISession.instances[0]
        assert len(fake.run_calls) == 1
        assert fake.run_calls[0]["content"] == "hello"
        assert fake.cancel_calls >= 1

    def test_stop_with_no_running_turn_is_noop(self, patched_env):
        """``stop`` while idle must not crash and must not emit stopped."""
        workspace = patched_env
        workspace.register("s1")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s1") as ws:
            ws.send_json({"type": "stop"})
            # Send an empty message after to confirm the loop is still alive
            ws.send_json({"type": "message", "content": ""})
            err = ws.receive_json()
            assert err == {"type": "error", "message": "Empty message"}


class TestInterruptReplacesTurn:
    """Behaviour 2: ``interrupt`` cancels current turn and starts a new one."""

    def test_interrupt_cancels_and_starts_new(self, patched_env):
        workspace = patched_env
        workspace.register("s2")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s2") as ws:
            ws.send_json({"type": "message", "content": "first"})
            # Add one queued message that should be cleared by the interrupt
            ws.send_json({"type": "message", "content": "queued"})
            assert ws.receive_json() == {"type": "queued"}

            ws.send_json({"type": "interrupt", "content": "second"})
            seen = _drain_until(ws, lambda m: m.get("type") == "interrupted")
            assert any(m.get("type") == "interrupted" for m in seen)

            # At this point a NEW FakeCLISession.run_turn should be running.
            # We can't easily synchronise from the outside, so allow a small
            # loop to wait for the second run_turn to register.
            for _ in range(50):
                fake = FakeCLISession.instances[0]
                if len(fake.run_calls) >= 2:
                    break
                # sleeping inside TestClient's portal isn't trivial; we
                # rely on the next receive to drive the loop -- send a
                # cheap ping (an empty message yields an error response)
                ws.send_json({"type": "message", "content": ""})
                got = ws.receive_json()
                assert got["type"] == "error"

            fake = FakeCLISession.instances[0]
            assert len(fake.run_calls) == 2, (
                f"expected 2 run_turn calls, got {len(fake.run_calls)}: "
                f"{fake.run_calls!r}"
            )
            assert fake.run_calls[0]["content"] == "first"
            assert fake.run_calls[1]["content"] == "second"
            assert fake.cancel_calls >= 1

    def test_interrupt_with_empty_content_emits_error(self, patched_env):
        workspace = patched_env
        workspace.register("s2")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s2") as ws:
            ws.send_json({"type": "interrupt", "content": "  "})
            err = ws.receive_json()
            assert err == {"type": "error", "message": "Empty message"}


class TestQueueLimit:
    """Behaviour 3: queue accepts up to ``MAX_QUEUE_SIZE`` then rejects."""

    def test_queue_full_rejection(self, patched_env):
        workspace = patched_env
        workspace.register("s3")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s3") as ws:
            # First message starts the turn (no ack)
            ws.send_json({"type": "message", "content": "running"})

            # Push exactly MAX_QUEUE_SIZE messages -- all should be queued
            for i in range(MAX_QUEUE_SIZE):
                ws.send_json({"type": "message", "content": f"q-{i}"})
                ack = ws.receive_json()
                assert ack == {"type": "queued"}, f"message {i}: {ack!r}"

            # The next one must hit the limit
            ws.send_json({"type": "message", "content": "overflow"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert "Queue full" in err["message"] or "queue" in err["message"].lower()
            assert str(MAX_QUEUE_SIZE) in err["message"]


class TestQueueDrainAfterTurn:
    """Behaviour 4: when a turn finishes, the next queued item runs."""

    def test_queue_drains_after_turn_completes(self, patched_env):
        workspace = patched_env
        workspace.register("s4")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/s4") as ws:
            ws.send_json({"type": "message", "content": "first"})
            ws.send_json({"type": "message", "content": "second"})
            assert ws.receive_json() == {"type": "queued"}

            # Release the first turn -- it must finish, then the queued
            # item ("second") starts. Wait until run_calls includes both.
            for _ in range(100):
                if FakeCLISession.instances:
                    fake = FakeCLISession.instances[0]
                    if len(fake.run_calls) >= 1:
                        # complete current turn; FakeCLISession resets event
                        # at the start of each run_turn so we have to be
                        # careful: trigger the event for the current call.
                        fake.complete_event.set()
                        break
                # nudge the event loop with a no-op ping
                ws.send_json({"type": "message", "content": ""})
                ws.receive_json()

            # Now wait for the queued message to start running. Each turn
            # creates a fresh event, so we only need to observe the second
            # run_turn entry.
            for _ in range(100):
                fake = FakeCLISession.instances[0]
                if len(fake.run_calls) >= 2:
                    break
                ws.send_json({"type": "message", "content": ""})
                ws.receive_json()

            fake = FakeCLISession.instances[0]
            assert len(fake.run_calls) == 2
            assert fake.run_calls[0]["content"] == "first"
            assert fake.run_calls[1]["content"] == "second"

            # Release the second turn so the loop exits cleanly
            fake.complete_event.set()


class TestConnectionRegistryRejectsDuplicate:
    """Behaviour 5: a second connect for the same session_id supersedes."""

    def test_duplicate_connection_supersedes_old(self, patched_env):
        workspace = patched_env
        workspace.register("dup")
        app = _build_app(workspace)
        client = TestClient(app)

        # First client connects
        ws1 = client.websocket_connect("/ws/agent/dup").__enter__()
        # Confirm registry is populated
        assert "dup" in agent_module._active_connections

        # Second client connects -- should close the first
        with client.websocket_connect("/ws/agent/dup") as ws2:
            # The new connection is now the active one
            assert agent_module._active_connections["dup"] is not ws1

            # The old WS should now be closed (server-initiated). Reading
            # from it should raise WebSocketDisconnect with code 1008.
            from starlette.websockets import WebSocketDisconnect

            with pytest.raises(WebSocketDisconnect) as exc:
                # The old client may still receive a few residual messages,
                # but eventually gets a close frame.
                for _ in range(5):
                    ws1.receive_json()
            assert exc.value.code == 1008

            # The new connection is still alive: we can send a no-op
            # empty message and get an error back.
            ws2.send_json({"type": "message", "content": ""})
            err = ws2.receive_json()
            assert err["type"] == "error"

        # Try-close ws1 now (it's already disconnected; suppress errors).
        try:
            ws1.__exit__(None, None, None)
        except Exception:
            pass


class TestIdleStatusEmittedAfterTurns:
    """Behaviour 6: server emits ``status: idle`` after each turn batch."""

    def test_idle_emitted_after_single_turn(self, patched_env):
        workspace = patched_env
        workspace.register("idle1")
        app = _build_app(workspace)
        client = TestClient(app)

        with client.websocket_connect("/ws/agent/idle1") as ws:
            ws.send_json({"type": "message", "content": "go"})

            # Wait for run_turn to be called, then release
            for _ in range(100):
                if FakeCLISession.instances and FakeCLISession.instances[0].run_calls:
                    FakeCLISession.instances[0].complete_event.set()
                    break
                ws.send_json({"type": "message", "content": ""})
                ws.receive_json()

            # Drain until we see the idle status event
            seen = _drain_until(
                ws,
                lambda m: m.get("type") == "status" and m.get("status") == "idle",
                max_messages=20,
            )
            idle_msgs = [
                m for m in seen if m.get("type") == "status" and m.get("status") == "idle"
            ]
            assert len(idle_msgs) >= 1


class TestMessageCompleteSuppressedAfterCancel:
    """Behaviour 7: ``_parse_stream`` suppresses message_complete on cancel.

    This is internal to ``CLISession``; we test it directly using the same
    ``FakeProcess`` pattern as ``tests/unit/test_cli_session.py``.
    """

    async def test_message_complete_not_emitted_when_cancelled(
        self, tmp_path: Path
    ) -> None:
        """When ``_cancelled`` flips during streaming, no message_complete fires."""
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        session = CLISession("cancel-suppress", tmp_path, on_event)

        # Build a stream that emits one text delta then "hangs". We make
        # the FakeProcess yield bytes one line at a time, then mark the
        # session cancelled before the EOF so the loop exits via the
        # ``while not self._cancelled`` guard.
        stream_bytes = (
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text"},
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "partial..."},
                    },
                }
            )
            + "\n"
        )

        # Custom reader: returns the first two lines, then sets _cancelled
        # before the next readline so the loop exits without calling
        # message_complete.
        lines = [line + b"\n" for line in stream_bytes.encode().split(b"\n") if line]
        line_iter = iter(lines)

        async def fake_readline():
            try:
                return next(line_iter)
            except StopIteration:
                # On EOF, mark cancelled so the parser does not emit
                # message_complete on the way out.
                session._cancelled = True
                return b""

        class _Proc:
            returncode = 0
            pid = 1

            def __init__(self):
                self.stdout = AsyncMock()
                self.stdout.readline = fake_readline
                self.stderr = AsyncMock()
                self.stderr.read = AsyncMock(return_value=b"")

            async def wait(self):
                return 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass

        proc = _Proc()
        session._process = proc  # type: ignore[assignment]

        # Run the parser directly (bypasses run_turn so we don't need the
        # full subprocess plumbing).
        await session._parse_stream()

        complete_events = [e for e in events if e.get("type") == "message_complete"]
        assert complete_events == [], (
            f"message_complete must NOT be emitted after cancel, got: "
            f"{complete_events!r}"
        )

        # Sanity: at least one token event WAS emitted (proving we did
        # process some output before the cancel)
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) >= 1
