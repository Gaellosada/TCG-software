"""Tests for tcg.core.agent.session -- AgentSession agentic loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tcg.core.agent.session import AgentSession, _serialise_content


# ---------------------------------------------------------------------------
# Helpers for building mock Anthropic responses
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(
    tool_id: str, name: str, tool_input: dict[str, Any]
) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _make_response(
    content_blocks: list[MagicMock], stop_reason: str = "end_turn"
) -> MagicMock:
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


class _FakeStreamContext:
    """Mimics ``async with client.messages.stream(...) as stream:``."""

    def __init__(self, events: list[MagicMock], response: MagicMock) -> None:
        self._events = events
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def get_final_message(self):
        return self._response


def _text_event(text: str) -> MagicMock:
    ev = MagicMock()
    ev.type = "text"
    ev.text = text
    return ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session(tmp_path: Path) -> AgentSession:
    return AgentSession(
        session_id="test-session",
        workspace_path=tmp_path,
        system_prompt="You are a test assistant.",
        api_key="sk-test-fake",
        mongo_uri="mongodb://localhost:27017",
        mongo_db_name="test-db",
        tools=[],
        tool_executors={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSerialiseContent:
    def test_text_block(self) -> None:
        block = _make_text_block("Hello")
        result = _serialise_content([block])
        assert result == [{"type": "text", "text": "Hello"}]

    def test_tool_use_block(self) -> None:
        block = _make_tool_use_block("tool_123", "query_db", {"sql": "SELECT 1"})
        result = _serialise_content([block])
        assert result == [
            {
                "type": "tool_use",
                "id": "tool_123",
                "name": "query_db",
                "input": {"sql": "SELECT 1"},
            }
        ]

    def test_mixed_blocks(self) -> None:
        blocks = [
            _make_text_block("Let me check"),
            _make_tool_use_block("t1", "query", {"q": "x"}),
        ]
        result = _serialise_content(blocks)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "tool_use"


class TestRunTurnSimpleResponse:
    async def test_simple_text_response(self, session: AgentSession) -> None:
        """Model returns a text-only response with stop_reason=end_turn."""
        text_block = _make_text_block("The answer is 42.")
        response = _make_response([text_block], stop_reason="end_turn")
        events = [_text_event("The answer is 42.")]
        stream_ctx = _FakeStreamContext(events, response)

        events_received: list[dict] = []

        async def on_event(event: dict) -> None:
            events_received.append(event)

        with patch.object(session._client.messages, "stream", return_value=stream_ctx):
            await session.run_turn("What is 6*7?", on_event)

        # Should have: token event + message_complete
        token_events = [e for e in events_received if e["type"] == "token"]
        assert len(token_events) == 1
        assert token_events[0]["content"] == "The answer is 42."

        complete_events = [
            e for e in events_received if e["type"] == "message_complete"
        ]
        assert len(complete_events) == 1
        assert complete_events[0]["content"] == "The answer is 42."

        # Conversation history should have user + assistant
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["role"] == "user"
        assert session.conversation_history[1]["role"] == "assistant"


class TestRunTurnWithToolUse:
    async def test_tool_call_then_final_response(self, session: AgentSession) -> None:
        """Model calls a tool, then produces a final text response."""
        # First API call: tool_use
        tool_block = _make_tool_use_block("t1", "list_collections", {})
        first_response = _make_response([tool_block], stop_reason="tool_use")
        first_stream = _FakeStreamContext([], first_response)

        # Second API call: text response
        text_block = _make_text_block("Found 3 collections.")
        second_response = _make_response([text_block], stop_reason="end_turn")
        second_events = [_text_event("Found 3 collections.")]
        second_stream = _FakeStreamContext(second_events, second_response)

        # Register the tool executor
        async def fake_list_collections(inp: dict) -> str:
            return json.dumps(["prices", "instruments", "options"])

        session.tool_executors["list_collections"] = fake_list_collections

        events_received: list[dict] = []

        async def on_event(event: dict) -> None:
            events_received.append(event)

        call_count = 0

        def stream_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return first_stream if call_count == 1 else second_stream

        with patch.object(
            session._client.messages, "stream", side_effect=stream_side_effect
        ):
            await session.run_turn("List collections", on_event)

        # Should have: tool_call, tool_result, token, message_complete
        types = [e["type"] for e in events_received]
        assert "tool_call" in types
        assert "tool_result" in types
        assert "message_complete" in types

        # Conversation: user, assistant (tool_use), user (tool_result), assistant (text)
        assert len(session.conversation_history) == 4


class TestRunTurnUnknownTool:
    async def test_unknown_tool_returns_error_dict(self, session: AgentSession) -> None:
        """When the model calls an unregistered tool, we return an error to the model."""
        tool_block = _make_tool_use_block("t1", "unknown_tool", {"x": 1})
        first_response = _make_response([tool_block], stop_reason="tool_use")
        first_stream = _FakeStreamContext([], first_response)

        text_block = _make_text_block("Sorry, that tool is not available.")
        second_response = _make_response([text_block], stop_reason="end_turn")
        second_stream = _FakeStreamContext(
            [_text_event("Sorry, that tool is not available.")], second_response
        )

        events_received: list[dict] = []

        async def on_event(event: dict) -> None:
            events_received.append(event)

        call_count = 0

        def stream_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return first_stream if call_count == 1 else second_stream

        with patch.object(
            session._client.messages, "stream", side_effect=stream_side_effect
        ):
            await session.run_turn("Call unknown tool", on_event)

        # The tool_result event should contain an error message
        tool_results = [e for e in events_received if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert "Unknown tool" in tool_results[0]["result"]


class TestRunTurnAPIError:
    async def test_api_error_emits_error_event(self, session: AgentSession) -> None:
        """When the Anthropic API raises, we emit an error event."""

        def stream_raises(**kwargs):
            raise RuntimeError("API connection failed")

        events_received: list[dict] = []

        async def on_event(event: dict) -> None:
            events_received.append(event)

        with patch.object(
            session._client.messages, "stream", side_effect=stream_raises
        ):
            await session.run_turn("Test error", on_event)

        error_events = [e for e in events_received if e["type"] == "error"]
        assert len(error_events) == 1
        assert "API connection failed" in error_events[0]["message"]
