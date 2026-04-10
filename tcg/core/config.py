"""Configuration loader -- reads .env from project root, builds MongoConfig."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from tcg.types.config import MongoConfig


def load_config() -> MongoConfig:
    """Load MongoDB configuration from environment variables or .env file.

    Priority: real env vars > .env file > defaults.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    env = dotenv_values(env_path)
    return MongoConfig(
        uri=os.getenv("MONGO_URI") or env.get("MONGO_URI", "mongodb://localhost:27017"),
        db_name=os.getenv("MONGO_DB_NAME") or env.get("MONGO_DB_NAME", "tcg-instrument"),
    )
