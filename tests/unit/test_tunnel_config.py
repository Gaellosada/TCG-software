"""Unit tests for SSM tunnel configuration and URI assembly.

These tests use monkeypatched env vars — no AWS or MongoDB required.
``dotenv_values`` is patched to return ``{}`` so the on-disk ``.env``
file (which contains real credentials) does not leak into test results.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tcg.core.config import load_config, load_tunnel_config
from tcg.core.tunnel import TunnelConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TUNNEL_ENV = {
    "SSM_TUNNEL_ENABLED": "true",
    "SSM_BASTION_ID": "i-0132f2ba5f7ed8c81",
    "DB_REMOTE_HOST": "10.0.5.10",
    "DB_REMOTE_PORT": "27017",
    "LOCAL_PORT": "27017",
    "AWS_REGION": "eu-west-1",
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "MONGO_USER": "reader",
    "MONGO_PASSWORD": "secret123",
    "MONGO_AUTH_SOURCE": "admin",
    "MONGO_DB": "tcg-instrument",
}

_EMPTY_DOTENV = lambda _path=None: {}


@contextmanager
def _patch_env(overrides: dict[str, str] | None = None):
    """Patch both os.environ and dotenv_values so only explicit vars exist."""
    env = {**_TUNNEL_ENV, **(overrides or {})}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("tcg.core.config.dotenv_values", _EMPTY_DOTENV),
        patch("tcg.persistence._client.dotenv_values", _EMPTY_DOTENV),
    ):
        yield


@contextmanager
def _patch_env_raw(env: dict[str, str]):
    """Patch with arbitrary env dict (no tunnel defaults merged)."""
    with (
        patch.dict(os.environ, env, clear=True),
        patch("tcg.core.config.dotenv_values", _EMPTY_DOTENV),
        patch("tcg.persistence._client.dotenv_values", _EMPTY_DOTENV),
    ):
        yield


# ---------------------------------------------------------------------------
# load_tunnel_config
# ---------------------------------------------------------------------------


class TestLoadTunnelConfig:
    def test_disabled_returns_false(self):
        with _patch_env_raw({"SSM_TUNNEL_ENABLED": "false"}):
            cfg = load_tunnel_config()
            assert cfg.enabled is False

    def test_absent_returns_false(self):
        with _patch_env_raw({}):
            cfg = load_tunnel_config()
            assert cfg.enabled is False

    def test_enabled_loads_all_fields(self):
        with _patch_env():
            cfg = load_tunnel_config()
            assert cfg.enabled is True
            assert cfg.bastion_id == "i-0132f2ba5f7ed8c81"
            assert cfg.db_host == "10.0.5.10"
            assert cfg.db_port == "27017"
            assert cfg.local_port == "27017"
            assert cfg.region == "eu-west-1"
            assert cfg.aws_access_key_id == "AKIAIOSFODNN7EXAMPLE"

    def test_missing_var_raises(self):
        env = {**_TUNNEL_ENV}
        del env["AWS_REGION"]
        with _patch_env_raw(env):
            with pytest.raises(ValueError, match="AWS_REGION"):
                load_tunnel_config()

    def test_multiple_missing_vars_listed(self):
        env = {**_TUNNEL_ENV}
        del env["AWS_REGION"]
        del env["AWS_ACCESS_KEY_ID"]
        with _patch_env_raw(env):
            with pytest.raises(ValueError, match="AWS_REGION") as exc_info:
                load_tunnel_config()
            assert "AWS_ACCESS_KEY_ID" in str(exc_info.value)


# ---------------------------------------------------------------------------
# load_config — URI assembly
# ---------------------------------------------------------------------------


class TestLoadConfigTunnelMode:
    def test_assembles_uri_with_tunnel(self):
        with _patch_env():
            cfg = load_config()
            assert cfg.uri.startswith("mongodb://reader:secret123@localhost:27017/")
            assert "authSource=admin" in cfg.uri
            assert "directConnection=true" in cfg.uri
            assert cfg.db_name == "tcg-instrument"

    def test_url_encodes_special_chars(self):
        with _patch_env({"MONGO_USER": "user@domain", "MONGO_PASSWORD": "p@ss:word/"}):
            cfg = load_config()
            assert "user%40domain" in cfg.uri
            assert "p%40ss%3Aword%2F" in cfg.uri

    def test_missing_credentials_raises(self):
        env = {**_TUNNEL_ENV}
        del env["MONGO_PASSWORD"]
        with _patch_env_raw(env):
            with pytest.raises(ValueError, match="MONGO_PASSWORD"):
                load_config()


class TestLoadConfigDirectMode:
    def test_uses_mongo_uri_when_tunnel_disabled(self):
        env = {
            "SSM_TUNNEL_ENABLED": "false",
            "MONGO_URI": "mongodb://direct-host:27017",
            "MONGO_DB_NAME": "my-db",
        }
        with _patch_env_raw(env):
            cfg = load_config()
            assert cfg.uri == "mongodb://direct-host:27017"
            assert cfg.db_name == "my-db"

    def test_defaults_when_no_env(self):
        with _patch_env_raw({}):
            cfg = load_config()
            assert cfg.uri == "mongodb://localhost:27017"
            assert cfg.db_name == "tcg-instrument"


# ---------------------------------------------------------------------------
# Write URI assembly (persistence layer)
# ---------------------------------------------------------------------------


class TestWriteUriTunnelMode:
    def test_assembles_write_uri(self):
        env = {
            **_TUNNEL_ENV,
            "MONGO_APP_WRITE_USER": "app-writer",
            "MONGO_APP_WRITE_PASSWORD": "writepass",
        }
        with _patch_env_raw(env):
            from tcg.persistence._client import _read_write_uri

            uri = _read_write_uri()
            assert uri.startswith("mongodb://app-writer:writepass@localhost:27017/")
            assert "authSource=admin" in uri
            assert "directConnection=true" in uri

    def test_falls_through_without_write_creds(self):
        env = {**_TUNNEL_ENV, "MONGO_APP_WRITE_URI": "mongodb://fallback:27017"}
        with _patch_env_raw(env):
            from tcg.persistence._client import _read_write_uri

            uri = _read_write_uri()
            assert uri == "mongodb://fallback:27017"

    def test_raises_without_any_write_config(self):
        with _patch_env():
            from tcg.persistence._client import _read_write_uri

            with pytest.raises(ValueError, match="MONGO_APP_WRITE_URI"):
                _read_write_uri()
