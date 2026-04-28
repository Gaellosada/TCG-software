"""Module 1 — ``tcg.data.options``.

Read-only Mongo adapter for OPT_* collections. Exposes a typed Protocol
(:class:`OptionsDataReader`) and a Motor-backed implementation
(:class:`MongoOptionsDataReader`).

This module NEVER calls ``tcg.engine.options.pricing`` (Module 2) —
guardrail #2 forbids silent computation. Stored values surface here;
callers above the data layer must opt into computation explicitly.
"""

from tcg.data.options.protocol import OptionsDataReader
from tcg.data.options.reader import MongoOptionsDataReader

__all__ = [
    "OptionsDataReader",
    "MongoOptionsDataReader",
]
