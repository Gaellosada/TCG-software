from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class StrategyStage(StrEnum):
    """Lifecycle stage (from user research)."""
    TRIAL = "trial"
    VALIDATION = "validation"
    PROD = "prod"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class StrategyMeta:
    id: str
    name: str
    description: str
    stage: StrategyStage
    created_at: datetime
    updated_at: datetime | None = None
    tags: tuple[str, ...] = ()
    legacy_name: str | None = None  # Maps to legacy MongoDB strategy name


@dataclass(frozen=True)
class StrategyDefinition:
    meta: StrategyMeta
    code: str
    config: dict[str, Any]
