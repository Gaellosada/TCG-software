from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class TCGError(Exception):
    """Base error for all TCG operations. Carries a machine-readable
    error_type and human-readable message."""
    def __init__(self, message: str, error_type: str):
        self.message = message
        self.error_type = error_type
        super().__init__(message)


class DataNotFoundError(TCGError):
    """Requested data does not exist."""
    def __init__(self, message: str):
        super().__init__(message, "data_not_found")


class DataAccessError(TCGError):
    """Storage backend failure (MongoDB down, corrupt file, timeout)."""
    def __init__(self, message: str):
        super().__init__(message, "data_access_error")


class StrategyExecutionError(TCGError):
    """Strategy code failed: syntax error, runtime error, invalid output."""
    def __init__(self, message: str):
        super().__init__(message, "strategy_execution_error")


class SimulationError(TCGError):
    """Engine-level failure: NaN equity, impossible state, etc."""
    def __init__(self, message: str):
        super().__init__(message, "simulation_error")


class ValidationError(TCGError):
    """Input validation failure (bad config, missing fields)."""
    def __init__(self, message: str):
        super().__init__(message, "validation_error")


@dataclass(frozen=True)
class ErrorResponse:
    """Structured error returned by the API. The CLI prints
    error_type + message to stderr and exits with code 1."""
    error_type: str                          # Machine-readable
    message: str                             # Human-readable
    details: dict[str, Any] | None = None    # Optional context
