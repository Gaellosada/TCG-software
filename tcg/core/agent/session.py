"""Agent session -- wraps the Claude CLI subprocess for agentic conversations.

A single ``CLISession`` instance is created per WebSocket connection and
drives one multi-turn conversation by spawning the ``claude`` CLI in
``--print --output-format stream-json`` mode for each user turn.

The CLI handles tool execution internally (with --dangerously-skip-permissions),
so there is no need for custom tool definitions or executors on the Python side.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Model name mapping: frontend sends full model IDs, CLI accepts aliases or full names
_MODEL_MAP: dict[str, str] = {
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
}

# Bug 3 watchdog: if the CLI's stdout produces no bytes for this many
# seconds we emit a visible {type:status, status:idle_warning, seconds:N}
# event (the FE can show "agent silent for Ns") and KEEP LOOPING. We do
# NOT kill the subprocess on idle -- cancellation stays user-driven.
# This is observability, not a kill timer (guardrail G8 honoured).
#
# 120s is a balance between (a) noticing real stalls quickly and (b) not
# emitting noise during slow Anthropic streams with dense tool calls. A
# single multi-minute stall produces N cumulative events
# (seconds=120, 240, 360, ...), not N independent "silent for 120s"
# events -- FE handlers must OVERWRITE, not append.
IDLE_TIMEOUT: float = 120.0

# Issue 5: raise the asyncio StreamReader buffer ceiling for the spawned
# CLI's stdout pipe. The asyncio default is 64 KiB; CLI events that wrap
# large MCP tool results (e.g. a year of options data) routinely exceed
# that, causing readline() to raise ValueError("Separator is found, but
# chunk is longer than limit"). 10 MiB is large enough for realistic
# MCP payloads while still bounding memory if a child floods stdout.
STREAM_READER_LIMIT: int = 10 * 1024 * 1024


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
        self._cancelled = False
        self.conversation_history: list[dict[str, Any]] = []
        self._process: asyncio.subprocess.Process | None = None
        # Track file state for change detection
        self._assumptions_snapshot: str | None = None
        self._notebook_exists: bool = False
        # Issue 3 watchdog state for live ASSUMPTIONS.json streaming.
        # Mtime-gated sha256 lets us re-snapshot ASSUMPTIONS.json after
        # every parsed CLI event without paying a full hash on each tick
        # (mtime check is ~1 us; sha256 only runs when mtime changed).
        # Both reset in _snapshot_file_state at turn start so each turn
        # begins from the pre-turn baseline.
        self._last_assumptions_mtime_ns: int | None = None
        self._last_assumptions_sha: str | None = None
        # Issue 2 compaction state. The CLI emits system/status:"compacting"
        # roughly every 30s while a compaction is in progress; we only want
        # to forward the FIRST occurrence so the FE's status field stays
        # sticky (last-writer-wins). Cleared on compact_done / error /
        # turn-start so the next compaction in the same session re-fires.
        self._is_compacting: bool = False
        # Last status string forwarded to the FE. Used by api.agent's
        # _keepalive to re-emit the *current* sticky status rather than
        # always "processing" (which would clobber "compacting").
        self._current_status: str = "processing"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_cancelled(self) -> bool:
        """Whether this session has been cancelled."""
        return self._cancelled

    @is_cancelled.setter
    def is_cancelled(self, value: bool) -> None:
        self._cancelled = bool(value)

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

            # Spawn subprocess in its own process group so we can kill
            # the entire tree (MCP servers, child shells, etc.)
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(self.workspace_path),
                preexec_fn=os.setsid,
                limit=STREAM_READER_LIMIT,
                env=self._build_subprocess_env(),
            )

            # Parse stream output (consumes stdout)
            assistant_content = await self._parse_stream()

            # Drain stderr via communicate() to avoid pipe-buffer deadlock.
            # stdout is already consumed by _parse_stream(), so communicate()
            # only drains stderr here.
            _, stderr_bytes = await self._process.communicate()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

            if self._process.returncode != 0:
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
            # Post-turn file change detection fires on ALL exit paths
            # (success, error, retry) because the agent may write
            # ASSUMPTIONS.json even during a turn that ultimately errors.
            try:
                await self._check_file_changes()
            except Exception:
                logger.debug("File change check failed for session %s", self.session_id)

    async def cancel(self) -> None:
        """Kill the running subprocess and its entire process group.

        Uses ``os.killpg`` to terminate MCP servers and child shells
        that the CLI may have spawned.

        Also refreshes ``self.session_id`` and resets ``_first_turn``
        (Bug 2 fix). The Claude CLI 2.1.85 keys "Session ID already in
        use" off the persistence of ``<id>.jsonl`` in its sessions
        directory; that file survives SIGTERM/SIGKILL. Re-using the
        same id on the next spawn would therefore fail. Minting a fresh
        uuid here decouples the next turn from the now-tainted CLI
        state. Conversation continuity is preserved Python-side via
        ``self.conversation_history``.
        """
        self._cancelled = True
        if self._process and self._process.returncode is None:
            try:
                # Kill the entire process group (subprocess + children)
                os.killpg(self._process.pid, signal.SIGTERM)
                # Defensive 3s SIGTERM->SIGKILL grace period. This bounds
                # how long cancellation can hang on a misbehaving CLI/MCP
                # tree; it does NOT affect normal turn duration because
                # cancel() only runs after the user explicitly stops or
                # interrupts. Safe to keep short.
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(self._process.pid, signal.SIGKILL)
                    except OSError:
                        self._process.kill()
            except OSError:
                # Process or group already dead
                pass

        # Bug 2 fix: the CLI's transcript file <old_id>.jsonl is now
        # orphaned on disk. The next spawn must NOT re-emit that id
        # (the CLI would reject it as "already in use"). Mint a fresh
        # uuid and reset the first-turn flag so _build_command opens a
        # clean new CLI session.
        new_id = str(uuid.uuid4())
        logger.info(
            "Refreshing session id after cancel: %s -> %s", self.session_id, new_id
        )
        self.session_id = new_id
        self._first_turn = True

    # ------------------------------------------------------------------
    # Retry logic
    # ------------------------------------------------------------------

    async def _retry_as_new_session(
        self, user_message: str, model: str
    ) -> list[dict[str, Any]]:
        """Retry a failed --resume as a fresh --session-id invocation.

        Called when the CLI session store doesn't have the session (e.g.,
        after purge or server migration). Returns assistant_content.

        Bug 2 fix: the original session_id is now tainted in the CLI's
        on-disk state (or absent — which is exactly why --resume failed).
        Either way, mint a fresh uuid so --session-id can succeed.
        """
        new_id = str(uuid.uuid4())
        logger.info(
            "Retrying as new session: %s -> %s (was %s)",
            self.session_id,
            new_id,
            "first_turn" if self._first_turn else "resume",
        )
        self.session_id = new_id
        self._first_turn = True
        cmd = self._build_command(user_message, model)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(self.workspace_path),
            preexec_fn=os.setsid,
            limit=STREAM_READER_LIMIT,
            env=self._build_subprocess_env(),
        )
        assistant_content = await self._parse_stream()
        # Drain stderr via communicate() to avoid pipe-buffer deadlock
        _, stderr_bytes = await self._process.communicate()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if self._process.returncode != 0 and not assistant_content:
            error_msg = f"CLI retry failed (exit {self._process.returncode})"
            if stderr_text:
                error_msg += f": {stderr_text[:500]}"
            await self.on_event({"type": "error", "message": error_msg})

        return assistant_content

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _build_command(self, user_message: str, model: str) -> list[str]:
        """Build the claude CLI command for this turn.

        Argv hardening (Issue 1, §4-5): we pass ``--strict-mcp-config``
        together with ``--mcp-config <abs path of workspace .mcp.json>``
        so the spawned CLI does NOT merge the user's
        ``~/.claude/settings.json`` MCP servers, plugins, or
        SessionStart hooks into the agent's context. Without this,
        roughly 12 KB / ~3 KTok of foreign content bleeds into every
        spawned-CLI system prompt (measured: superpowers SessionStart
        hook ≈ 5717 B, foreign CLAUDE.md walk ≈ 7021 B).
        """
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
            "--strict-mcp-config",
            "--mcp-config",
            str(self.workspace_path / ".mcp.json"),
        ]

        if self._first_turn:
            cmd.extend(["--session-id", self.session_id])
        else:
            cmd.extend(["--resume", self.session_id])

        return cmd

    def _build_subprocess_env(self) -> dict[str, str]:
        """Build the environment dict passed to the spawned CLI.

        Issue 4 (§5): the agent's spawned Python (running scripts that
        call ``tcg.backtester.lib.data_load``) needs ``MONGO_URI`` in
        its environment. The agent's cwd is the session workspace,
        which has no ``.env`` file; the project ``.env`` lives at the
        repo root, six directories up. Resolving ``MONGO_URI`` here
        and injecting it via ``env=`` short-circuits the .env walk
        entirely and removes the agent's incentive to fabricate
        "MongoDB unreachable from Python scripts" rationalisations.

        Resolution chain mirrors ``tcg.backtester.lib.mongo``:
        process env first, then ``.env`` at repo root, then a default
        placeholder. We never strip variables already present in
        ``os.environ`` -- only add/override ``MONGO_URI``.
        """
        # Local import keeps this module decoupled from workspace.py
        # at import time (workspace.py imports session indirectly via
        # the agent router, so a top-level import would risk a cycle).
        from tcg.core.agent.workspace import _get_mongo_uri

        env = dict(os.environ)
        try:
            env["MONGO_URI"] = _get_mongo_uri()
        except Exception:
            # If env resolution itself fails, propagate whatever was
            # already in os.environ (may be empty -- the agent will see
            # the same failure either way; we don't fabricate a value).
            logger.debug("MONGO_URI resolution failed; using bare os.environ")
        return env

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

        # Bug 3 (Option B) idle watchdog: track total seconds the CLI's
        # stdout has been silent across consecutive timeouts so the FE
        # can show "agent silent for Ns". Resets to 0 each time bytes
        # arrive. We do NOT kill the subprocess on timeout -- this is
        # observability, not a kill timer (guardrail G8).
        idle_seconds_total: float = 0.0

        while not self._cancelled:
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Subprocess is alive but stdout has been silent for
                # IDLE_TIMEOUT seconds. Surface a visible status event
                # and KEEP LOOPING. The user can cancel via the existing
                # path; we never kill on idle.
                idle_seconds_total += IDLE_TIMEOUT
                logger.info(
                    "CLI silent for %ss on session %s -- emitting idle_warning",
                    int(idle_seconds_total),
                    self.session_id,
                )
                await self.on_event(
                    {
                        "type": "status",
                        "status": "idle_warning",
                        "seconds": idle_seconds_total,
                    }
                )
                continue
            except ValueError:
                # Issue 5: asyncio.StreamReader.readline() catches
                # LimitOverrunError internally and re-raises as bare
                # ValueError when a single line exceeds the reader's
                # _limit. The buffer's .clear() has already run inside
                # readline (see asyncio/streams.py line 565-571), so
                # we just emit a visible status event and KEEP LOOPING.
                # We do NOT kill the subprocess (G8 / Sign 3): the agent
                # may still emit further usable events on subsequent
                # lines, and the user can cancel via the existing path.
                logger.warning(
                    "CLI emitted a line exceeding StreamReader limit"
                    " (%d B) on session %s; skipping line and continuing",
                    STREAM_READER_LIMIT,
                    self.session_id,
                )
                await self.on_event(
                    {
                        "type": "status",
                        "status": "oversized_line",
                        "limit": STREAM_READER_LIMIT,
                        "message": (
                            "A single stdout line from the agent exceeded"
                            " the stream reader limit and was skipped."
                        ),
                    }
                )
                continue
            if not line:
                break

            # Bytes arrived: clear the idle counter.
            idle_seconds_total = 0.0

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

            # Issue 3 watchdog: between every parsed CLI event,
            # re-snapshot ASSUMPTIONS.json and emit if it changed.
            # Placed AFTER _handle_event because tool_result events
            # are the natural moment a Write/Edit on ASSUMPTIONS.json
            # becomes visible on disk -- the agent's tool just ran.
            try:
                await self._check_assumptions_changed()
            except Exception:
                # Watchdog is observability, not load-bearing. A failure
                # must not break the parse loop or kill the subprocess.
                logger.debug(
                    "ASSUMPTIONS.json watchdog tick failed for session %s",
                    self.session_id,
                )

        if self._cancelled:
            logger.info("Stream parsing cancelled for session %s", self.session_id)

        # Emit message_complete with the accumulated text — but only if
        # the turn was not cancelled (avoids confusing the frontend with
        # a completion event after a stop/interrupt).
        final_text = "".join(full_text_parts)
        if not self._cancelled:
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
            subtype = event.get("subtype", "")
            if subtype == "tool_result" or "tool_result" in str(event):
                # Try to extract tool result info
                logger.debug("System tool_result event: %s", str(event)[:300])
            elif subtype == "status":
                # Issue 2: CLI emits system/status:"compacting" once at
                # the start of compaction, then re-emits every 30 s
                # while compacting. We forward only the FIRST occurrence
                # (sticky) so the FE last-writer-wins reducer doesn't
                # flicker. _is_compacting is reset on compact_done /
                # turn-start.
                cli_status = event.get("status")
                if cli_status == "compacting":
                    if not self._is_compacting:
                        self._is_compacting = True
                        self._current_status = "compacting"
                        await self.on_event(
                            {"type": "status", "status": "compacting"}
                        )
                    # else: silent dedup -- already compacting; the keepalive
                    # will keep the FE's status sticky on "compacting" via
                    # _current_status (api/agent.py:_keepalive).
                else:
                    # Other CLI status values (null, idle, etc.) -- keep
                    # quiet here. _keepalive's heartbeat already conveys
                    # liveness; the post-compact natural token stream will
                    # implicitly clear stickiness on FE side.
                    logger.debug(
                        "system/status %r ignored on session %s",
                        cli_status,
                        self.session_id,
                    )
            elif subtype == "compact_boundary":
                # Issue 2: terminal compaction event. Lifts the sticky
                # compacting flag so the keepalive returns to the
                # "processing" baseline and the FE can resume normal
                # status handling. compact_metadata fields are forwarded
                # snake_case (matching the stream-json shape -- see
                # issue2-diagnosis.md §1).
                cm = event.get("compact_metadata", {}) or {}
                self._is_compacting = False
                self._current_status = "processing"
                await self.on_event(
                    {
                        "type": "status",
                        "status": "compact_done",
                        "trigger": cm.get("trigger", "auto"),
                        "pre_tokens": cm.get("pre_tokens", 0),
                        "preserved_segment": cm.get("preserved_segment"),
                    }
                )
            elif subtype == "microcompact_boundary":
                # CLI's own TUI ignores microcompact_boundary (binary
                # offset 17256835). We do too -- it is a smaller-scope
                # internal event with no user-meaningful effect.
                logger.debug(
                    "microcompact_boundary ignored on session %s",
                    self.session_id,
                )

        elif event_type == "user":
            # Synthetic continuation message immediately after a
            # compact_boundary (CLI bookkeeping -- isSynthetic=true).
            # We do NOT surface it as a user message to the FE (the
            # user did not type it); we just log so the trail exists.
            if event.get("isSynthetic") is True or "isCompactSummary" in str(
                event
            ):
                logger.debug(
                    "Synthetic user continuation event (post-compact) on session %s",
                    self.session_id,
                )

        else:
            # Unknown top-level event type. CLI 2.1.85 emits at least
            # rate_limit_event and bare-system events whose subtype we
            # don't currently dispatch. Surfacing here makes future CLI
            # surface changes diagnosable instead of silent.
            logger.debug(
                "Unhandled stream-json event type %r: %s",
                event_type,
                str(event)[:200],
            )

    # ------------------------------------------------------------------
    # File change detection (ASSUMPTIONS.json, notebook)
    # ------------------------------------------------------------------

    def _snapshot_file_state(self) -> None:
        """Capture file state before a turn for post-turn diff.

        Resets the Issue 3 watchdog mtime/sha trackers so the
        intra-turn watchdog starts each turn from a clean baseline
        (otherwise a write that sneaks in between turns would never
        emit because mtime/sha are unchanged across the boundary).
        """
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

        # Reset watchdog trackers. Hash the pre-turn snapshot (if any)
        # so the very first watchdog tick that observes an unchanged
        # file is a NO-OP (idempotency).
        if self._assumptions_snapshot is not None:
            self._last_assumptions_sha = hashlib.sha256(
                self._assumptions_snapshot.encode("utf-8")
            ).hexdigest()
            try:
                self._last_assumptions_mtime_ns = (
                    assumptions_path.stat().st_mtime_ns
                )
            except OSError:
                self._last_assumptions_mtime_ns = None
        else:
            self._last_assumptions_sha = None
            self._last_assumptions_mtime_ns = None

        # Compaction stickiness is per-turn -- a previous turn's
        # compaction must not leak into a new turn's status.
        self._is_compacting = False
        self._current_status = "processing"

        notebook_path = self.workspace_path / "results" / "notebook.ipynb"
        self._notebook_exists = notebook_path.exists()

    async def _check_assumptions_changed(self) -> None:
        """Issue 3: re-snapshot ASSUMPTIONS.json mid-turn and emit on delta.

        Cheap: mtime probe is ~1 us; sha256 only fires when mtime moved
        (~5-25 us depending on file size). Even at hundreds of stream
        events per second the overhead is <0.1%.

        Emission shape REUSES the existing post-turn ``assumptions_update``
        contract (full snapshot of ``data["assumptions"]``). FE handler
        already does pure replace, so mid-turn events compose cleanly
        with the post-turn safety-net emit in ``_check_file_changes``.
        Idempotency: if the same content is written twice (mtime moves,
        sha unchanged) we DO NOT re-emit. JSONDecodeError is swallowed
        -- the agent may be mid-write; a future tick will catch the
        finished file.
        """
        path = self.workspace_path / "ASSUMPTIONS.json"
        try:
            stat_result = path.stat()
        except OSError:
            # File deleted or unreadable mid-turn. Per contract (§6.4),
            # do NOT emit an empty list -- that would falsely tell the
            # FE the agent wiped its log. Just skip this tick.
            return

        mtime_ns = stat_result.st_mtime_ns
        if (
            self._last_assumptions_mtime_ns is not None
            and mtime_ns == self._last_assumptions_mtime_ns
        ):
            # Cheap path: file untouched since last tick.
            return
        self._last_assumptions_mtime_ns = mtime_ns

        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            return

        sha = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if sha == self._last_assumptions_sha:
            # mtime moved but bytes are identical (e.g., agent rewrote
            # the same JSON). No FE update needed.
            return
        self._last_assumptions_sha = sha

        try:
            data = json.loads(current)
        except json.JSONDecodeError:
            # Agent is likely mid-write. Roll back the sha so the next
            # tick (after the write completes) re-evaluates.
            self._last_assumptions_sha = None
            return

        assumptions_list = data.get("assumptions", [])
        await self.on_event(
            {"type": "assumptions_update", "assumptions": assumptions_list}
        )

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
