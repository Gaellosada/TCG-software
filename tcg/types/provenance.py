from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum


class ResultSource(StrEnum):
    LEGACY = "legacy"              # Old Java platform
    PRECOMPUTED = "precomputed"    # Cached from precise engine run
    ON_THE_FLY = "on_the_fly"     # Just computed


@dataclass(frozen=True)
class DataVersion:
    """Tracks the exact data used in a computation.

    Without this, two runs of "the same strategy" may differ because
    data was silently corrected (advice doc, section 7).
    """
    source: str                     # "mongodb", "parquet", etc.
    snapshot_date: date | None
    vendor_version: str | None = None
    preprocessing: tuple[str, ...] = ()
    collections_accessed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provenance:
    """Full provenance record attached to every result.

    First-class property of every result object.
    The UI renders it as colour-coded badges; the CLI tags every output.
    """
    source: ResultSource
    engine: str                     # "vectorized-0.1.0", etc.
    data_version: DataVersion
    computed_at: datetime
    config_hash: str                # SHA-256 of SimConfig
    strategy_hash: str              # SHA-256 of strategy source code
