"""Agent router -- REST endpoints for session management + WebSocket for chat.

The WebSocket endpoint is mounted at ``/ws/agent/{session_id}`` to avoid
prefix-routing issues with FastAPI's ``APIRouter``.  The REST endpoints
live under ``/api/agent``.

This module uses the Claude CLI (subprocess) for agent conversations,
requiring no Anthropic API key — only the ``claude`` binary on PATH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tcg.core.agent.session import (
    CLISession,
    _concat_text_blocks,
    _detect_unmet_intent,
    _has_done_marker,
    cli_available,
)
from tcg.core.agent.workspace import AgentWorkspace

logger = logging.getLogger(__name__)

# The CLI accepts full model names directly
ALLOWED_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}

# Maximum number of messages that can be queued while a turn is running
MAX_QUEUE_SIZE = 10


def _resolve_max_auto_continue() -> int:
    """Resolve ``MAX_AUTO_CONTINUE`` from env (fallback 5; clamp [1, 50]).

    Issue 23 (Round 6): the iteration cap for the auto-continue harness
    loop is ``5`` by default. Operators can override via the environment
    variable ``TCG_AGENT_MAX_AUTO_CONTINUE``. Parse failures fall back
    to the default; values outside the safe range ``[1, 50]`` are
    clamped (1 below, 50 above) so a pathological env does not
    silently disable the cap or produce a runaway loop.
    """
    raw = os.environ.get("TCG_AGENT_MAX_AUTO_CONTINUE")
    if raw is None:
        return 5
    try:
        value = int(raw)
    except ValueError:
        return 5
    if value < 1:
        return 1
    if value > 50:
        return 50
    return value


# Issue 23 (Round 6): max auto-continue iterations per user-driven turn.
# Read from env at import time -- tests that need to override this
# patch the module attribute directly (no live env-var reload).
MAX_AUTO_CONTINUE: int = _resolve_max_auto_continue()

# Issue 23 (Round 6): build the in-band assistant message appended once
# the auto-continue cap is reached.  Persisted to disk via the existing
# ``_on_persist`` callback so it survives a reload.
# N2 fix: dynamic cap value (not hardcoded "5") so operator overrides
# via TCG_AGENT_MAX_AUTO_CONTINUE are reflected in the message.
def _build_auto_continue_cap_message(max_iters: int) -> str:
    return (
        f"Auto-continue cap ({max_iters}) reached. The task may not be complete. "
        "Please redirect via the input box if more work is needed."
    )


# Module-level alias resolved at import time (used by tests that check the
# message text without knowing the current MAX_AUTO_CONTINUE value).
_AUTO_CONTINUE_CAP_MESSAGE: str = _build_auto_continue_cap_message(MAX_AUTO_CONTINUE)

# Registry of active WebSocket connections per session_id.
# Prevents concurrent connections from corrupting session state.
_active_connections: dict[str, WebSocket] = {}

# Issue 9 (Option B): pending ``turn_aborted`` notifications keyed by
# session_id. Populated in the WS handler ``finally`` block when a
# turn was in flight at the moment the WS dropped (cancel-mid-turn).
# Drained on the next connect for the same session_id, immediately
# AFTER the ``history`` payload, then deleted. One-shot per
# cancelled-mid-turn event; never accumulates over time. Single
# FastAPI event loop -> serialized access -> no lock required.
_pending_abort_notifications: dict[str, dict[str, Any]] = {}

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ------------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    name: str | None = None


class RenameSessionRequest(BaseModel):
    name: str


# ------------------------------------------------------------------
# Dependency helpers
# ------------------------------------------------------------------


def _get_workspace(request: Request) -> AgentWorkspace:
    return request.app.state.agent_workspace


# ------------------------------------------------------------------
# REST endpoints
# ------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    """List all agent sessions."""
    workspace = _get_workspace(request)
    return workspace.list_sessions()


@router.post("/sessions")
async def create_session(
    request: Request, body: CreateSessionRequest
) -> dict[str, Any]:
    """Create a new agent session."""
    workspace = _get_workspace(request)
    return workspace.create_session(name=body.name)


@router.patch("/sessions/{session_id}")
async def rename_session(
    request: Request, session_id: str, body: RenameSessionRequest
) -> Any:
    """Rename an agent session."""
    workspace = _get_workspace(request)
    updated = workspace.rename_session(session_id, body.name)
    if updated is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "session_not_found",
                "message": f"No session {session_id}",
            },
        )
    return updated


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> dict[str, str]:
    """Delete an agent session and its workspace."""
    workspace = _get_workspace(request)
    deleted = workspace.delete_session(session_id)
    if not deleted:
        return {"status": "not_found"}
    return {"status": "deleted"}


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> dict[str, Any]:
    """Get metadata for a single session."""
    workspace = _get_workspace(request)
    session = workspace.get_session(session_id)
    if session is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={
                "error": "session_not_found",
                "message": f"No session {session_id}",
            },
        )
    return session


@router.get("/sessions/{session_id}/conversation")
async def get_conversation(request: Request, session_id: str) -> list[dict[str, Any]]:
    """Load the saved conversation for a session."""
    workspace = _get_workspace(request)
    return workspace.load_conversation(session_id)


@router.get("/sessions/{session_id}/notebook")
async def get_notebook(request: Request, session_id: str) -> Any:
    """Return the compiled notebook as JSON (for frontend rendering).

    Reads results/notebook.ipynb from the session workspace and returns
    the parsed nbformat JSON structure.
    """
    workspace = _get_workspace(request)
    session_meta = workspace.get_session(session_id)
    if session_meta is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "session_not_found",
                "message": f"No session {session_id}",
            },
        )

    notebook_path = Path(session_meta["workspace_path"]) / "results" / "notebook.ipynb"
    if not notebook_path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "error": "notebook_not_found",
                "message": "No notebook compiled yet",
            },
        )

    try:
        content = notebook_path.read_text(encoding="utf-8")
        return json.loads(content)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "notebook_read_failed", "message": str(exc)},
        )
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "notebook_read_failed", "message": str(exc)},
        )


@router.get("/sessions/{session_id}/assumptions")
async def get_assumptions(request: Request, session_id: str) -> Any:
    """Return the session's ASSUMPTIONS.json content."""
    workspace = _get_workspace(request)
    session_meta = workspace.get_session(session_id)
    if session_meta is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "session_not_found",
                "message": f"No session {session_id}",
            },
        )

    return workspace.load_assumptions(session_id)


@router.get("/health")
async def agent_health(request: Request) -> dict[str, Any]:
    """Check whether the agent feature is available.

    The agent is available when the ``claude`` CLI binary is found on PATH.
    No API key is required — the CLI handles its own authentication.
    """
    available = cli_available()
    return {
        "available": available,
        "model": "claude-sonnet-4-6" if available else None,
    }


# ------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------

# This is a standalone function that will be mounted on the app directly
# rather than via the router, to avoid FastAPI WebSocket prefix issues.


async def _keepalive(
    websocket: WebSocket,
    session: CLISession | None = None,
    interval: int = 30,
) -> None:
    """Send periodic heartbeat events to keep the WebSocket alive.

    APPLICATION-LEVEL keepalive only -- this is independent of the
    WebSocket-protocol ping/pong handled by uvicorn (default
    ``ws_ping_interval=20``, ``ws_ping_timeout=20``). The protocol-level
    ping is automatically answered by browsers without JS involvement,
    so even very long-running turns (>10 min) do NOT require this
    heartbeat to stay connected. Its purpose is to (a) keep
    intermediary proxies/load balancers from idle-closing the
    connection and (b) feed the frontend a periodic
    ``{"type":"status","status":<sticky>}`` so the UI can
    differentiate "still working" from "frozen". No turn timeout is
    derived from it.

    Issue 2 sticky-status fix: instead of always emitting
    ``processing``, we emit the session's CURRENT sticky status
    (e.g. ``compacting`` while a CLI compaction is in progress).
    This avoids last-writer-wins clobbering on the FE: the BE-side
    ``_handle_event`` now translates ``system/status:"compacting"``
    into a ``compacting`` event, and the keepalive must NOT
    overwrite that with ``processing`` 30 s later. Reading
    ``session._current_status`` is cheap and removes the race
    entirely (no need for a pause-keepalive flag dance).

    If no session is supplied (legacy callers / tests) we fall back
    to the previous ``processing`` behaviour.

    Designed to be run as a child asyncio.Task that is cancelled when the
    turn ends.  Uses only ``asyncio.CancelledError`` for shutdown -- no
    flag polling -- so cancellation of the parent naturally stops this.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            status = (
                session._current_status
                if session is not None
                else "processing"
            )
            await websocket.send_json({"type": "status", "status": status})
    except asyncio.CancelledError:
        raise
    except Exception:
        # WebSocket is dead -- let the caller handle it via task cancellation
        pass


def _build_continuation_message(reason: str, phrase: str = "") -> str:
    """Compose the continuation prompt sent to the CLI by the wrapper.

    Issue 23 (Round 6): two reason codes per the contract --
    ``"missing_done_marker"`` (primary, marker absent) and
    ``"unmet_intent"`` (fallback, marker present but text announces
    work that was not performed via ``tool_use``). The continuation
    text is plain English -- no JSON, no marker -- so the agent treats
    it as ordinary user feedback rather than a synthetic event.
    """
    if reason == "unmet_intent":
        # Inline the matched phrase if we have one -- helps the agent
        # locate the offending sentence in its own prior message.
        excerpt = phrase if phrase else "future work"
        return (
            f"Your message announced future work ('{excerpt}') but the "
            "marker is present. Either complete that work and re-emit "
            "the marker, or restate that the work is intentionally "
            "deferred."
        )
    # Default / "missing_done_marker"
    return (
        "Your last response did not end with the handoff marker "
        "`<<<TURN_HANDOFF_DONE>>>`. If you are truly done, end with "
        "the marker on its own line. Otherwise continue your task and "
        "emit the marker only when complete."
    )


def _evaluate_auto_continue(
    session: CLISession,
) -> tuple[bool, str, str]:
    """Decide whether to auto-continue based on the last assistant message.

    Returns ``(should_continue, reason, phrase)``:
    - ``should_continue=False`` if the last message has the marker AND
      no unmet intent (clean end), or there is no assistant message at
      all (defensive). The wrapper falls through and emits ``idle``.
    - ``should_continue=True`` otherwise; ``reason`` is one of
      ``"missing_done_marker"`` / ``"unmet_intent"``.
    """
    history = session.conversation_history
    if not history:
        return False, "", ""
    last = history[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return False, "", ""
    content = last.get("content")
    text = _concat_text_blocks(content)
    has_marker = _has_done_marker(text)
    if not has_marker:
        return True, "missing_done_marker", ""
    unmet, phrase = _detect_unmet_intent(text, content)
    if unmet:
        return True, "unmet_intent", phrase
    return False, "", ""


async def _execute_single_turn(
    session: CLISession,
    content: str,
    model: str,
    ws: WebSocket,
    workspace: AgentWorkspace,
    session_id: str,
    request_id: str,
) -> None:
    """Run one turn: spawn heartbeat, execute, cancel heartbeat, save.

    Extracted to eliminate DRY violation between the initial turn and
    queued-message processing.
    """
    logger.info(
        "[%s] Executing turn for session %s (model=%s)",
        request_id,
        session_id,
        model,
    )
    session.is_cancelled = False
    heartbeat = asyncio.create_task(_keepalive(ws, session=session, interval=30))
    try:
        await session.run_turn(content, model=model)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

    # Persist conversation after each turn
    try:
        workspace.save_conversation(session_id, session.conversation_history)
    except Exception:
        logger.warning(
            "[%s] Failed to save conversation for %s", request_id, session_id
        )


async def agent_websocket(websocket: WebSocket, session_id: str) -> None:
    """WebSocket handler for agent chat.

    Protocol (JSON messages):
    - Client sends:  ``{"type": "message"|"stop"|"interrupt", ...}``
    - Server sends:  ``{"type": "token"|"tool_call"|"tool_result"|"message_complete"|"error"|"history"|"stopped"|"queued"|"interrupted"|"status", ...}``

    Flow control:
    - ``stop``      -- cancel the running turn and clear the queue.
    - ``interrupt``  -- cancel the running turn, then start a new one.
    - ``message`` while busy -- queue it; auto-processed after the current turn.
    """
    # Check that claude CLI is available
    if not cli_available():
        await websocket.close(
            code=1008, reason="Agent feature not available (claude CLI not found)"
        )
        return

    workspace: AgentWorkspace = websocket.app.state.agent_workspace

    # Validate session exists
    session_meta = workspace.get_session(session_id)
    if session_meta is None:
        await websocket.close(code=1008, reason=f"Session {session_id} not found")
        return

    # --- Concurrent connection guard (#4) ---
    # If another WebSocket is already connected to this session, close it.
    old_ws = _active_connections.get(session_id)
    if old_ws is not None:
        try:
            await old_ws.close(code=1008, reason="Superseded by new connection")
        except Exception:
            pass  # Already dead

    await websocket.accept()
    _active_connections[session_id] = websocket

    workspace_path = Path(session_meta["workspace_path"])

    # Event callback that sends to WebSocket. Passes through every
    # event type unchanged: ``token``, ``tool_call``, ``tool_result``,
    # ``message_complete``, ``status``, ``error``, ``process_exit``,
    # ``turn_complete`` (Issue 16b -- positive end-of-turn marker),
    # ``token_usage``, ``subagent_count``, etc. No special handling
    # needed for ``turn_complete`` -- it ships as plain JSON like
    # ``result``-derived events.
    async def on_event(event: dict[str, Any]) -> None:
        try:
            await websocket.send_json(event)
        except Exception:
            # WebSocket is dead -- cancel the session so the parse loop stops
            logger.warning("WebSocket dead for session %s, cancelling", session_id)
            session.is_cancelled = True

    # Create the CLI session
    session = CLISession(
        session_id=session_id,
        workspace_path=workspace_path,
        on_event=on_event,
    )

    # Issue 7 (incremental save): wire the workspace's
    # ``save_conversation`` to the session via the ``_on_persist``
    # attribute. The session calls this at user-append (start of
    # turn), assistant-append (end of turn), and partial-append
    # (cancel/error). The finally-block save below remains as a
    # defensive backstop. Wired post-construction (rather than as a
    # constructor kwarg) to keep the test-double constructor in
    # ``tests/test_agent_websocket.py`` unchanged (Sign 4).
    async def on_persist(messages: list[dict[str, Any]]) -> None:
        try:
            workspace.save_conversation(session_id, messages)
        except Exception:
            logger.warning(
                "Incremental save failed for session %s", session_id
            )

    session._on_persist = on_persist

    # Load any prior conversation history
    prior_messages = workspace.load_conversation(session_id)
    if prior_messages:
        session.conversation_history = prior_messages
        # If there are prior messages, this is a resumed session
        session._first_turn = False

    # Send conversation history to client on connect
    if prior_messages:
        try:
            await websocket.send_json({"type": "history", "messages": prior_messages})
        except Exception:
            logger.warning("Failed to send history for session %s", session_id)

    # Issue 9 (Option B): if the prior connection for THIS session_id
    # was killed mid-turn (cancel-on-WS-disconnect), surface it now
    # as a sibling event to ``process_exit`` so the FE can clear
    # ``hasInFlightTurnRef`` and show a banner. One-shot: drained
    # and removed after a single emit.
    pending_abort = _pending_abort_notifications.pop(session_id, None)
    if pending_abort is not None:
        try:
            await websocket.send_json(
                {
                    "type": "turn_aborted",
                    "reason": pending_abort.get("reason", "ws_disconnect"),
                    "session_id": session_id,
                    "had_partial_content": bool(
                        pending_abort.get("had_partial_content", False)
                    ),
                }
            )
        except Exception:
            logger.warning(
                "Failed to send turn_aborted for session %s", session_id
            )

    # ------------------------------------------------------------------
    # Task-based turn management
    # ------------------------------------------------------------------

    turn_task: asyncio.Task[None] | None = None
    queued_messages: deque[dict[str, Any]] = deque()

    def _on_turn_done(task: asyncio.Task[None]) -> None:
        """Log uncaught exceptions from turn tasks (avoids 'never retrieved')."""
        if not task.cancelled():
            exc = task.exception()
            if exc:
                # Use logger.exception to include the full traceback (#25)
                logger.exception(
                    "Turn task error for session %s", session_id, exc_info=exc
                )

    async def _maybe_auto_continue(model: str) -> None:
        """Issue 23 (Round 6): auto-continue loop wrapped around a CLI turn.

        Inspects the just-completed assistant message after each clean
        CLI turn (``_saw_result=True``) and re-dispatches with a
        continuation prompt if the structured DONE marker is absent
        OR the message contains unmet future-tense intent. Capped by
        ``MAX_AUTO_CONTINUE`` (default 5; env override
        ``TCG_AGENT_MAX_AUTO_CONTINUE``). Honors ``session.is_cancelled``
        between iterations -- a user interrupt mid-loop terminates
        immediately.

        Mutex / contract:
        - Not entered if ``_saw_result=False`` (silent EOF; the
          ``process_exit`` path already covers that case).
        - Not entered if ``is_cancelled=True`` (user interrupt).
        - Each iteration emits exactly one ``auto_continue`` event
          BEFORE the re-dispatched ``_execute_single_turn``.
        - At cap, emits exactly one ``auto_continue_capped`` event AND
          appends an in-band assistant message to ``conversation_history``
          (persisted via the existing ``_on_persist`` callback).
        """
        while True:
            # Round-6 G-AUTO-INTERRUPT: re-check between iterations so
            # an interrupt arriving in the post-turn micro-window does
            # not trigger one extra spurious continuation.
            if session.is_cancelled:
                return
            # Silent EOF -- the ``process_exit`` path already surfaced
            # the abnormal termination; do NOT auto-continue.
            if not getattr(session, "_saw_result", False):
                return
            # Cap check BEFORE incrementing so the cap event fires once
            # at iter==MAX, not iter==MAX+1.
            if session._continue_iters >= MAX_AUTO_CONTINUE:
                try:
                    await websocket.send_json(
                        {
                            "type": "auto_continue_capped",
                            "session_id": session_id,
                            "iter": session._continue_iters,
                            "max": MAX_AUTO_CONTINUE,
                            "reason": "cap_reached",
                            "timestamp": time.time(),
                        }
                    )
                except Exception:
                    pass
                # R-3 fix: stream the cap message as a synthetic token + message_complete
                # so it renders in the live transcript immediately (not only on reload).
                # The token event starts a new streaming bubble; message_complete seals it.
                _cap_text = _build_auto_continue_cap_message(MAX_AUTO_CONTINUE)
                try:
                    await websocket.send_json(
                        {
                            "type": "token",
                            "session_id": session_id,
                            "content": _cap_text,
                            "timestamp": time.time(),
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "message_complete",
                            "session_id": session_id,
                            "timestamp": time.time(),
                        }
                    )
                except Exception:
                    pass
                # In-band assistant message, persisted to disk via the
                # existing ``_on_persist`` callback. Survives reload.
                # N2 fix: use dynamic message with actual MAX_AUTO_CONTINUE.
                try:
                    await session.append_assistant_message(_cap_text)
                except Exception:
                    logger.warning(
                        "Failed to append cap message for session %s",
                        session_id,
                    )
                return

            should_continue, reason, phrase = _evaluate_auto_continue(session)
            if not should_continue:
                return

            session._continue_iters += 1
            try:
                await websocket.send_json(
                    {
                        "type": "auto_continue",
                        "session_id": session_id,
                        "iter": session._continue_iters,
                        "max": MAX_AUTO_CONTINUE,
                        "reason": reason,
                        "timestamp": time.time(),
                    }
                )
            except Exception:
                pass

            continuation = _build_continuation_message(reason, phrase)
            rid = uuid.uuid4().hex[:8]
            logger.info(
                "[%s] auto-continue iter=%d reason=%s for session %s",
                rid,
                session._continue_iters,
                reason,
                session_id,
            )
            await _execute_single_turn(
                session,
                continuation,
                model,
                websocket,
                workspace,
                session_id,
                rid,
            )
            # Loop re-checks is_cancelled / _saw_result / cap on next iter.

    async def _run_turn_wrapper(content: str, model: str) -> None:
        """Run a single turn, then drain the queue."""
        request_id = uuid.uuid4().hex[:8]
        logger.info("[%s] Starting turn for session %s", request_id, session_id)
        await _execute_single_turn(
            session, content, model, websocket, workspace, session_id, request_id
        )

        # Issue 23 (Round 6): auto-continue loop after the user's
        # initial turn completes cleanly. Re-dispatches up to
        # ``MAX_AUTO_CONTINUE`` times if the agent forgot the DONE
        # marker or announced unmet future work.
        await _maybe_auto_continue(model)

        # Process queued messages automatically
        while queued_messages and not session.is_cancelled:
            next_msg = queued_messages.popleft()
            rid = uuid.uuid4().hex[:8]
            logger.info(
                "[%s] Dequeued message for session %s (remaining=%d)",
                rid,
                session_id,
                len(queued_messages),
            )
            # Issue 23: queued user messages start a fresh auto-continue
            # loop -- counter reset here so the queued message gets its
            # own MAX_AUTO_CONTINUE budget.
            session._continue_iters = 0
            await _execute_single_turn(
                session,
                next_msg["content"],
                next_msg["model"],
                websocket,
                workspace,
                session_id,
                rid,
            )
            await _maybe_auto_continue(next_msg["model"])

        # All turns complete -- emit idle status so the frontend
        # can clear its "processing" badge (#20)
        try:
            await websocket.send_json({"type": "status", "status": "idle"})
        except Exception:
            pass

    async def _cancel_turn() -> None:
        """Cancel the running turn task and clear the queue."""
        nonlocal turn_task
        if turn_task and not turn_task.done():
            await session.cancel()
            turn_task.cancel()
            try:
                await turn_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        turn_task = None
        session.is_cancelled = False

    async def _start_turn(content: str, model: str) -> None:
        """Launch a new turn as a background asyncio task."""
        nonlocal turn_task
        turn_task = asyncio.create_task(_run_turn_wrapper(content, model))
        turn_task.add_done_callback(_on_turn_done)

    # ------------------------------------------------------------------
    # Main receive loop
    # ------------------------------------------------------------------

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "stop":
                if turn_task and not turn_task.done():
                    queued_messages.clear()
                    await _cancel_turn()
                    try:
                        await websocket.send_json({"type": "stopped"})
                    except Exception:
                        break
                continue

            if msg_type == "interrupt":
                content = data.get("content", "")
                if not content.strip():
                    await websocket.send_json(
                        {"type": "error", "message": "Empty message"}
                    )
                    continue
                requested_model = data.get("model", "claude-sonnet-4-6")
                if requested_model not in ALLOWED_MODELS:
                    requested_model = "claude-sonnet-4-6"

                queued_messages.clear()
                await _cancel_turn()
                # Issue 23 (Round 6): user interrupt resets the
                # auto-continue counter so the new user-driven turn
                # starts a clean loop.
                session._continue_iters = 0
                try:
                    await websocket.send_json({"type": "interrupted"})
                except Exception:
                    break
                await _start_turn(content, requested_model)
                continue

            if msg_type == "message":
                content = data.get("content", "")
                if not content.strip():
                    await websocket.send_json(
                        {"type": "error", "message": "Empty message"}
                    )
                    continue
                requested_model = data.get("model", "claude-sonnet-4-6")
                if requested_model not in ALLOWED_MODELS:
                    requested_model = "claude-sonnet-4-6"

                # Queue if a turn is already running
                if turn_task and not turn_task.done():
                    # Enforce queue size limit (#3)
                    if len(queued_messages) >= MAX_QUEUE_SIZE:
                        try:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": (
                                        "Queue full — maximum "
                                        f"{MAX_QUEUE_SIZE} pending messages"
                                    ),
                                }
                            )
                        except Exception:
                            break
                        continue

                    queued_messages.append(
                        {"content": content, "model": requested_model}
                    )
                    logger.info(
                        "Message queued for session %s (queue_size=%d)",
                        session_id,
                        len(queued_messages),
                    )
                    try:
                        await websocket.send_json({"type": "queued"})
                    except Exception:
                        break
                    continue

                # Issue 23 (Round 6): user-driven message resets the
                # auto-continue counter. (The queued-drain path inside
                # ``_run_turn_wrapper`` handles its own reset for
                # already-running-turn queue cases.)
                session._continue_iters = 0
                await _start_turn(content, requested_model)
                continue

            await websocket.send_json(
                {"type": "error", "message": f"Unknown message type: {msg_type}"}
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for session %s", session_id)
    except asyncio.CancelledError:
        logger.info("WebSocket cancelled for session %s", session_id)
    except Exception:
        logger.exception("WebSocket error for session %s", session_id)
    finally:
        # Remove from active connections registry. Only remove if WE
        # are the registered connection -- a superseding handler may
        # have already overwritten the slot.
        if _active_connections.get(session_id) is websocket:
            _active_connections.pop(session_id, None)
        queued_messages.clear()

        # Issue 9 (Option B): if a turn is still running at the
        # moment we tear down, the upcoming ``_cancel_turn`` will
        # SIGTERM the CLI subprocess and the in-flight turn's events
        # are lost (the WS is already dead). Record a one-shot
        # ``turn_aborted`` notification so the next connect for the
        # same session_id can surface it AFTER history. Done BEFORE
        # _cancel_turn so we capture the partial-content hint while
        # the session state is still intact. No new kill timers are
        # introduced (G3): we only annotate the existing cancel.
        #
        # F1 (R-be-correctness): mutex with ``turn_complete``. If the
        # turn already saw a clean ``result`` event (``_saw_result``
        # is True), the run_turn body is in the post-parse tail --
        # the turn ended cleanly and turn_complete has either been
        # emitted or is one await away. Buffering turn_aborted in
        # that ~1ms window would deliver BOTH events to the FE on
        # reconnect, violating the contract's "one of T or A per
        # turn" guarantee. Skip the enqueue when _saw_result is True.
        if (
            turn_task is not None
            and not turn_task.done()
            and not getattr(session, "_saw_result", False)
        ):
            had_partial = bool(
                getattr(session, "_partial_text_parts", None)
            ) or any(
                isinstance(m, dict) and m.get("interrupted") is True
                for m in session.conversation_history
            )
            _pending_abort_notifications[session_id] = {
                "reason": "ws_disconnect",
                "had_partial_content": had_partial,
            }

        await _cancel_turn()
        # Persist conversation on disconnect
        try:
            workspace.save_conversation(session_id, session.conversation_history)
        except Exception:
            logger.exception("Failed to save conversation for session %s", session_id)
