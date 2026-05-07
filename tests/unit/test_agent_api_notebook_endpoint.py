"""R7 (Issue 27 F1): GET /api/agent/sessions/{id}/notebook endpoint gate.

Contract: when ``results/notebook.ipynb`` exists on disk but contains
no executed code-cell outputs (the bypass shape -- agent skipped
``compile_workspace``), the endpoint MUST return 422
Unprocessable Entity rather than the raw 200-with-blank-notebook
response. The FE's ``getNotebook`` ``.catch()`` path already treats
non-2xx as "no notebook" -- so 422 keeps the Notebook tab disabled
without any FE change to that path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.core.agent.workspace import AgentWorkspace
from tcg.core.api.agent import router


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.agent_workspace = AgentWorkspace(root=tmp_path / "workspaces")
    app.include_router(router)
    return app


@pytest.fixture()
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _zero_output_notebook() -> dict[str, Any]:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "source": "x = 1",
                "outputs": [],
                "metadata": {},
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "source": "print(x)",
                "outputs": [],
                "metadata": {},
            },
        ],
    }


def _executed_notebook() -> dict[str, Any]:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
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


def _seed_notebook(app: FastAPI, session_id: str, nb: dict[str, Any]) -> None:
    ws = app.state.agent_workspace
    session_dir = Path(ws.get_session(session_id)["workspace_path"])
    results_dir = session_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "notebook.ipynb").write_text(json.dumps(nb), encoding="utf-8")


class TestGetNotebookGate:
    async def test_returns_422_for_zero_output_notebook(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        """Bypass-shape notebook -> 422 Unprocessable Entity."""
        create_resp = await client.post(
            "/api/agent/sessions", json={"name": "BypassNB"}
        )
        session_id = create_resp.json()["id"]
        _seed_notebook(app, session_id, _zero_output_notebook())

        resp = await client.get(
            f"/api/agent/sessions/{session_id}/notebook"
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "notebook_no_outputs"

    async def test_returns_200_for_executed_notebook(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        """Compiled-with-outputs notebook -> 200 OK with body."""
        create_resp = await client.post(
            "/api/agent/sessions", json={"name": "GoodNB"}
        )
        session_id = create_resp.json()["id"]
        _seed_notebook(app, session_id, _executed_notebook())

        resp = await client.get(
            f"/api/agent/sessions/{session_id}/notebook"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["nbformat"] == 4
        assert len(body["cells"]) == 1

    async def test_404_when_no_notebook_file(
        self, client: AsyncClient
    ) -> None:
        """Missing notebook.ipynb -> 404 (unchanged from R6)."""
        create_resp = await client.post(
            "/api/agent/sessions", json={"name": "EmptyNB"}
        )
        session_id = create_resp.json()["id"]

        resp = await client.get(
            f"/api/agent/sessions/{session_id}/notebook"
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "notebook_not_found"

    async def test_404_for_missing_session(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/agent/sessions/nonexistent/notebook")
        assert resp.status_code == 404

    async def test_422_for_only_markdown_notebook(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        """A notebook with only markdown cells -> 422 (no code outputs)."""
        create_resp = await client.post(
            "/api/agent/sessions", json={"name": "MarkdownOnly"}
        )
        session_id = create_resp.json()["id"]
        nb = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {"cell_type": "markdown", "source": "# Hi", "metadata": {}}
            ],
        }
        _seed_notebook(app, session_id, nb)

        resp = await client.get(
            f"/api/agent/sessions/{session_id}/notebook"
        )
        assert resp.status_code == 422
