"""Agent workspace manager -- persistent session storage on disk.

Each session gets a UUID-named directory under ``WORKSPACES_ROOT`` containing:
- ``conversation.json``  — full message history for the Anthropic API
- ``ASSUMPTIONS.json``   — running list of assumptions the agent surfaces
- ``meta.json``          — session name, creation timestamp, etc.
- ``.mcp.json``          — MongoDB MCP server config for the Claude CLI
- ``CLAUDE.md``          — agent system instructions for the Claude CLI
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tcg.core.agent.pipeline_guide import PIPELINE_GUIDE_MD

# tcg/core/agent/workspace.py -> parents[3] = TCG-software/
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_CLAUDE_MD_CONTENT = (Path(__file__).parent / "claude_md.md").read_text(
    encoding="utf-8"
)


def _workspaces_root() -> Path:
    """Resolve the root directory for all agent workspaces.

    Reads ``AGENT_WORKSPACES_ROOT`` from the environment; falls back to
    ``<project_root>/agent_workspaces``.
    """
    env_root = os.environ.get("AGENT_WORKSPACES_ROOT")
    if env_root:
        return Path(env_root)
    return _PROJECT_ROOT / "agent_workspaces"


_META_FILE = "meta.json"
_CONVERSATION_FILE = "conversation.json"
_ASSUMPTIONS_FILE = "ASSUMPTIONS.json"

_ASSUMPTIONS_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "assumptions": [],
}

_CONVERSATION_SCHEMA_VERSION = 1


def _get_mongo_uri() -> str:
    """Resolve MongoDB connection string from .env file or environment.

    Uses the same priority as ``tcg.core.config.load_config``:
    real env vars > .env file > default.
    """
    from tcg.core.config import _load_env

    env = _load_env()
    return os.getenv("MONGO_URI") or env.get("MONGO_URI") or "mongodb://localhost:27017"


def _build_mcp_json(mongo_uri: str) -> dict[str, Any]:
    """Build .mcp.json content for the Claude CLI MongoDB MCP server."""
    return {
        "mcpServers": {
            "mongodb": {
                "command": "npx",
                "args": ["-y", "mongodb-mcp-server", "--readOnly"],
                "env": {
                    "MDB_MCP_CONNECTION_STRING": mongo_uri,
                },
            }
        }
    }


class AgentWorkspace:
    """Manages on-disk session directories for the agent feature."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _workspaces_root()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, name: str | None = None) -> dict[str, Any]:
        """Create a new session directory with scaffolding files.

        Returns a dict with ``id``, ``name``, ``created_at``, ``workspace_path``.
        """
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "id": session_id,
            "name": name or f"Session {now[:10]}",
            "created_at": now,
        }
        self._write_json(session_dir / _META_FILE, meta)
        self._write_json(
            session_dir / _CONVERSATION_FILE,
            {"schema_version": _CONVERSATION_SCHEMA_VERSION, "messages": []},
        )
        self._write_json(session_dir / _ASSUMPTIONS_FILE, _ASSUMPTIONS_TEMPLATE)

        # Write pipeline guide for the agent to read on first turn
        (session_dir / "PIPELINE_GUIDE.md").write_text(
            PIPELINE_GUIDE_MD, encoding="utf-8"
        )

        # Copy backtester CLAUDE.md as BACKTESTER_GUIDE.md for full API reference
        backtester_guide_src = _PROJECT_ROOT / "tcg" / "backtester" / "CLAUDE.md"
        if backtester_guide_src.exists():
            (session_dir / "BACKTESTER_GUIDE.md").write_text(
                backtester_guide_src.read_text(encoding="utf-8"), encoding="utf-8"
            )

        # Scaffold SCHEMA.md (per-collection MongoDB doc shapes) into the
        # session workspace. The library docs reference this file; copying
        # rather than symlinking guarantees the agent can `Read` it without
        # tripping over symlink-target permission rules and keeps the file
        # available even if the source is later moved.
        schema_src = (
            _PROJECT_ROOT / "tcg" / "backtester" / "lib" / "data" / "SCHEMA.md"
        )
        if schema_src.exists():
            (session_dir / "SCHEMA.md").write_text(
                schema_src.read_text(encoding="utf-8"), encoding="utf-8"
            )

        # Copy snippet templates into the workspace
        snippets_src = _PROJECT_ROOT / "tcg" / "backtester" / "snippets"
        if snippets_src.exists():
            snippets_dst = session_dir / "snippets"
            snippets_dst.mkdir(exist_ok=True)
            for snippet in snippets_src.glob("*.py"):
                (snippets_dst / snippet.name).write_text(
                    snippet.read_text(encoding="utf-8"), encoding="utf-8"
                )

        # --- CLI-compatible configuration files ---

        # .mcp.json — MongoDB MCP server config
        mongo_uri = _get_mongo_uri()
        self._write_json(session_dir / ".mcp.json", _build_mcp_json(mongo_uri))

        # Issue 17: write a workspace-local ``.env`` so that scripts the
        # agent runs from the session workspace pick up MONGO_URI via
        # ``tcg.backtester.lib.mongo.resolve_env()``'s file-walk path,
        # not just via inherited ``os.environ``. The CWD-walk path
        # otherwise finds no ``.env`` in the session workspace and
        # falls through to a default placeholder when env inheritance
        # is unreliable. Reuses the SAME ``mongo_uri`` resolved above
        # for ``.mcp.json`` -- single source of truth at session-create
        # time. The MCP server's runtime override
        # (``MDB_MCP_CONNECTION_STRING`` set in ``_build_subprocess_env``)
        # remains the canonical source for the MCP child; this ``.env``
        # is for agent-spawned Python scripts only.
        #
        # F3 (R-be-correctness): single-quote the value. python-dotenv
        # treats unquoted values as spanning to the first ``#`` (start
        # of inline comment); a MongoDB URI with ``#`` (legal in a
        # password or fragment) would silently truncate on read. Single
        # quotes also disable backslash-escape and dollar-expansion in
        # python-dotenv, so the URI round-trips byte-for-byte. URIs
        # containing a literal ``'`` are rejected at write-time --
        # RFC 3986 §3 forbids unencoded single quotes in URI authority,
        # so this only blocks pathological input.
        if "'" in mongo_uri:
            raise ValueError(
                f"MONGO_URI contains a single quote, which the .env writer"
                f" cannot escape safely: {mongo_uri!r}"
            )
        (session_dir / ".env").write_text(
            f"MONGO_URI='{mongo_uri}'\n", encoding="utf-8"
        )

        # CLAUDE.md — agent system instructions
        (session_dir / "CLAUDE.md").write_text(_CLAUDE_MD_CONTENT, encoding="utf-8")

        return {
            "id": session_id,
            "name": meta["name"],
            "created_at": now,
            "workspace_path": str(session_dir),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return metadata for every session, sorted newest-first."""
        sessions: list[dict[str, Any]] = []
        if not self.root.exists():
            return sessions
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            meta_path = entry / _META_FILE
            if not meta_path.exists():
                continue
            meta = self._read_json(meta_path)
            if meta is not None:
                meta["workspace_path"] = str(entry)
                sessions.append(meta)
        sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return sessions

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Load metadata for a single session, or ``None`` if not found."""
        session_dir = self.root / session_id
        meta_path = session_dir / _META_FILE
        if not meta_path.exists():
            return None
        meta = self._read_json(meta_path)
        if meta is not None:
            meta["workspace_path"] = str(session_dir)
        return meta

    def rename_session(self, session_id: str, new_name: str) -> dict[str, Any] | None:
        """Rename a session. Returns updated metadata or None if not found."""
        session_dir = self.root / session_id
        meta_path = session_dir / _META_FILE
        if not meta_path.exists():
            return None
        meta = self._read_json(meta_path)
        if meta is None:
            return None
        meta["name"] = new_name
        self._write_json(meta_path, meta)
        meta["workspace_path"] = str(session_dir)
        return meta

    def delete_session(self, session_id: str) -> bool:
        """Remove a session directory entirely. Returns True if it existed."""
        session_dir = self.root / session_id
        if not session_dir.exists():
            return False
        shutil.rmtree(session_dir)
        return True

    # ------------------------------------------------------------------
    # Conversation persistence
    # ------------------------------------------------------------------

    def load_conversation(self, session_id: str) -> list[dict[str, Any]]:
        """Load the saved conversation history for a session.

        Handles both the legacy format (raw JSON array) and the current
        envelope format ({"schema_version": 1, "messages": [...]}).
        """
        conv_path = self.root / session_id / _CONVERSATION_FILE
        if not conv_path.exists():
            return []
        data = self._read_json(conv_path)
        if isinstance(data, list):
            # Legacy format: raw array written by older versions
            return data
        if isinstance(data, dict) and "messages" in data:
            messages = data["messages"]
            return messages if isinstance(messages, list) else []
        return []

    def save_conversation(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Persist the full conversation history to disk."""
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        envelope = {
            "schema_version": _CONVERSATION_SCHEMA_VERSION,
            "messages": messages,
        }
        self._write_json(session_dir / _CONVERSATION_FILE, envelope)

    # ------------------------------------------------------------------
    # Assumptions file
    # ------------------------------------------------------------------

    def load_assumptions(self, session_id: str) -> dict[str, Any]:
        """Load the ASSUMPTIONS.json for a session."""
        path = self.root / session_id / _ASSUMPTIONS_FILE
        if not path.exists():
            return dict(_ASSUMPTIONS_TEMPLATE)
        data = self._read_json(path)
        return data if isinstance(data, dict) else dict(_ASSUMPTIONS_TEMPLATE)

    def save_assumptions(self, session_id: str, assumptions: dict[str, Any]) -> None:
        """Persist assumptions to disk."""
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(session_dir / _ASSUMPTIONS_FILE, assumptions)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
