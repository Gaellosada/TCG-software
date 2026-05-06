"""PIPELINE_GUIDE.md content written into each new agent session workspace.

Contains the full pipeline phases, probe catalog, and STRATEGY.yaml schema
so the agent can read it on first turn rather than carrying it in the system prompt.
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_GUIDE_MD = (Path(__file__).parent / "pipeline_guide.md").read_text(
    encoding="utf-8"
)
