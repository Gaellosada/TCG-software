"""Agent session -- wraps the Anthropic Messages API with an agentic tool-use loop.

A single ``AgentSession`` instance is created per WebSocket connection and
drives one multi-turn conversation.  Each call to ``run_turn`` streams the
model response back via an ``on_event`` callback and loops until the model
emits an ``end_turn`` stop reason (no outstanding tool calls).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Retry configuration for transient API errors
_MAX_API_RETRIES = 5
_RETRY_BASE_DELAY_S = 20.0  # Rate limit is per-minute; short retries just burn attempts
_MAX_RETRY_DELAY_S = (
    60.0  # Never wait more than 60s; if API says longer, fail immediately
)
# Maximum tool-use loop iterations to prevent runaway loops
_MAX_TOOL_LOOPS = 25

# Type alias for the tool executor registry injected at creation.
# Each tool is an async callable: (input_dict) -> str | dict
ToolExecutor = Callable[[dict[str, Any]], Awaitable[str | dict[str, Any]]]


class AgentSession:
    """One agent conversation backed by the Anthropic Messages API.

    Parameters
    ----------
    session_id:
        Unique identifier (matches the workspace directory name).
    workspace_path:
        Path to the session's disk workspace (for tools that read/write files).
    system_prompt:
        The system prompt sent with every API call.
    api_key:
        Anthropic API key.
    mongo_uri:
        MongoDB connection string (forwarded to tools).
    mongo_db_name:
        Target MongoDB database name (forwarded to tools).
    model:
        Anthropic model identifier.
    max_tokens:
        Max tokens per API response.
    tools:
        Tool definitions in Anthropic API format (list of dicts with
        ``name``, ``description``, ``input_schema``).
    tool_executors:
        Mapping of tool name -> async callable that executes the tool.
    """

    def __init__(
        self,
        *,
        session_id: str,
        workspace_path: Path,
        system_prompt: str,
        api_key: str,
        mongo_uri: str,
        mongo_db_name: str,
        model: str = "claude-opus-4-6",
        max_tokens: int = 128000,
        thinking_budget: int = 100000,
        tools: list[dict[str, Any]] | None = None,
        tool_executors: dict[str, ToolExecutor] | None = None,
    ) -> None:
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.system_prompt = system_prompt
        self.mongo_uri = mongo_uri
        self.mongo_db_name = mongo_db_name
        self.model = model
        self.max_tokens = max_tokens
        self.thinking_budget = thinking_budget
        self.tools: list[dict[str, Any]] = tools or []
        self.tool_executors: dict[str, ToolExecutor] = tool_executors or {}

        self.conversation_history: list[dict[str, Any]] = []
        self._client = AsyncAnthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        user_message: str,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Execute one full user turn, potentially with multiple tool-use loops.

        Events emitted via ``on_event``:
        - ``{"type": "token", "content": "..."}``         — streamed text delta
        - ``{"type": "tool_call", "name": ..., "input": ..., "id": ...}``
        - ``{"type": "tool_result", "name": ..., "result": ...}``
        - ``{"type": "assumptions_update", "assumptions": ...}``
        - ``{"type": "message_complete", "content": "..."}``
        - ``{"type": "error", "message": "..."}``
        """
        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            await self._agentic_loop(on_event)
        except Exception as exc:
            logger.exception("Agent turn failed for session %s", self.session_id)
            await on_event({"type": "error", "message": str(exc)})

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _agentic_loop(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Loop: call the API, stream tokens, execute tools, repeat until end_turn.

        Resilience features:
        - Retries on rate limit and transient API errors with exponential backoff
        - Tool errors are passed back to the model (it can adapt its approach)
        - Loop iteration cap prevents runaway tool chains
        """
        loop_count = 0

        while True:
            loop_count += 1
            if loop_count > _MAX_TOOL_LOOPS:
                await on_event(
                    {
                        "type": "token",
                        "content": "\n\n[Reached maximum tool iterations. Stopping here.]",
                    }
                )
                await on_event({"type": "message_complete", "content": ""})
                return

            # Build API kwargs
            api_kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": self.system_prompt,
                "messages": self.conversation_history,
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                },
            }
            if self.tools:
                api_kwargs["tools"] = self.tools

            # Stream the response with retry on transient errors
            full_text_parts: list[str] = []
            response = await self._stream_with_retry(
                api_kwargs, full_text_parts, on_event
            )

            if response is None:
                # All retries exhausted — inform the user and stop
                await on_event(
                    {
                        "type": "token",
                        "content": "\n\n[Rate limit exceeded. Try switching to Sonnet (faster limits) or wait a minute before retrying.]",
                    }
                )
                await on_event({"type": "message_complete", "content": ""})
                return

            # Extract content blocks from the response for conversation history
            assistant_content = _serialise_content(response.content)
            self.conversation_history.append(
                {"role": "assistant", "content": assistant_content}
            )

            # If no tool use, we're done
            if response.stop_reason != "tool_use":
                full_text = "".join(full_text_parts)
                await on_event({"type": "message_complete", "content": full_text})
                return

            # Process each tool_use block
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                await on_event(
                    {
                        "type": "tool_call",
                        "name": tool_name,
                        "input": tool_input,
                        "id": tool_use_id,
                    }
                )

                result = await self._execute_tool(tool_name, tool_input)
                result_str = (
                    json.dumps(result, default=str)
                    if not isinstance(result, str)
                    else result
                )

                await on_event(
                    {
                        "type": "tool_result",
                        "name": tool_name,
                        "result": result_str,
                    }
                )

                # Check if the tool wrote to ASSUMPTIONS.json
                if tool_name == "write_assumptions" and isinstance(result, dict):
                    assumptions_list = result.get("assumptions", [])
                    await on_event(
                        {"type": "assumptions_update", "assumptions": assumptions_list}
                    )

                # Notify frontend when notebook is compiled
                if tool_name == "compile_notebook" and isinstance(result, dict):
                    if result.get("status") == "compiled":
                        await on_event({"type": "notebook_ready"})

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_str,
                    }
                )

            # Append tool results as a user message and loop
            self.conversation_history.append({"role": "user", "content": tool_results})

    async def _stream_with_retry(
        self,
        api_kwargs: dict[str, Any],
        full_text_parts: list[str],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> Any:
        """Stream an API call with retry on rate limit / transient errors.

        Returns the final message on success, or None if all retries fail.
        """
        for attempt in range(_MAX_API_RETRIES):
            try:
                async with self._client.messages.stream(**api_kwargs) as stream:
                    async for event in stream:
                        if event.type == "text":
                            full_text_parts.append(event.text)
                            await on_event({"type": "token", "content": event.text})
                    return await stream.get_final_message()

            except RateLimitError as exc:
                # Read retry-after header from the API response
                retry_after = None
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after_str = exc.response.headers.get("retry-after")
                    if retry_after_str:
                        try:
                            retry_after = float(retry_after_str)
                        except Exception:
                            pass

                # If API says wait longer than our cap, fail immediately
                if retry_after and retry_after > _MAX_RETRY_DELAY_S:
                    logger.warning(
                        "Rate limit retry-after=%ds exceeds cap, failing fast",
                        retry_after,
                    )
                    return None

                delay = min(
                    retry_after if retry_after else _RETRY_BASE_DELAY_S * (2**attempt),
                    _MAX_RETRY_DELAY_S,
                )
                logger.warning(
                    "Rate limited (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                if attempt < _MAX_API_RETRIES - 1:
                    await on_event(
                        {
                            "type": "token",
                            "content": f"\n[Rate limited — retrying in {delay:.0f}s...]\n",
                        }
                    )
                    await asyncio.sleep(delay)
                    full_text_parts.clear()

            except APIConnectionError as exc:
                delay = _RETRY_BASE_DELAY_S * (2**attempt)
                logger.warning(
                    "API connection error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_API_RETRIES,
                    delay,
                    exc,
                )
                if attempt < _MAX_API_RETRIES - 1:
                    await asyncio.sleep(delay)
                    full_text_parts.clear()

            except APIStatusError as exc:
                # 5xx errors are transient, 4xx (except 429) are not
                if exc.status_code >= 500:
                    delay = _RETRY_BASE_DELAY_S * (2**attempt)
                    logger.warning(
                        "API server error %d (attempt %d/%d), retrying: %s",
                        exc.status_code,
                        attempt + 1,
                        _MAX_API_RETRIES,
                        exc,
                    )
                    if attempt < _MAX_API_RETRIES - 1:
                        await asyncio.sleep(delay)
                        full_text_parts.clear()
                else:
                    # Non-retryable (e.g., 400 bad request)
                    raise

        # All retries exhausted
        logger.error("All %d API retries exhausted", _MAX_API_RETRIES)
        return None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, tool_input: dict[str, Any]
    ) -> str | dict[str, Any]:
        """Dispatch a tool call to the registered executor."""
        executor = self.tool_executors.get(name)
        if executor is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return await executor(tool_input)
        except Exception as exc:
            logger.exception("Tool %s failed in session %s", name, self.session_id)
            return {"error": f"Tool execution failed: {exc}"}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _serialise_content(content_blocks: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK ContentBlock objects to plain dicts for JSON serialisation.

    The Anthropic SDK returns typed objects (TextBlock, ToolUseBlock,
    ThinkingBlock, etc.).  We need plain dicts to store in
    conversation_history and to pass back to the API in subsequent calls.
    """
    result: list[dict[str, Any]] = []
    for block in content_blocks:
        if block.type == "thinking":
            result.append(
                {
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                }
            )
        elif block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        else:
            # Fallback: try model_dump if available, otherwise vars
            if hasattr(block, "model_dump"):
                result.append(block.model_dump())
            else:
                result.append({"type": block.type})
    return result
