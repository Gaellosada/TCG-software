"""Tests for tcg.core.api.agent -- REST endpoints for agent session management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi import FastAPI

from tcg.core.agent.workspace import AgentWorkspace
from tcg.core.api.agent import router


def _make_test_app(tmp_path: Path) -> FastAPI:
    """Build a minimal FastAPI app with only the agent router."""
    app = FastAPI()
    app.state.agent_workspace = AgentWorkspace(root=tmp_path / "workspaces")
    app.include_router(router)
    return app


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return _make_test_app(tmp_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestListSessions:
    async def test_empty_initially(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agent/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_lists_created_sessions(self, client: AsyncClient) -> None:
        await client.post("/api/agent/sessions", json={"name": "A"})
        await client.post("/api/agent/sessions", json={"name": "B"})
        resp = await client.get("/api/agent/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2


class TestCreateSession:
    async def test_creates_with_name(self, client: AsyncClient) -> None:
        resp = await client.post("/api/agent/sessions", json={"name": "Test"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test"
        assert "id" in data
        assert "created_at" in data

    async def test_creates_without_name(self, client: AsyncClient) -> None:
        resp = await client.post("/api/agent/sessions", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"].startswith("Session ")


class TestDeleteSession:
    async def test_deletes_existing(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "Doomed"})
        session_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/agent/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    async def test_returns_not_found(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/agent/sessions/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"


class TestGetSession:
    async def test_returns_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "Found"})
        session_id = create_resp.json()["id"]
        resp = await client.get(f"/api/agent/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Found"

    async def test_returns_404_for_missing(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agent/sessions/nonexistent")
        assert resp.status_code == 404


class TestGetConversation:
    async def test_empty_for_new_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "New"})
        session_id = create_resp.json()["id"]
        resp = await client.get(f"/api/agent/sessions/{session_id}/conversation")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetNotebook:
    async def test_404_for_missing_session(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agent/sessions/nonexistent/notebook")
        assert resp.status_code == 404

    async def test_404_when_no_notebook(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "NoNB"})
        session_id = create_resp.json()["id"]
        resp = await client.get(f"/api/agent/sessions/{session_id}/notebook")
        assert resp.status_code == 404
        assert resp.json()["error"] == "notebook_not_found"

    async def test_returns_notebook_json(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "WithNB"})
        session_id = create_resp.json()["id"]
        # Write a fake notebook with an executed code cell so the R7
        # 422 gate (Issue 27 F1) lets it through.
        ws = app.state.agent_workspace
        session_dir = Path(ws.get_session(session_id)["workspace_path"])
        results_dir = session_dir / "results"
        results_dir.mkdir(parents=True)

        nb = {
            "nbformat": 4,
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": 1,
                    "source": "print('hi')",
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": "hi\n",
                        }
                    ],
                    "metadata": {},
                }
            ],
        }
        (results_dir / "notebook.ipynb").write_text(json.dumps(nb))
        resp = await client.get(f"/api/agent/sessions/{session_id}/notebook")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nbformat"] == 4


class TestGetAssumptions:
    async def test_404_for_missing_session(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agent/sessions/nonexistent/assumptions")
        assert resp.status_code == 404

    async def test_returns_default_for_new_session(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/agent/sessions", json={"name": "Assum"})
        session_id = create_resp.json()["id"]
        resp = await client.get(f"/api/agent/sessions/{session_id}/assumptions")
        assert resp.status_code == 200
        data = resp.json()
        assert "assumptions" in data
        assert isinstance(data["assumptions"], list)


class TestHealthEndpoint:
    async def test_available_when_claude_on_path(self, client: AsyncClient) -> None:
        with patch("tcg.core.api.agent.cli_available", return_value=True):
            resp = await client.get("/api/agent/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["available"] is True
            assert data["model"] == "claude-sonnet-4-6"

    async def test_unavailable_when_no_claude(self, client: AsyncClient) -> None:
        with patch("tcg.core.api.agent.cli_available", return_value=False):
            resp = await client.get("/api/agent/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["available"] is False
            assert data["model"] is None
