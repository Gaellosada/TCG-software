"""Configuration loader -- reads .env from project root, builds MongoConfig."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import dotenv_values

from tcg.core.tunnel import TunnelConfig
from tcg.types.config import DwhConfig, MongoConfig

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

    # Validate local_port is a valid port number.
    try:
        port = int(values["local_port"])
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, KeyError):
        raise ValueError(
            f"LOCAL_PORT must be a number between 1 and 65535, "
            f"got {values.get('local_port', '')!r}"
        ) from None

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

        # Validate local_port is numeric (load_tunnel_config validates the
        # full range; here we just prevent a malformed URI).
        if not local_port.isdigit():
            raise ValueError(f"LOCAL_PORT must be numeric, got {local_port!r}")

        uri = (
            f"mongodb://{quote_plus(user)}:{quote_plus(password)}"
            f"@localhost:{local_port}/{quote_plus(db_name)}"
            f"?authSource={quote_plus(auth_source)}&directConnection=true"
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


def load_dwh_config() -> DwhConfig:
    """Load PostgreSQL ``dwh`` configuration for the market-data read path.

    Reads ``DWH_HOST`` / ``DWH_PORT`` / ``DWH_DB`` / ``DWH_USER`` /
    ``DWH_PASSWORD`` (priority: real env > ``.env`` > defaults), mirroring
    :func:`load_config`'s python-dotenv style. ``DWH_HOST`` / ``DWH_USER`` /
    ``DWH_PASSWORD`` are required; the rest have safe defaults. Optional
    knobs ``DWH_SSLMODE`` (default ``require``) and ``DWH_STATEMENT_TIMEOUT_MS``
    (default 60000) let an operator tune without code change, so a tunneled
    localhost (``DWH_SSLMODE=disable``) and an in-VPC host both work.

    Raises ``ValueError`` listing every required variable that is missing.
    """
    env = dotenv_values(_ENV_PATH)

    required = ("DWH_HOST", "DWH_USER", "DWH_PASSWORD")
    missing = [k for k in required if not _get(k, env)]
    if missing:
        raise ValueError(
            "dwh market-data reads require the following variables which "
            f"are not set: {', '.join(missing)}"
        )

    port_raw = _get("DWH_PORT", env, "5432")
    if not port_raw.isdigit():
        raise ValueError(f"DWH_PORT must be numeric, got {port_raw!r}")

    timeout_raw = _get("DWH_STATEMENT_TIMEOUT_MS", env, "60000")
    if not timeout_raw.isdigit():
        raise ValueError(
            f"DWH_STATEMENT_TIMEOUT_MS must be numeric, got {timeout_raw!r}"
        )

    return DwhConfig(
        host=_get("DWH_HOST", env),
        port=int(port_raw),
        dbname=_get("DWH_DB", env, "dwh"),
        user=_get("DWH_USER", env),
        password=_get("DWH_PASSWORD", env),
        sslmode=_get("DWH_SSLMODE", env, "require"),
        statement_timeout_ms=int(timeout_raw),
    )
