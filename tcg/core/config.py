"""Configuration loader -- reads .env from project root, builds MongoConfig."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import dotenv_values

from tcg.core.tunnel import TunnelConfig
from tcg.types.config import MongoConfig

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _get(key: str, env: dict[str, str | None], default: str = "") -> str:
    """Read *key* with priority: real env > .env > *default*."""
    return os.getenv(key) or env.get(key) or default


def load_tunnel_config() -> TunnelConfig:
    """Load SSM tunnel configuration from environment / .env.

    Returns a ``TunnelConfig`` with ``enabled=False`` when the tunnel is
    not requested.  When enabled, raises ``ValueError`` listing every
    required variable that is missing.
    """
    env = dotenv_values(_ENV_PATH)
    enabled = _get("SSM_TUNNEL_ENABLED", env).lower() == "true"
    if not enabled:
        return TunnelConfig(
            enabled=False,
            bastion_id="",
            db_host="",
            db_port="",
            local_port="",
            region="",
            aws_access_key_id="",
            aws_secret_access_key="",
        )

    required = {
        "SSM_BASTION_ID": "bastion_id",
        "DB_REMOTE_HOST": "db_host",
        "DB_REMOTE_PORT": "db_port",
        "LOCAL_PORT": "local_port",
        "AWS_REGION": "region",
        "AWS_ACCESS_KEY_ID": "aws_access_key_id",
        "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for env_key, field in required.items():
        val = _get(env_key, env)
        if not val:
            missing.append(env_key)
        else:
            values[field] = val

    if missing:
        raise ValueError(
            "SSM tunnel is enabled but the following required variables "
            f"are not set: {', '.join(missing)}"
        )

    return TunnelConfig(enabled=True, **values)


def load_config() -> MongoConfig:
    """Load MongoDB configuration from environment variables or .env file.

    When the SSM tunnel is enabled, the URI is assembled from individual
    credential variables instead of reading ``MONGO_URI`` directly.

    Priority: real env vars > .env file > defaults.
    """
    env = dotenv_values(_ENV_PATH)

    tunnel_enabled = _get("SSM_TUNNEL_ENABLED", env).lower() == "true"

    if tunnel_enabled:
        user = _get("MONGO_USER", env)
        password = _get("MONGO_PASSWORD", env)
        local_port = _get("LOCAL_PORT", env, "27017")
        db_name = _get("MONGO_DB", env) or _get("MONGO_DB_NAME", env, "tcg-instrument")
        auth_source = _get("MONGO_AUTH_SOURCE", env, "admin")

        if not user or not password:
            raise ValueError(
                "SSM tunnel is enabled but MONGO_USER and/or MONGO_PASSWORD "
                "are not set. These are required to assemble the connection URI."
            )

        uri = (
            f"mongodb://{quote_plus(user)}:{quote_plus(password)}"
            f"@localhost:{local_port}/{db_name}"
            f"?authSource={auth_source}&directConnection=true"
        )
    else:
        uri = _get("MONGO_URI", env, "mongodb://localhost:27017")
        db_name = _get("MONGO_DB_NAME", env, "tcg-instrument")

    return MongoConfig(
        uri=uri,
        db_name=db_name,
        app_write_db_name=_get("MONGO_APP_WRITE_DB_NAME", env, "tcg-app-data"),
        app_write_collection=_get("MONGO_APP_WRITE_COLLECTION", env, "2026-app-data"),
    )
