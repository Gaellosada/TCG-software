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


class OptionsValidationError(TCGError):
    """Raised when options API request parameters are invalid."""
    def __init__(self, message: str):
        super().__init__(message, "options_validation_error")


class OptionsContractNotFound(TCGError):
    """Raised when a specific options contract cannot be found in Mongo."""
    def __init__(self, message: str):
        super().__init__(message, "options_contract_not_found")


class OptionsSelectionError(TCGError):
    """Raised when selection criterion fails on a non-empty chain (e.g., ByDelta with all delta_stored=None and no compute opt-in)."""
    def __init__(self, message: str):
        super().__init__(message, "options_selection_error")


class OptionsDataAccessError(TCGError):
    """Raised when the options Mongo read fails (timeout, network)."""
    def __init__(self, message: str):
        super().__init__(message, "options_data_access_error")


@dataclass(frozen=True)
class ErrorResponse:
    """Structured error returned by the API. The CLI prints
    error_type + message to stderr and exits with code 1."""
    error_type: str                          # Machine-readable
    message: str                             # Human-readable
    details: dict[str, Any] | None = None    # Optional context
