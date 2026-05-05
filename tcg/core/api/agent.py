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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tcg.core.agent.session import CLISession, cli_available
from tcg.core.agent.workspace import AgentWorkspace

logger = logging.getLogger(__name__)

# The CLI accepts full model names directly
ALLOWED_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}

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
    except (json.JSONDecodeError, OSError) as exc:
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
    websocket: WebSocket, session: CLISession, interval: int = 30
) -> None:
    """Send periodic status events to keep the WebSocket alive.

    During long CLI tool executions, no data flows on the wire.  Proxies
    (Vite http-proxy defaults to ~120 s) and browsers may drop the idle
    connection.  This heartbeat keeps traffic flowing.
    """
    while not session._cancelled:
        await asyncio.sleep(interval)
        try:
            await websocket.send_json({"type": "status", "status": "processing"})
        except Exception:
            session._cancelled = True
            break


async def agent_websocket(websocket: WebSocket, session_id: str) -> None:
    """WebSocket handler for agent chat.

    Protocol (JSON messages):
    - Client sends:  ``{"type": "message"|"stop"|"interrupt", ...}``
    - Server sends:  ``{"type": "token"|"tool_call"|"tool_result"|"message_complete"|"error"|"history"|"stopped"|"queued"|"interrupted", ...}``

    Flow control:
    - ``stop``      — cancel the running turn and clear the queue.
    - ``interrupt``  — cancel the running turn, then start a new one.
    - ``message`` while busy — queue it; auto-processed after the current turn.
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

    await websocket.accept()

    workspace_path = Path(session_meta["workspace_path"])

    # Event callback that sends to WebSocket
    async def on_event(event: dict[str, Any]) -> None:
        try:
            await websocket.send_json(event)
        except Exception:
            # WebSocket is dead — cancel the session so the parse loop stops
            logger.warning("WebSocket dead for session %s, cancelling", session_id)
            session._cancelled = True

    # Create the CLI session
    session = CLISession(
        session_id=session_id,
        workspace_path=workspace_path,
        on_event=on_event,
    )

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

    # ------------------------------------------------------------------
    # Task-based turn management
    # ------------------------------------------------------------------

    turn_task: asyncio.Task[None] | None = None
    queued_messages: list[dict[str, Any]] = []

    def _on_turn_done(task: asyncio.Task[None]) -> None:
        """Log uncaught exceptions from turn tasks (avoids 'never retrieved')."""
        if not task.cancelled():
            exc = task.exception()
            if exc:
                logger.error("Turn task error for session %s: %s", session_id, exc)

    async def _run_turn_wrapper(content: str, model: str) -> None:
        """Run a single turn with keepalive, persist, then drain the queue."""
        session._cancelled = False
        heartbeat = asyncio.create_task(_keepalive(websocket, session, interval=30))
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
            logger.warning("Failed to save conversation for %s", session_id)

        # Process queued messages automatically
        while queued_messages and not session._cancelled:
            next_msg = queued_messages.pop(0)
            session._cancelled = False
            heartbeat = asyncio.create_task(_keepalive(websocket, session, interval=30))
            try:
                await session.run_turn(next_msg["content"], model=next_msg["model"])
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            try:
                workspace.save_conversation(session_id, session.conversation_history)
            except Exception:
                logger.warning("Failed to save conversation for %s", session_id)

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
        session._cancelled = False

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
                    queued_messages.append(
                        {"content": content, "model": requested_model}
                    )
                    try:
                        await websocket.send_json({"type": "queued"})
                    except Exception:
                        break
                    continue

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
        queued_messages.clear()
        await _cancel_turn()
        # Persist conversation on disconnect
        try:
            workspace.save_conversation(session_id, session.conversation_history)
        except Exception:
            logger.exception("Failed to save conversation for session %s", session_id)
