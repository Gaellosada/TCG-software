"""Public surface for the TCG-claude backtester library."""
from __future__ import annotations

from . import compile as compile
from . import (
    aliases,
    constants,
    data_load,
    diagnostics,
    engine,
    metrics,
    mongo,
    options,
    plotting,
    signals,
    snippets_registry,
    types,
    validate,
)

__all__ = [
    "signals",
    "validate",
    "types",
    "diagnostics",
    "mongo",
    "data_load",
    "options",
    "engine",
    "metrics",
    "plotting",
    "aliases",
    "constants",
    "snippets_registry",
    "compile",
]
