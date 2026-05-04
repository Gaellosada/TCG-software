"""End-to-end tests for the MongoDB Agent feature with mocked Anthropic API.

These tests exercise the FULL flow: WebSocket connection -> user message ->
agentic loop with tool calls -> response streaming.  The Anthropic API is
always mocked via ``FakeStreamContext``; MongoDB is mocked at the Motor
client level so that the real tool executors run but never hit a live DB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from tcg.core.agent.workspace import AgentWorkspace
from tcg.core.api.agent import agent_websocket, router as agent_router
from tcg.types.config import AgentConfig


# ---------------------------------------------------------------------------
# Mock helpers — replicates the Anthropic SDK streaming interface
# ---------------------------------------------------------------------------


def _text_block(text: str) -> MagicMock:
    """Build a fake TextBlock."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(tool_id: str, name: str, tool_input: dict[str, Any]) -> MagicMock:
    """Build a fake ToolUseBlock."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _response(
    content_blocks: list[MagicMock], stop_reason: str = "end_turn"
) -> MagicMock:
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


class FakeStreamContext:
    """Mimics ``async with client.messages.stream(...) as stream:``."""

    def __init__(self, resp: MagicMock) -> None:
        self._response = resp
        # Produce text events for every text block in the response
        self._text_events: list[MagicMock] = []
        for block in resp.content:
            if block.type == "text":
                ev = MagicMock()
                ev.type = "text"
                ev.text = block.text
                self._text_events.append(ev)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for ev in self._text_events:
            yield ev

    async def get_final_message(self):
        return self._response


# ---------------------------------------------------------------------------
# Fake Motor client — prevents real MongoDB connections from tool executors
# ---------------------------------------------------------------------------


class _FakeMotorCursor:
    """Minimal cursor that returns canned documents."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, *_a, **_kw):
        return self

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if length is not None:
            return self._docs[:length]
        return self._docs


class _FakeMotorCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self._docs = docs or []

    def find(self, query=None, **kwargs):
        return _FakeMotorCursor(list(self._docs))

    def aggregate(self, pipeline):
        return _FakeMotorCursor(list(self._docs))

    async def count_documents(self, query=None):
        return len(self._docs)

    async def distinct(self, field, query=None):
        return list({d.get(field) for d in self._docs if field in d})


class _FakeMotorDB:
    def __init__(
        self,
        collection_names: list[str] | None = None,
        docs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._collection_names = collection_names or []
        self._docs = docs or []

    def __getitem__(self, name: str):
        return _FakeMotorCollection(self._docs)

    async def list_collection_names(self) -> list[str]:
        return list(self._collection_names)


class FakeMotorClient:
    """Drop-in replacement for ``AsyncIOMotorClient``."""

    def __init__(
        self,
        *args: Any,
        collection_names: list[str] | None = None,
        docs: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        self._collection_names = collection_names or []
        self._docs = docs or []

    def __getitem__(self, name: str):
        return _FakeMotorDB(collection_names=self._collection_names, docs=self._docs)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# App factory for E2E tests
# ---------------------------------------------------------------------------


def _make_e2e_app(tmp_path: Path) -> FastAPI:
    """Build a minimal FastAPI app with the agent router + WS endpoint."""
    app = FastAPI()
    app.state.agent_workspace = AgentWorkspace(root=tmp_path / "workspaces")
    app.state.agent_config = AgentConfig(api_key="sk-test-fake")
    app.state.mongo_uri = "mongodb://localhost:27017"
    app.state.mongo_db_name = "test-db"
    app.include_router(agent_router)
    app.websocket("/ws/agent/{session_id}")(agent_websocket)
    return app


def _create_session(test_client: TestClient, name: str = "e2e-test") -> str:
    """Create a session via REST and return its id."""
    resp = test_client.post("/api/agent/sessions", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Helper to build a chained mock for messages.stream with multiple calls
# ---------------------------------------------------------------------------


def _make_anthropic_mock(stream_responses: list[FakeStreamContext]) -> MagicMock:
    """Build a mock AsyncAnthropic whose messages.stream returns responses in order."""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    call_idx = {"i": 0}

    def _stream_side_effect(**kwargs):
        i = call_idx["i"]
        call_idx["i"] += 1
        if i < len(stream_responses):
            return stream_responses[i]
        raise RuntimeError(f"Unexpected stream call #{i + 1}")

    mock_client.messages.stream = MagicMock(side_effect=_stream_side_effect)
    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_app(tmp_path: Path) -> FastAPI:
    return _make_e2e_app(tmp_path)


@pytest.fixture()
def e2e_client(e2e_app: FastAPI) -> TestClient:
    return TestClient(e2e_app)


# ---------------------------------------------------------------------------
# WS message collector
# ---------------------------------------------------------------------------


def _collect_ws_events(
    ws,
    *,
    until_type: str = "message_complete",
    max_events: int = 50,
) -> list[dict[str, Any]]:
    """Read JSON events from a WebSocket until the expected final event or limit."""
    events: list[dict[str, Any]] = []
    for _ in range(max_events):
        data = ws.receive_json()
        events.append(data)
        if data.get("type") == until_type:
            break
        if data.get("type") == "error":
            break
    return events


# ============================================================================
# Test Scenario 1: Simple conversation (no tools)
# ============================================================================


class TestSimpleConversation:
    """Model returns a text response without any tool calls."""

    def test_simple_text_streamed(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        resp = _response([_text_block("The answer is 42.")], stop_reason="end_turn")
        fake_stream = FakeStreamContext(resp)
        mock_client = _make_anthropic_mock([fake_stream])

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                # May receive history first (empty for new session)
                ws.send_json({"type": "message", "content": "What is 6*7?"})
                events = _collect_ws_events(ws)

        types = [e["type"] for e in events]
        assert "token" in types, f"Expected token event, got: {types}"
        assert "message_complete" in types

        # Token content matches
        token_events = [e for e in events if e["type"] == "token"]
        assert any("42" in e["content"] for e in token_events)

        # message_complete carries full text
        complete = next(e for e in events if e["type"] == "message_complete")
        assert complete["content"] == "The answer is 42."


# ============================================================================
# Test Scenario 2: Strategy intake with assumptions
# ============================================================================


class TestStrategyIntakeWithAssumptions:
    """Agent calls write_assumptions, then list_collections, then end_turn."""

    def test_assumptions_update_and_collections(
        self, e2e_client: TestClient, e2e_app: FastAPI, tmp_path: Path
    ) -> None:
        session_id = _create_session(e2e_client)

        assumptions_input = {
            "assumptions": [
                {
                    "field": "execution.fees_bps",
                    "value": 5,
                    "source": "default",
                    "confidence": "high",
                    "rationale": "Day-1 default per project policy.",
                    "group": "execution",
                },
                {
                    "field": "date_range.start",
                    "value": 20200102,
                    "source": "inferred",
                    "confidence": "medium",
                    "rationale": "Extracted from user prompt.",
                    "group": "date_range",
                },
            ]
        }

        # Step 1: write_assumptions
        resp1 = _response(
            [_tool_use_block("t1", "write_assumptions", assumptions_input)],
            stop_reason="tool_use",
        )
        # Step 2: list_collections
        resp2 = _response(
            [_tool_use_block("t2", "list_collections", {})],
            stop_reason="tool_use",
        )
        # Step 3: final text
        resp3 = _response(
            [_text_block("Strategy configured. Found 3 collections.")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
                FakeStreamContext(resp3),
            ]
        )

        # Mock Motor to avoid real MongoDB for list_collections
        fake_motor = FakeMotorClient(
            collection_names=["YAHOO_INDEX", "YAHOO_ETF", "FUT_VIX"]
        )

        with (
            patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client),
            patch("tcg.core.agent.tools.AsyncIOMotorClient", return_value=fake_motor),
        ):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json(
                    {
                        "type": "message",
                        "content": "Backtest a 20-SMA crossover on SPX from 2020 to 2024",
                    }
                )
                events = _collect_ws_events(ws)

        types = [e["type"] for e in events]

        # Verify assumptions_update event (payload is now the unwrapped list)
        assert "assumptions_update" in types, f"Missing assumptions_update in: {types}"
        assum_event = next(e for e in events if e["type"] == "assumptions_update")
        assumptions_list = assum_event["assumptions"]
        assert isinstance(assumptions_list, list)
        fields = [a["field"] for a in assumptions_list]
        assert "execution.fees_bps" in fields
        assert "date_range.start" in fields

        # Verify tool_call for list_collections
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_names = [tc["name"] for tc in tool_calls]
        assert "write_assumptions" in tool_names
        assert "list_collections" in tool_names

        # Verify the tool_result for list_collections has collection data
        list_results = [
            e
            for e in events
            if e["type"] == "tool_result" and e["name"] == "list_collections"
        ]
        assert len(list_results) == 1
        result_data = json.loads(list_results[0]["result"])
        assert "YAHOO_INDEX" in result_data["collections"]

        # Verify message_complete
        assert "message_complete" in types

        # Verify ASSUMPTIONS.json was written to workspace
        ws_mgr: AgentWorkspace = e2e_app.state.agent_workspace
        stored = ws_mgr.load_assumptions(session_id)
        assert len(stored["assumptions"]) == 2


# ============================================================================
# Test Scenario 3: MongoDB query tool call
# ============================================================================


class TestMongoDBQueryToolCall:
    """Agent calls query_mongodb with a find operation, Motor is mocked."""

    def test_query_mongodb_find(self, e2e_client: TestClient, e2e_app: FastAPI) -> None:
        session_id = _create_session(e2e_client)

        query_input = {
            "collection": "YAHOO_INDEX",
            "operation": "find",
            "query": {"instrument": "SPX"},
            "limit": 3,
        }

        resp1 = _response(
            [_tool_use_block("t1", "query_mongodb", query_input)],
            stop_reason="tool_use",
        )
        resp2 = _response(
            [_text_block("Found 2 SPX documents with recent price data.")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
            ]
        )

        sample_docs = [
            {"instrument": "SPX", "date": 20240101, "close": 4770.0},
            {"instrument": "SPX", "date": 20240102, "close": 4780.5},
        ]
        fake_motor = FakeMotorClient(docs=sample_docs)

        with (
            patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client),
            patch("tcg.core.agent.tools.AsyncIOMotorClient", return_value=fake_motor),
        ):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json(
                    {
                        "type": "message",
                        "content": "Show me recent SPX prices",
                    }
                )
                events = _collect_ws_events(ws)

        # Verify tool_call emitted
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "query_mongodb"

        # Verify tool_result contains documents
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        result_data = json.loads(tool_results[0]["result"])
        assert result_data["count"] == 2
        assert len(result_data["documents"]) == 2

        # Verify message_complete
        complete = [e for e in events if e["type"] == "message_complete"]
        assert len(complete) == 1


# ============================================================================
# Test Scenario 4: File write + read round-trip
# ============================================================================


class TestFileWriteReadRoundTrip:
    """Agent writes a file then reads it back."""

    def test_write_then_read(self, e2e_client: TestClient, e2e_app: FastAPI) -> None:
        session_id = _create_session(e2e_client)

        file_content = "meta:\n  name: SMA Crossover\n  description: Simple test\n"

        # Step 1: write_file
        resp1 = _response(
            [
                _tool_use_block(
                    "t1",
                    "write_file",
                    {
                        "path": "STRATEGY.yaml",
                        "content": file_content,
                    },
                )
            ],
            stop_reason="tool_use",
        )
        # Step 2: read_file
        resp2 = _response(
            [_tool_use_block("t2", "read_file", {"path": "STRATEGY.yaml"})],
            stop_reason="tool_use",
        )
        # Step 3: final
        resp3 = _response(
            [_text_block("Strategy file written and verified.")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
                FakeStreamContext(resp3),
            ]
        )

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json(
                    {
                        "type": "message",
                        "content": "Create a strategy file",
                    }
                )
                events = _collect_ws_events(ws)

        types = [e["type"] for e in events]
        assert "message_complete" in types

        # Verify write_file tool_result
        write_results = [
            e
            for e in events
            if e["type"] == "tool_result" and e["name"] == "write_file"
        ]
        assert len(write_results) == 1
        wr = json.loads(write_results[0]["result"])
        assert wr["status"] == "written"
        assert wr["path"] == "STRATEGY.yaml"

        # Verify read_file tool_result returned the content
        read_results = [
            e for e in events if e["type"] == "tool_result" and e["name"] == "read_file"
        ]
        assert len(read_results) == 1
        assert "SMA Crossover" in read_results[0]["result"]

        # Verify file actually exists on disk
        ws_mgr: AgentWorkspace = e2e_app.state.agent_workspace
        session_meta = ws_mgr.get_session(session_id)
        ws_path = Path(session_meta["workspace_path"])
        strategy_file = ws_path / "STRATEGY.yaml"
        assert strategy_file.exists()
        assert "SMA Crossover" in strategy_file.read_text()


# ============================================================================
# Test Scenario 5: Python execution
# ============================================================================


class TestPythonExecution:
    """Agent calls execute_python with inline code."""

    def test_execute_python_inline(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        python_code = "print('hello from agent')\nprint(2 + 2)"

        resp1 = _response(
            [_tool_use_block("t1", "execute_python", {"code": python_code})],
            stop_reason="tool_use",
        )
        resp2 = _response(
            [_text_block("Script executed. Output: hello from agent, 4")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
            ]
        )

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json(
                    {
                        "type": "message",
                        "content": "Run a quick calculation",
                    }
                )
                events = _collect_ws_events(ws)

        # Verify tool_result from execute_python
        tool_results = [
            e
            for e in events
            if e["type"] == "tool_result" and e["name"] == "execute_python"
        ]
        assert len(tool_results) == 1
        result_data = json.loads(tool_results[0]["result"])
        assert result_data["returncode"] == 0
        assert "hello from agent" in result_data["stdout"]
        assert "4" in result_data["stdout"]


# ============================================================================
# Test Scenario 6: Full pipeline simulation (multi-turn)
# ============================================================================


class TestMultiTurnPipeline:
    """Two-turn conversation: strategy setup then execution."""

    def test_multi_turn_state_persists(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        # --- Turn 1: write_assumptions + list_collections -> summary ---
        turn1_resp1 = _response(
            [
                _tool_use_block(
                    "t1",
                    "write_assumptions",
                    {
                        "assumptions": [
                            {
                                "field": "meta.name",
                                "value": "SMA Crossover",
                                "source": "inferred",
                                "confidence": "high",
                                "rationale": "From user prompt.",
                                "group": "meta",
                            }
                        ],
                    },
                )
            ],
            stop_reason="tool_use",
        )
        turn1_resp2 = _response(
            [_tool_use_block("t2", "list_collections", {})],
            stop_reason="tool_use",
        )
        turn1_resp3 = _response(
            [_text_block("Strategy configured. Ready to run.")],
            stop_reason="end_turn",
        )

        # --- Turn 2: execute_python -> results ---
        turn2_resp1 = _response(
            [
                _tool_use_block(
                    "t3",
                    "execute_python",
                    {
                        "code": "import json\nresult = {'sharpe': 1.23}\nprint(json.dumps(result))",
                    },
                )
            ],
            stop_reason="tool_use",
        )
        turn2_resp2 = _response(
            [_text_block("Backtest complete. Sharpe ratio: 1.23")],
            stop_reason="end_turn",
        )

        fake_motor = FakeMotorClient(collection_names=["YAHOO_INDEX", "YAHOO_ETF"])

        # First turn uses 3 stream responses, second uses 2
        all_streams = [
            FakeStreamContext(turn1_resp1),
            FakeStreamContext(turn1_resp2),
            FakeStreamContext(turn1_resp3),
            FakeStreamContext(turn2_resp1),
            FakeStreamContext(turn2_resp2),
        ]
        mock_client = _make_anthropic_mock(all_streams)

        with (
            patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client),
            patch("tcg.core.agent.tools.AsyncIOMotorClient", return_value=fake_motor),
        ):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                # Turn 1
                ws.send_json(
                    {
                        "type": "message",
                        "content": "Backtest SMA crossover on SPX",
                    }
                )
                turn1_events = _collect_ws_events(ws)

                # Turn 2
                ws.send_json({"type": "message", "content": "Run it"})
                turn2_events = _collect_ws_events(ws)

        # Turn 1 assertions
        t1_types = [e["type"] for e in turn1_events]
        assert "assumptions_update" in t1_types
        assert "message_complete" in t1_types
        t1_complete = next(e for e in turn1_events if e["type"] == "message_complete")
        assert "Ready to run" in t1_complete["content"]

        # Turn 2 assertions
        t2_types = [e["type"] for e in turn2_events]
        assert "message_complete" in t2_types
        t2_tool_results = [
            e
            for e in turn2_events
            if e["type"] == "tool_result" and e["name"] == "execute_python"
        ]
        assert len(t2_tool_results) == 1
        exec_result = json.loads(t2_tool_results[0]["result"])
        assert exec_result["returncode"] == 0
        assert "sharpe" in exec_result["stdout"]

        # Verify the Anthropic API received conversation history from turn 1
        # in the turn 2 call (stream was called 5 times total)
        assert mock_client.messages.stream.call_count == 5


# ============================================================================
# Test Scenario 7: Error handling
# ============================================================================


class TestErrorHandling:
    """Anthropic API raises an exception — error event sent, connection stays."""

    def test_api_error_emits_error_event(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.stream = MagicMock(
            side_effect=RuntimeError("Anthropic API unreachable")
        )

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json(
                    {
                        "type": "message",
                        "content": "This should fail",
                    }
                )
                events = _collect_ws_events(ws, until_type="error")

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "Anthropic API unreachable" in error_events[0]["message"]

    def test_connection_survives_error(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        """After an error, the client can still send another message."""
        session_id = _create_session(e2e_client)

        # First call: error. Second call: success.
        error_stream = MagicMock()
        error_stream.messages = MagicMock()

        call_idx = {"i": 0}

        def _side_effect(**kwargs):
            i = call_idx["i"]
            call_idx["i"] += 1
            if i == 0:
                raise RuntimeError("Transient failure")
            return FakeStreamContext(
                _response([_text_block("Recovered!")], stop_reason="end_turn")
            )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.stream = MagicMock(side_effect=_side_effect)

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                # First message triggers error
                ws.send_json({"type": "message", "content": "Fail please"})
                err_events = _collect_ws_events(ws, until_type="error")

                # Second message succeeds
                ws.send_json({"type": "message", "content": "Try again"})
                ok_events = _collect_ws_events(ws)

        assert any(e["type"] == "error" for e in err_events)
        assert any(e["type"] == "message_complete" for e in ok_events)
        complete = next(e for e in ok_events if e["type"] == "message_complete")
        assert complete["content"] == "Recovered!"


# ============================================================================
# Test Scenario 8: Session lifecycle (REST + WS + persistence)
# ============================================================================


class TestSessionLifecycle:
    """Create session -> WS message -> disconnect -> reconnect -> verify."""

    def test_conversation_persisted_across_connections(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client, name="lifecycle-test")

        resp = _response([_text_block("Hello there!")], stop_reason="end_turn")
        mock_client = _make_anthropic_mock([FakeStreamContext(resp)])

        # First connection: send a message
        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "message", "content": "Hi"})
                _collect_ws_events(ws)
        # WS disconnected here — conversation should be saved

        # Verify conversation persisted via REST
        conv_resp = e2e_client.get(f"/api/agent/sessions/{session_id}/conversation")
        assert conv_resp.status_code == 200
        messages = conv_resp.json()
        assert len(messages) == 2  # user + assistant
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_session_metadata_via_rest(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        """Full REST lifecycle: create, get, list, delete."""
        # Create
        resp = e2e_client.post("/api/agent/sessions", json={"name": "rest-test"})
        assert resp.status_code == 200
        session_id = resp.json()["id"]

        # Get
        get_resp = e2e_client.get(f"/api/agent/sessions/{session_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == "rest-test"

        # List
        list_resp = e2e_client.get("/api/agent/sessions")
        assert list_resp.status_code == 200
        ids = [s["id"] for s in list_resp.json()]
        assert session_id in ids

        # Delete
        del_resp = e2e_client.delete(f"/api/agent/sessions/{session_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "deleted"

        # Verify gone
        get_resp2 = e2e_client.get(f"/api/agent/sessions/{session_id}")
        assert get_resp2.status_code == 404

    def test_reconnect_receives_history(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        """On reconnect, the WS handler sends a history event with prior messages."""
        session_id = _create_session(e2e_client, name="reconnect-test")

        resp = _response([_text_block("First reply")], stop_reason="end_turn")
        mock_client = _make_anthropic_mock([FakeStreamContext(resp)])

        # First connection
        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "message", "content": "Hello"})
                _collect_ws_events(ws)

        # Reconnect — should receive history event
        resp2 = _response([_text_block("Second reply")], stop_reason="end_turn")
        mock_client2 = _make_anthropic_mock([FakeStreamContext(resp2)])

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client2):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                # First event should be history
                history_event = ws.receive_json()
                assert history_event["type"] == "history"
                assert (
                    len(history_event["messages"]) == 2
                )  # user + assistant from turn 1

                # Can still chat
                ws.send_json({"type": "message", "content": "Follow up"})
                events = _collect_ws_events(ws)
                assert any(e["type"] == "message_complete" for e in events)


# ============================================================================
# Test Scenario: Empty message rejected
# ============================================================================


class TestEdgeCases:
    """Edge cases: empty messages, unknown message types."""

    def test_empty_message_returns_error(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        with patch("tcg.core.agent.session.AsyncAnthropic"):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "message", "content": "   "})
                data = ws.receive_json()
                assert data["type"] == "error"
                assert "Empty" in data["message"]

    def test_unknown_message_type_returns_error(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        with patch("tcg.core.agent.session.AsyncAnthropic"):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "banana", "content": "hi"})
                data = ws.receive_json()
                assert data["type"] == "error"
                assert "Unknown" in data["message"]

    def test_nonexistent_session_ws_rejected(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        """Connecting to a WS for a session that doesn't exist closes immediately."""
        # WebSocketDisconnect is raised because the server closes the connection
        with pytest.raises(Exception):
            with e2e_client.websocket_connect("/ws/agent/nonexistent") as ws:
                ws.receive_json()


# ============================================================================
# Test Scenario: Multiple tool calls in single response
# ============================================================================


class TestMultipleToolsInSingleResponse:
    """Model returns multiple tool_use blocks in a single response."""

    def test_two_tools_in_one_response(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        # Response with two tool_use blocks
        resp1 = _response(
            [
                _tool_use_block(
                    "t1",
                    "write_file",
                    {
                        "path": "data/notes.txt",
                        "content": "some notes",
                    },
                ),
                _tool_use_block(
                    "t2",
                    "write_file",
                    {
                        "path": "data/config.json",
                        "content": '{"key": "value"}',
                    },
                ),
            ],
            stop_reason="tool_use",
        )
        resp2 = _response(
            [_text_block("Both files written.")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
            ]
        )

        with patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "message", "content": "Write two files"})
                events = _collect_ws_events(ws)

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 2

        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 2

        # Both files created
        ws_mgr: AgentWorkspace = e2e_app.state.agent_workspace
        session_meta = ws_mgr.get_session(session_id)
        ws_path = Path(session_meta["workspace_path"])
        assert (ws_path / "data" / "notes.txt").exists()
        assert (ws_path / "data" / "config.json").exists()


# ============================================================================
# Test Scenario: Text + tool_use mixed in response
# ============================================================================


class TestTextAndToolUseMixed:
    """Response contains both a text block and a tool_use block."""

    def test_text_before_tool_use(
        self, e2e_client: TestClient, e2e_app: FastAPI
    ) -> None:
        session_id = _create_session(e2e_client)

        resp1 = _response(
            [
                _text_block("Let me check the database..."),
                _tool_use_block("t1", "list_collections", {}),
            ],
            stop_reason="tool_use",
        )
        resp2 = _response(
            [_text_block("Found collections: YAHOO_INDEX, YAHOO_ETF")],
            stop_reason="end_turn",
        )

        mock_client = _make_anthropic_mock(
            [
                FakeStreamContext(resp1),
                FakeStreamContext(resp2),
            ]
        )

        fake_motor = FakeMotorClient(collection_names=["YAHOO_INDEX", "YAHOO_ETF"])

        with (
            patch("tcg.core.agent.session.AsyncAnthropic", return_value=mock_client),
            patch("tcg.core.agent.tools.AsyncIOMotorClient", return_value=fake_motor),
        ):
            with e2e_client.websocket_connect(f"/ws/agent/{session_id}") as ws:
                ws.send_json({"type": "message", "content": "What data do you have?"})
                events = _collect_ws_events(ws)

        types = [e["type"] for e in events]
        # Should have: token (for the text), tool_call, tool_result, token, message_complete
        assert "token" in types
        assert "tool_call" in types
        assert "tool_result" in types
        assert "message_complete" in types

        # The first text should be streamed as a token
        first_token = next(e for e in events if e["type"] == "token")
        assert "check the database" in first_token["content"]
