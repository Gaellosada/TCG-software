"""Agent session -- wraps the Anthropic Messages API with an agentic tool-use loop.

A single ``AgentSession`` instance is created per WebSocket connection and
drives one multi-turn conversation.  Each call to ``run_turn`` streams the
model response back via an ``on_event`` callback and loops until the model
emits an ``end_turn`` stop reason (no outstanding tool calls).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

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
        max_tokens: int = 16384,
        thinking_budget: int = 10000,
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
        """Loop: call the API, stream tokens, execute tools, repeat until end_turn."""
        while True:
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

            # Stream the response
            full_text_parts: list[str] = []

            async with self._client.messages.stream(**api_kwargs) as stream:
                async for event in stream:
                    if event.type == "text":
                        full_text_parts.append(event.text)
                        await on_event({"type": "token", "content": event.text})

                response = await stream.get_final_message()

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
