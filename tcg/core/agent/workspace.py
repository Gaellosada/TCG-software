"""Agent workspace manager -- persistent session storage on disk.

Each session gets a UUID-named directory under ``WORKSPACES_ROOT`` containing:
- ``conversation.json``  — full message history for the Anthropic API
- ``ASSUMPTIONS.json``   — running list of assumptions the agent surfaces
- ``meta.json``          — session name, creation timestamp, etc.
- ``.mcp.json``          — MongoDB MCP server config for the Claude CLI
- ``.claude/settings.json`` — permissions config for the Claude CLI
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


def _workspaces_root() -> Path:
    """Resolve the root directory for all agent workspaces.

    Reads ``AGENT_WORKSPACES_ROOT`` from the environment; falls back to
    ``<project_root>/agent_workspaces``.
    """
    env_root = os.environ.get("AGENT_WORKSPACES_ROOT")
    if env_root:
        return Path(env_root)
    # project root = three levels up from this file (tcg/core/agent/workspace.py)
    return Path(__file__).resolve().parents[3] / "agent_workspaces"


_META_FILE = "meta.json"
_CONVERSATION_FILE = "conversation.json"
_ASSUMPTIONS_FILE = "ASSUMPTIONS.json"

_ASSUMPTIONS_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "assumptions": [],
}


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


def _build_claude_settings() -> dict[str, Any]:
    """Build .claude/settings.json with permissions for the backtester agent."""
    return {
        "permissions": {
            "allow": [
                "Bash(python *)",
                "Bash(python3 *)",
                "Bash(ls *)",
                "Bash(mkdir *)",
                "Bash(mkdir -p *)",
                "Bash(cp *)",
                "Bash(find *)",
                "Bash(grep *)",
                "Bash(cat *)",
                "Bash(head *)",
                "Bash(tail *)",
                "Bash(wc *)",
            ],
            "deny": [
                "Bash(rm *)",
                "Bash(sudo *)",
                "Bash(curl *)",
                "Bash(wget *)",
                "Bash(ssh *)",
                "Bash(pip *)",
                "Bash(npm *)",
                "Bash(git push *)",
                "Bash(git commit *)",
                "Bash(git reset *)",
            ],
        },
        "enabledMcpjsonServers": ["mongodb"],
    }


_CLAUDE_MD_CONTENT = """\
# Backtester Agent

You are a quantitative analyst that turns strategy descriptions into backtested results.
Given a trading idea, you produce a workspace with scripts, a compiled notebook, and metrics.
Communicate results, not process.

## Tools

You have access to the Claude CLI built-in tools (Bash, Read, Write, Edit, Glob, Grep) \
plus the MongoDB MCP server configured in `.mcp.json` (read-only access to market data).

## First Turn Protocol

1. Read `PIPELINE_GUIDE.md` — contains the workflow, decision tree, and API reference.
2. Check if `ASSUMPTIONS.json` has existing assumptions (resume context from prior turns).
3. Follow the pipeline decision tree in the guide.

## Library: tcg.backtester.lib

ALL scripts MUST import from this library. Never reimplement what it provides.

```python
from tcg.backtester.lib import data_load, signals, engine, metrics, plotting, diagnostics
from tcg.backtester.lib.engine import BacktestSpec, ExecutionConfig, SizingConfig
from tcg.backtester.lib.validate import bar_integrity
```

Key classes:
- `BacktestSpec` — top-level specification: bars, signal, sizing, execution config
- `ExecutionConfig` — slippage, commission, fill assumptions
- `SizingConfig` — position sizing method and parameters

Key functions:
- `data_load.fetch_index_bars(symbol, start, end)` — load price bars from MongoDB
- `data_load.fetch_futures_bars(symbol, start, end)` — load futures price bars
- `signals.sma(series, period)` — simple moving average
- `signals.ema(series, period)` — exponential moving average
- `engine.run_backtest(spec)` — execute a backtest from a BacktestSpec
- `metrics.compute_metrics(result)` — compute performance metrics from backtest result

Key pattern (sync, no asyncio needed):
```python
bars = data_load.fetch_index_bars("IND_SP_500", start=20200101, end=20241231)
fast = signals.sma(bars.close, 50)
slow = signals.sma(bars.close, 200)
sig = (fast > slow).astype(float)
spec = BacktestSpec(
    bars=bars,
    signal=sig,
    sizing=SizingConfig(method="fixed_fraction", fraction=1.0),
)
result = engine.run_backtest(spec)
m = metrics.compute_metrics(result)
print(m.to_dict())
```

## Critical Rules

- `BacktestSpec` takes `bars` (PriceSeries), NOT separate dates/close arrays.
- `fetch_*` functions are sync — no `asyncio.run` needed. They manage their own DB connection.
- Signal arrays must be same length as `bars.dates`. NaN warm-up is normal.
- Engine fires entries on signal transitions (0->nonzero or sign change), not on every nonzero bar.
- Use `Path.cwd()` in scripts, never `Path(__file__)`.
- NEVER fabricate data or results. If data is missing, stop and report.
- On ANY failure: write to `PROBLEMS.md`, explain plainly, wait for the user.

## Communication Style

Speak as a quant to a portfolio manager. Report what you found, what you built, what the \
numbers say. When you need input, ask one clear question about the strategy itself.

## Workspace Files

| Path | Purpose |
|------|---------|
| `scripts/` | Generated Python scripts (numbered: `01_data.py`, `02_signal.py`, etc.) |
| `results/` | Outputs — notebook.ipynb, metrics JSON, plots |
| `snippets/` | Reusable code templates — read before writing new code |
| `ASSUMPTIONS.json` | Tracked assumptions — update when inferring strategy parameters |
| `PIPELINE_GUIDE.md` | Workflow instructions and decision tree |
| `PROBLEMS.md` | Failure log — write here when something goes wrong |
"""


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
        self._write_json(session_dir / _CONVERSATION_FILE, [])
        self._write_json(session_dir / _ASSUMPTIONS_FILE, _ASSUMPTIONS_TEMPLATE)

        # Write pipeline guide for the agent to read on first turn
        (session_dir / "PIPELINE_GUIDE.md").write_text(
            PIPELINE_GUIDE_MD, encoding="utf-8"
        )

        # Copy snippet templates into the workspace
        snippets_src = (
            Path(__file__).resolve().parents[3] / "tcg" / "backtester" / "snippets"
        )
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

        # .claude/settings.json — permissions config
        claude_dir = session_dir / ".claude"
        claude_dir.mkdir(exist_ok=True)
        self._write_json(claude_dir / "settings.json", _build_claude_settings())

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
        """Load the saved conversation history for a session."""
        conv_path = self.root / session_id / _CONVERSATION_FILE
        if not conv_path.exists():
            return []
        data = self._read_json(conv_path)
        return data if isinstance(data, list) else []

    def save_conversation(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Persist the full conversation history to disk."""
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(session_dir / _CONVERSATION_FILE, messages)

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
