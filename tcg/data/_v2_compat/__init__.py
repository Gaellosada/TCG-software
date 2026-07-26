"""Compatibility layer that serves v1-shaped DTOs from the v2 star schema.

Everything in this package exists so that ``data_source="v2"`` drives the
EXACT same engine as ``data_source="v1"``. The engine must never learn that a
second warehouse exists (guardrail Sign 3: ``tcg.engine ⊥ tcg.data``), so all
accommodation — symbol translation, cycle routing, error taxonomy — lives here.

Guardrail Sign 1: nothing in this package is reachable from the v1 path.
"""

from __future__ import annotations
