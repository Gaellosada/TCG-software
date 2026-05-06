"""Tests for the workspace-local ``.env`` write at session creation.

Issue 17: ``AgentWorkspace.create_session`` writes a ``.env`` file into
each session directory so that scripts the agent runs from the session
workspace can resolve ``MONGO_URI`` via the file-walk path of
``tcg.backtester.lib.mongo.resolve_env()``, not just inherited
``os.environ``. Reuses the SAME ``mongo_uri`` value resolved for the
session's ``.mcp.json`` so both files agree at session-create time.

This file is separate from ``tests/unit/test_agent_workspace.py`` per
guardrail G4 (do not modify the existing test_agent_workspace.py beyond
strict fixture cleanup).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from tcg.core.agent.workspace import AgentWorkspace


@pytest.fixture
def workspace(tmp_path: Path) -> AgentWorkspace:
    return AgentWorkspace(root=tmp_path)


class TestWorkspaceDotenvWrite:
    """Issue 17: session_workspace/.env exists and contains MONGO_URI."""

    def test_create_session_writes_dotenv(
        self, workspace: AgentWorkspace
    ) -> None:
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://test-host:27017/?replicaSet=rs0",
        ):
            session = workspace.create_session(name="dotenv-test")

        env_path = Path(session["workspace_path"]) / ".env"
        assert env_path.exists(), (
            "Issue 17: create_session must write a workspace-local"
            " .env file so resolve_env() finds MONGO_URI via the"
            " file-walk path, not just os.environ"
        )
        # F3: assert via dotenv_values (the actual consumer) rather
        # than substring search -- substring search couples the test
        # to the on-disk quoting format, which python-dotenv abstracts
        # away on read.
        parsed = dotenv_values(env_path)
        assert parsed.get("MONGO_URI") == "mongodb://test-host:27017/?replicaSet=rs0", (
            f"Issue 17: .env must round-trip MONGO_URI through python-dotenv;"
            f" got parsed={parsed!r}, raw={env_path.read_text(encoding='utf-8')!r}"
        )

    def test_dotenv_uri_matches_mcp_json_uri(
        self, workspace: AgentWorkspace
    ) -> None:
        """Single source of truth: the .env URI must equal the .mcp.json
        URI so a script reading the workspace .env and the MCP server
        reading .mcp.json see the SAME database at session-create time."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://shared-uri:27017/?authSource=admin",
        ):
            session = workspace.create_session(name="parity-test")

        session_dir = Path(session["workspace_path"])
        # F3: parse the .env via the actual consumer (python-dotenv)
        # rather than substring-match on the raw bytes; the URI is
        # quoted on disk to defend against ``#`` truncation, but
        # round-trips byte-for-byte through dotenv_values.
        env_uri = dotenv_values(session_dir / ".env").get("MONGO_URI")

        import json
        mcp_content = json.loads(
            (session_dir / ".mcp.json").read_text(encoding="utf-8")
        )
        mcp_uri = (
            mcp_content["mcpServers"]["mongodb"]["env"][
                "MDB_MCP_CONNECTION_STRING"
            ]
        )

        assert mcp_uri == "mongodb://shared-uri:27017/?authSource=admin"
        assert env_uri == mcp_uri, (
            f".env's MONGO_URI must equal .mcp.json's"
            f" MDB_MCP_CONNECTION_STRING (single source of truth);"
            f" env_uri={env_uri!r}, mcp_uri={mcp_uri!r}"
        )

    def test_dotenv_resolves_real_uri_when_unmocked(
        self, workspace: AgentWorkspace
    ) -> None:
        """Smoke: even without monkeypatching _get_mongo_uri, the .env
        file is written. Content depends on the test runner's env/.env
        but presence is invariant."""
        session = workspace.create_session(name="smoke-test")
        env_path = Path(session["workspace_path"]) / ".env"
        assert env_path.exists()
        assert env_path.read_text(encoding="utf-8").startswith("MONGO_URI=")

    def test_dotenv_trailing_newline(
        self, workspace: AgentWorkspace
    ) -> None:
        """POSIX convention: text files end with a newline. python-dotenv
        and bash-style env loaders expect this; without it, appending
        future lines (e.g. MONGO_DB_NAME) would join onto the same line."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://localhost:27017",
        ):
            session = workspace.create_session(name="newline-test")

        env_path = Path(session["workspace_path"]) / ".env"
        content = env_path.read_text(encoding="utf-8")
        assert content.endswith("\n"), (
            f".env must end with a newline; got {content!r}"
        )


# ---------------------------------------------------------------------------
# F3 (R-be-correctness): the .env value must round-trip URIs containing
# special chars (notably ``#``, which python-dotenv treats as inline-comment
# start when the value is unquoted -- silently truncating the URI on read).
# ---------------------------------------------------------------------------


class TestWorkspaceDotenvSpecialChars:
    """F3: quoting must defend against silent truncation hazards."""

    def test_dotenv_roundtrips_uri_with_hash(
        self, workspace: AgentWorkspace
    ) -> None:
        """Sanity: even a URI with ``#`` round-trips through
        python-dotenv (which only treats ``#`` as a comment marker
        when preceded by whitespace -- URIs never contain literal
        spaces per RFC 3986). The F3 quoting is a belt-and-braces
        defence against parser changes, not a fix for an active bug
        in current python-dotenv. This test pins the round-trip
        invariant under the quoted format."""
        evil_uri = "mongodb://user:pa#ss@host:27017/db?replicaSet=rs0"
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value=evil_uri,
        ):
            session = workspace.create_session(name="hash-test")

        env_path = Path(session["workspace_path"]) / ".env"
        parsed = dotenv_values(env_path)
        assert parsed.get("MONGO_URI") == evil_uri, (
            f"MONGO_URI containing '#' must round-trip through"
            f" python-dotenv unchanged."
            f" expected={evil_uri!r}, parsed={parsed.get('MONGO_URI')!r},"
            f" raw_file={env_path.read_text(encoding='utf-8')!r}"
        )

    def test_dotenv_quoted_value_format(
        self, workspace: AgentWorkspace
    ) -> None:
        """F3: the on-disk format must be single-quoted so that
        python-dotenv's whitespace-then-``#`` comment rule cannot
        truncate the value, even under hypothetical parser changes
        or accidental whitespace injection. The value-rejection
        path (``ValueError`` on embedded ``'``) is the loud-failure
        guarantee in the unsupported case."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://h:27017/db",
        ):
            session = workspace.create_session(name="quote-format-test")

        raw = (Path(session["workspace_path"]) / ".env").read_text(
            encoding="utf-8"
        )
        # Quoted format: MONGO_URI='<uri>'\n
        assert raw == "MONGO_URI='mongodb://h:27017/db'\n", (
            f"F3: .env value must be single-quoted to defend against"
            f" python-dotenv inline-comment truncation; got {raw!r}"
        )

    def test_dotenv_writer_rejects_single_quote(
        self, workspace: AgentWorkspace
    ) -> None:
        """F3: the single-quote-wrapped writer cannot escape an embedded
        single quote without becoming non-trivial. RFC 3986 §3 forbids
        unencoded ``'`` in URI authority; we reject pathological input
        loudly rather than write a malformed .env."""
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value="mongodb://u:p'q@h/db",
        ):
            with pytest.raises(ValueError, match="single quote"):
                workspace.create_session(name="quote-test")


# ---------------------------------------------------------------------------
# R-be-correctness §5 missing-test: end-to-end resolve_env discovery. The
# stated purpose of writing the .env at session creation is so scripts
# running with cwd=session_workspace can discover MONGO_URI via
# ``tcg.backtester.lib.mongo.resolve_env()``'s file-walk path. Confirm
# via the actual consumer.
# ---------------------------------------------------------------------------


class TestWorkspaceDotenvResolveEnvE2E:
    """R-be-correctness §5: confirm a fresh subprocess started with
    cwd=session_workspace and no MONGO_URI in env actually discovers
    and parses the URI via resolve_env's CWD walk."""

    def test_resolve_env_discovers_session_dotenv(
        self, workspace: AgentWorkspace
    ) -> None:
        # The subprocess runs in a fresh interpreter; resolve_env walks
        # from CWD to find a workspace marker. We invoke it via a one-
        # liner Python script with cwd=session workspace and a scrubbed
        # environment (MONGO_URI explicitly unset so the file path is
        # exercised, not the real-env override).
        target_uri = "mongodb://e2e-resolve-host:27017/?authSource=admin"
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value=target_uri,
        ):
            session = workspace.create_session(name="resolve-e2e")

        session_dir = Path(session["workspace_path"])

        # resolve_env() walks UP from CWD looking for a workspace marker
        # (strategy.py / STRATEGY.yaml / strategy/__init__.py). When the
        # agent places a strategy at the session root and runs from there,
        # the walk anchors at session_dir and the discovered candidate
        # is ``<session_dir>/.env`` -- exactly the file we just wrote.
        # Drop a strategy.py marker so the walk finds it (mirrors the
        # agent's actual workflow per CLAUDE.md "Bootstrap a new workspace").
        (session_dir / "strategy.py").write_text(
            "META = {}\n", encoding="utf-8"
        )

        # Build a clean env that strips MONGO_URI so the file path
        # is forced (resolve_env prioritises os.environ over file vals).
        import os as _os
        env = {
            k: v for k, v in _os.environ.items() if k != "MONGO_URI"
        }

        script = (
            "from tcg.backtester.lib.mongo import resolve_env;"
            " print(resolve_env()['MONGO_URI'])"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(session_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"resolve_env subprocess failed: stderr={result.stderr!r}"
        )
        resolved = result.stdout.strip()
        assert resolved == target_uri, (
            f"R-be-correctness §5: resolve_env() must discover the"
            f" session-workspace .env via the file-walk path."
            f" expected={target_uri!r}, got={resolved!r}"
        )

    def test_resolve_env_via_explicit_path_roundtrips_special_chars(
        self, workspace: AgentWorkspace
    ) -> None:
        """Companion to the cwd-walk test: even with a `#`-laden URI,
        passing the .env path explicitly to resolve_env() returns the
        full URI (verifies the F3 quoting fix end-to-end through the
        actual consumer API, not just dotenv_values directly)."""
        from tcg.backtester.lib.mongo import resolve_env

        evil_uri = "mongodb://user:pa#ss@host:27017/db"
        with patch(
            "tcg.core.agent.workspace._get_mongo_uri",
            return_value=evil_uri,
        ):
            session = workspace.create_session(name="resolve-special")

        env_path = Path(session["workspace_path"]) / ".env"
        # Strip MONGO_URI from process env so the file path wins.
        import os as _os
        with patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop("MONGO_URI", None)
            resolved = resolve_env(env_path=env_path)
        assert resolved["MONGO_URI"] == evil_uri, (
            f"F3 + R-be-correctness §5: resolve_env(env_path=...) must"
            f" round-trip a #-containing URI through the quoted .env."
            f" expected={evil_uri!r}, got={resolved.get('MONGO_URI')!r}"
        )
