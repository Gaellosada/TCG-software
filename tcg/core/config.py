"""Configuration loader -- reads .env from project root, builds configs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from tcg.types.config import AgentConfig, MongoConfig

logger = logging.getLogger(__name__)

# Cache the parsed .env so multiple loaders share it.
_env_cache: dict[str, str | None] | None = None


def _load_env() -> dict[str, str | None]:
    global _env_cache
    if _env_cache is None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        _env_cache = dict(dotenv_values(env_path))
    return _env_cache


def load_config() -> MongoConfig:
    """Load MongoDB configuration from environment variables or .env file.

    Priority: real env vars > .env file > defaults.
    """
    env = _load_env()
    return MongoConfig(
        uri=os.getenv("MONGO_URI") or env.get("MONGO_URI", "mongodb://localhost:27017"),
        db_name=os.getenv("MONGO_DB_NAME")
        or env.get("MONGO_DB_NAME", "tcg-instrument"),
    )


def load_agent_config() -> AgentConfig | None:
    """Load Anthropic agent configuration.

    Returns ``None`` when ``ANTHROPIC_API_KEY`` is not set -- the app
    starts normally but agent endpoints return HTTP 503.
    """
    env = _load_env()
    api_key = os.getenv("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set -- agent feature disabled")
        return None
    return AgentConfig(api_key=api_key)
