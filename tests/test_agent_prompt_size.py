"""Verify the agent system prompt stays within token budget."""

import pytest
from tcg.core.agent.prompt import build_system_prompt
from tcg.core.agent.pipeline_guide import PIPELINE_GUIDE_MD


def test_system_prompt_under_3000_tokens():
    """System prompt must be under 3000 tokens (~12000 chars)."""
    prompt = build_system_prompt()
    # Conservative estimate: 1 token ~ 4 chars for English text
    est_tokens = len(prompt) / 4
    assert est_tokens < 3000, f"Prompt is ~{est_tokens:.0f} tokens, must be under 3000"


def test_pipeline_guide_reasonable_size():
    """Pipeline guide should be between 2000-6000 tokens."""
    est_tokens = len(PIPELINE_GUIDE_MD) / 4
    assert 2000 < est_tokens < 6000, f"Guide is ~{est_tokens:.0f} tokens"


def test_total_first_turn_budget():
    """System prompt + guide should stay under 10k tokens for rate limit."""
    prompt = build_system_prompt()
    total = len(prompt) + len(PIPELINE_GUIDE_MD)
    est_tokens = total / 4
    assert est_tokens < 10000, f"Total first-turn load is ~{est_tokens:.0f} tokens"
