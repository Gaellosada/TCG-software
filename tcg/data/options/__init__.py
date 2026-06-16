"""Module 1 — ``tcg.data.options``.

Exposes the read-only options Protocol (:class:`OptionsDataReader`). The
concrete adapter now lives in :mod:`tcg.data._sql.options`
(``SqlOptionsDataReader``, backed by the PostgreSQL ``dwh`` warehouse); the
former Motor implementation was removed in the SQL cutover. ``protocol.py``
and the pure provider/strike-factor helpers in this package carry no storage
dependency and remain.

This module NEVER calls ``tcg.engine.options.pricing`` (Module 2) —
guardrail #2 forbids silent computation. Stored values surface here;
callers above the data layer must opt into computation explicitly.
"""

from tcg.data.options.protocol import OptionsDataReader

__all__ = [
    "OptionsDataReader",
]
