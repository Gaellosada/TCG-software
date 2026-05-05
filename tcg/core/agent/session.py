"""Agent session -- wraps the Claude CLI subprocess for agentic conversations.

A single ``CLISession`` instance is created per WebSocket connection and
drives one multi-turn conversation by spawning the ``claude`` CLI in
``--print --output-format stream-json`` mode for each user turn.

The CLI handles tool execution internally (with --dangerously-skip-permissions),
so there is no need for custom tool definitions or executors on the Python side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Model name mapping: frontend sends full model IDs, CLI accepts aliases or full names
_MODEL_MAP: dict[str, str] = {
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
}


def _cli_model_arg(model: str) -> str:
    """Convert a frontend model ID to a CLI --model argument."""
    return _MODEL_MAP.get(model, model)


def cli_available() -> bool:
    """Check whether the ``claude`` binary is on PATH."""
    return shutil.which("claude") is not None


class CLISession:
    """One agent conversation backed by the Claude CLI subprocess.

    Each call to ``run_turn`` spawns a new CLI process using either
    ``--session-id`` (first turn) or ``--resume`` (subsequent turns) to
    maintain conversation continuity.

    Parameters
    ----------
    session_id:
        UUID used as the CLI session identifier (--session-id / --resume).
    workspace_path:
        Path to the session's disk workspace (used as cwd for the subprocess).
    on_event:
        Async callback for emitting WebSocket events to the client.
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: Path,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.on_event = on_event
        self._first_turn = True
        self.conversation_history: list[dict[str, Any]] = []
        self._process: asyncio.subprocess.Process | None = None
        # Track file state for change detection
        self._assumptions_snapshot: str | None = None
        self._notebook_exists: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_turn(self, user_message: str, model: str = "opus") -> None:
        """Execute one user turn by spawning the CLI and parsing stream output.

        Events emitted via ``self.on_event``:
        - ``{"type": "token", "content": "..."}``
        - ``{"type": "tool_call", "name": ..., "input": ..., "id": ...}``
        - ``{"type": "tool_result", "name": ..., "result": ...}``
        - ``{"type": "message_complete", "content": "..."}``
        - ``{"type": "assumptions_update", "assumptions": [...]}``
        - ``{"type": "notebook_ready"}``
        - ``{"type": "error", "message": "..."}``
        """
        # Snapshot file state before turn for change detection
        self._snapshot_file_state()

        try:
            cmd = self._build_command(user_message, model)
            logger.info(
                "CLI turn for session %s (first=%s, model=%s)",
                self.session_id,
                self._first_turn,
                model,
            )
            logger.debug("CLI command: %s", " ".join(cmd))

            # Spawn subprocess
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(self.workspace_path),
            )

            # Parse stream output
            assistant_content = await self._parse_stream()

            # Wait for process to finish
            await self._process.wait()

            if self._process.returncode != 0:
                stderr_bytes = await self._process.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

                # If --resume failed (session not found), retry with --session-id
                if (
                    not self._first_turn
                    and not assistant_content
                    and (
                        "session" in stderr_text.lower()
                        or "not found" in stderr_text.lower()
                        or "resume" in stderr_text.lower()
                    )
                ):
                    logger.warning(
                        "CLI --resume failed for session %s, retrying with --session-id",
                        self.session_id,
                    )
                    self._first_turn = True
                    assistant_content = await self._retry_as_new_session(
                        user_message, model
                    )
                    if not assistant_content:
                        return
                elif not assistant_content:
                    error_msg = f"CLI process failed (exit {self._process.returncode})"
                    if stderr_text:
                        error_msg += f": {stderr_text[:500]}"
                    await self.on_event({"type": "error", "message": error_msg})
                    return
                else:
                    # Process exited non-zero but we got content — log warning only
                    logger.warning(
                        "CLI exited %d for session %s but content was emitted. stderr: %s",
                        self._process.returncode,
                        self.session_id,
                        stderr_text[:200],
                    )

            # Turn succeeded — record in history
            self.conversation_history.append({"role": "user", "content": user_message})
            if assistant_content:
                self.conversation_history.append(
                    {"role": "assistant", "content": assistant_content}
                )

            # After first successful turn, switch to --resume for subsequent turns
            self._first_turn = False

            # Post-turn file change detection (fallback for writes via Bash/Python)
            await self._check_file_changes()

        except asyncio.CancelledError:
            # Kill subprocess on cancellation
            if self._process and self._process.returncode is None:
                self._process.kill()
            raise
        except Exception as exc:
            logger.exception("CLI turn failed for session %s", self.session_id)
            await self.on_event({"type": "error", "message": str(exc)})
        finally:
            self._process = None

    async def cancel(self) -> None:
        """Kill the running subprocess if any (e.g., on WebSocket disconnect)."""
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                # Give it a moment to clean up
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._process.kill()
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------
    # Retry logic
    # ------------------------------------------------------------------

    async def _retry_as_new_session(
        self, user_message: str, model: str
    ) -> list[dict[str, Any]]:
        """Retry a failed --resume as a fresh --session-id invocation.

        Called when the CLI session store doesn't have the session (e.g.,
        after purge or server migration). Returns assistant_content.
        """
        cmd = self._build_command(user_message, model)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(self.workspace_path),
        )
        assistant_content = await self._parse_stream()
        await self._process.wait()

        if self._process.returncode != 0 and not assistant_content:
            stderr_bytes = await self._process.stderr.read()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            error_msg = f"CLI retry failed (exit {self._process.returncode})"
            if stderr_text:
                error_msg += f": {stderr_text[:500]}"
            await self.on_event({"type": "error", "message": error_msg})

        return assistant_content

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _build_command(self, user_message: str, model: str) -> list[str]:
        """Build the claude CLI command for this turn."""
        cmd = [
            "claude",
            "-p",
            user_message,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--model",
            _cli_model_arg(model),
            "--disable-slash-commands",
        ]

        if self._first_turn:
            cmd.extend(["--session-id", self.session_id])
        else:
            cmd.extend(["--resume", self.session_id])

        return cmd

    # ------------------------------------------------------------------
    # Stream parsing
    # ------------------------------------------------------------------

    async def _parse_stream(self) -> list[dict[str, Any]]:
        """Read stdout line-by-line and emit WebSocket events.

        Returns the assistant content blocks for history tracking.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        assistant_content: list[dict[str, Any]] = []
        full_text_parts: list[str] = []

        # Track active content blocks for tool_use accumulation
        active_blocks: dict[int, dict[str, Any]] = {}

        while True:
            line = await self._process.stdout.readline()
            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                # Non-JSON lines (e.g., debug output) — skip
                logger.debug("Non-JSON CLI output: %s", line_str[:200])
                continue

            await self._handle_event(
                event, assistant_content, full_text_parts, active_blocks
            )

        # Emit message_complete with the accumulated text
        final_text = "".join(full_text_parts)
        await self.on_event({"type": "message_complete", "content": final_text})

        # If no explicit 'assistant' event populated content, build from streamed deltas
        if not assistant_content and final_text:
            assistant_content.append({"type": "text", "text": final_text})

        return assistant_content

    async def _handle_event(
        self,
        event: dict[str, Any],
        assistant_content: list[dict[str, Any]],
        full_text_parts: list[str],
        active_blocks: dict[int, dict[str, Any]],
    ) -> None:
        """Dispatch a single parsed JSON event from the CLI stream."""
        event_type = event.get("type")

        if event_type == "stream_event":
            await self._handle_stream_event(
                event.get("event", {}), full_text_parts, active_blocks
            )

        elif event_type == "assistant":
            # Full assistant message — extract content for history
            message = event.get("message", {})
            content_blocks = message.get("content", [])
            for block in content_blocks:
                block_type = block.get("type")
                if block_type == "text":
                    assistant_content.append(
                        {"type": "text", "text": block.get("text", "")}
                    )
                elif block_type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                            "id": block.get("id", ""),
                        }
                    )
                elif block_type == "tool_result":
                    # The CLI sometimes includes tool_result blocks in the stream
                    tool_name = block.get("name", "tool")
                    result_content = block.get("content", "")
                    # Extract text from content if it's a list
                    if isinstance(result_content, list):
                        result_parts = []
                        for part in result_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                result_parts.append(part.get("text", ""))
                        result_content = "\n".join(result_parts)
                    await self.on_event(
                        {
                            "type": "tool_result",
                            "name": tool_name,
                            "result": (
                                result_content
                                if isinstance(result_content, str)
                                else json.dumps(result_content, default=str)
                            ),
                        }
                    )

        elif event_type == "result":
            # Final result event
            subtype = event.get("subtype", "")
            is_error = event.get("is_error", False)

            if subtype == "error_max_budget_usd":
                await self.on_event({"type": "error", "message": "Budget exceeded"})
            elif is_error:
                error_text = event.get("result", "Unknown CLI error")
                await self.on_event({"type": "error", "message": error_text})
            else:
                # Success result — the text was already streamed via stream_events.
                # Extract final text if present (for history purposes)
                result_text = event.get("result", "")
                if result_text and not full_text_parts:
                    # Only use result text if we didn't get streaming deltas
                    full_text_parts.append(result_text)
                    assistant_content.append({"type": "text", "text": result_text})

        elif event_type == "system":
            # System events (e.g., tool execution status) — we can extract
            # tool results from these if available
            message = event.get("message", "")
            subtype = event.get("subtype", "")
            if subtype == "tool_result" or "tool_result" in str(event):
                # Try to extract tool result info
                logger.debug("System tool_result event: %s", str(event)[:300])

    # ------------------------------------------------------------------
    # File change detection (ASSUMPTIONS.json, notebook)
    # ------------------------------------------------------------------

    def _snapshot_file_state(self) -> None:
        """Capture file state before a turn for post-turn diff."""
        assumptions_path = self.workspace_path / "ASSUMPTIONS.json"
        if assumptions_path.exists():
            try:
                self._assumptions_snapshot = assumptions_path.read_text(
                    encoding="utf-8"
                )
            except OSError:
                self._assumptions_snapshot = None
        else:
            self._assumptions_snapshot = None

        notebook_path = self.workspace_path / "results" / "notebook.ipynb"
        self._notebook_exists = notebook_path.exists()

    async def _check_file_changes(self) -> None:
        """Post-turn check: emit events if ASSUMPTIONS.json or notebook changed."""
        # Check ASSUMPTIONS.json
        assumptions_path = self.workspace_path / "ASSUMPTIONS.json"
        if assumptions_path.exists():
            try:
                current = assumptions_path.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current is not None and current != self._assumptions_snapshot:
                try:
                    data = json.loads(current)
                    assumptions_list = data.get("assumptions", [])
                    await self.on_event(
                        {"type": "assumptions_update", "assumptions": assumptions_list}
                    )
                except json.JSONDecodeError:
                    pass

        # Check notebook
        notebook_path = self.workspace_path / "results" / "notebook.ipynb"
        if notebook_path.exists() and not self._notebook_exists:
            # Notebook was created during this turn
            await self.on_event({"type": "notebook_ready"})

    async def _handle_stream_event(
        self,
        inner: dict[str, Any],
        full_text_parts: list[str],
        active_blocks: dict[int, dict[str, Any]],
    ) -> None:
        """Handle a nested stream_event from the CLI output."""
        inner_type = inner.get("type")

        if inner_type == "content_block_start":
            index = inner.get("index", 0)
            content_block = inner.get("content_block", {})
            block_type = content_block.get("type")

            if block_type == "tool_use":
                active_blocks[index] = {
                    "type": "tool_use",
                    "id": content_block.get("id", ""),
                    "name": content_block.get("name", ""),
                    "input_json_parts": [],
                }
            elif block_type == "text":
                active_blocks[index] = {"type": "text"}

        elif inner_type == "content_block_delta":
            index = inner.get("index", 0)
            delta = inner.get("delta", {})
            delta_type = delta.get("type")

            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    full_text_parts.append(text)
                    await self.on_event({"type": "token", "content": text})

            elif delta_type == "input_json_delta":
                partial_json = delta.get("partial_json", "")
                block_info = active_blocks.get(index)
                if block_info and block_info["type"] == "tool_use":
                    block_info["input_json_parts"].append(partial_json)

        elif inner_type == "content_block_stop":
            index = inner.get("index", 0)
            block_info = active_blocks.pop(index, None)

            if block_info and block_info["type"] == "tool_use":
                # Assemble the full tool input JSON
                raw_json = "".join(block_info["input_json_parts"])
                try:
                    parsed_input = json.loads(raw_json) if raw_json else {}
                except json.JSONDecodeError:
                    parsed_input = {"_raw": raw_json}

                await self.on_event(
                    {
                        "type": "tool_call",
                        "name": block_info["name"],
                        "input": parsed_input,
                        "id": block_info["id"],
                    }
                )

        elif inner_type == "message_start":
            # Start of a new message — nothing to emit yet
            pass

        elif inner_type == "message_delta":
            # Message-level delta (e.g., stop_reason) — ignore for streaming
            pass

        elif inner_type == "message_stop":
            # End of a message in the stream — will be followed by result event
            pass
