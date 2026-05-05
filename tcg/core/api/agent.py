"""Agent router -- REST endpoints for session management + WebSocket for chat.

The WebSocket endpoint is mounted at ``/ws/agent/{session_id}`` to avoid
prefix-routing issues with FastAPI's ``APIRouter``.  The REST endpoints
live under ``/api/agent``.
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

from tcg.core.agent.prompt import build_system_prompt
from tcg.core.agent.session import AgentSession
from tcg.core.agent.tools import create_tools
from tcg.core.agent.workspace import AgentWorkspace
from tcg.types.config import AgentConfig

logger = logging.getLogger(__name__)

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


def _get_agent_config(request: Request) -> AgentConfig | None:
    return getattr(request.app.state, "agent_config", None)


def _require_agent_config(request: Request) -> AgentConfig:
    """Return agent config or raise 503."""
    config = _get_agent_config(request)
    if config is None:
        raise _AgentUnavailable()
    return config


class _AgentUnavailable(Exception):
    """Raised when the agent feature is not configured."""


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
    """Check whether the agent feature is available."""
    config = _get_agent_config(request)
    return {
        "available": config is not None,
        "model": config.model if config else None,
    }


# ------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------

# This is a standalone function that will be mounted on the app directly
# rather than via the router, to avoid FastAPI WebSocket prefix issues.


async def agent_websocket(websocket: WebSocket, session_id: str) -> None:
    """WebSocket handler for agent chat.

    Protocol (JSON messages):
    - Client sends:  ``{"type": "message", "content": "..."}``
    - Server sends:  ``{"type": "token"|"tool_call"|"tool_result"|"message_complete"|"error", ...}``
    """
    # Check agent availability before accepting
    agent_config: AgentConfig | None = getattr(
        websocket.app.state, "agent_config", None
    )
    if agent_config is None:
        await websocket.close(code=1008, reason="Agent feature not configured")
        return

    workspace: AgentWorkspace = websocket.app.state.agent_workspace

    # Validate session exists
    session_meta = workspace.get_session(session_id)
    if session_meta is None:
        await websocket.close(code=1008, reason=f"Session {session_id} not found")
        return

    await websocket.accept()

    # Load any prior conversation
    prior_messages = workspace.load_conversation(session_id)

    # Read mongo config from app state
    mongo_uri: str = getattr(
        websocket.app.state, "mongo_uri", "mongodb://localhost:27017"
    )
    mongo_db_name: str = getattr(websocket.app.state, "mongo_db_name", "tcg-instrument")

    # Build tools and system prompt
    workspace_path = Path(session_meta["workspace_path"])
    tool_definitions, tool_executors = create_tools(
        workspace_path=workspace_path,
        mongo_uri=mongo_uri,
        mongo_db_name=mongo_db_name,
        session_id=session_id,
        workspace_manager=workspace,
    )
    system_prompt = build_system_prompt()

    session = AgentSession(
        session_id=session_id,
        workspace_path=workspace_path,
        system_prompt=system_prompt,
        api_key=agent_config.api_key,
        mongo_uri=mongo_uri,
        mongo_db_name=mongo_db_name,
        model=agent_config.model,
        max_tokens=agent_config.max_tokens,
        thinking_budget=agent_config.thinking_budget,
        tools=tool_definitions,
        tool_executors=tool_executors,
    )
    session.conversation_history = prior_messages

    # Send conversation history to client on connect
    if prior_messages:
        try:
            await websocket.send_json({"type": "history", "messages": prior_messages})
        except Exception:
            logger.warning("Failed to send history for session %s", session_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type != "message":
                await websocket.send_json(
                    {"type": "error", "message": f"Unknown message type: {msg_type}"}
                )
                continue

            content = data.get("content", "")
            if not content.strip():
                await websocket.send_json({"type": "error", "message": "Empty message"})
                continue

            # Allow per-message model override from the frontend
            requested_model = data.get("model")
            if requested_model and requested_model in ALLOWED_MODELS:
                session.model = requested_model

            async def on_event(event: dict[str, Any]) -> None:
                try:
                    await websocket.send_json(event)
                except Exception:
                    logger.warning(
                        "Failed to send event to client, session %s", session_id
                    )

            await session.run_turn(content, on_event)

            # Persist conversation after each successful turn so reconnects
            # can resume from the latest state (not just on disconnect).
            try:
                workspace.save_conversation(session_id, session.conversation_history)
            except Exception:
                logger.warning(
                    "Failed to save conversation mid-session for %s", session_id
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for session %s", session_id)
    except asyncio.CancelledError:
        logger.info("WebSocket cancelled for session %s", session_id)
    except Exception:
        logger.exception("WebSocket error for session %s", session_id)
    finally:
        # Persist conversation on disconnect
        try:
            workspace.save_conversation(session_id, session.conversation_history)
        except Exception:
            logger.exception("Failed to save conversation for session %s", session_id)
