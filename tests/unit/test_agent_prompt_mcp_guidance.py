"""Regression tests for MCP / ToolSearch guidance in the spawned-agent prompt.

Bug 1 from `agent-runtime-bugs`: the live agent saw `mcp__mongodb__*` tools in
the deferred-tools bucket of Claude CLI 2.1.85, didn't know the `ToolSearch`
workflow used to surface them, and fell back to ad-hoc Python `pymongo`
scripts. The fix is documentation: the agent's system prompt
(`tcg/core/agent/claude_md.md`, loaded by `AgentWorkspace` at module import)
must teach `ToolSearch` and explicitly forbid the pymongo fallback.

These tests assert the prompt content includes that guidance. They cannot
exercise the running subprocess but they fail fast if the guidance is removed.
"""

from __future__ import annotations

from tcg.core.agent.workspace import _CLAUDE_MD_CONTENT


def _normalise(text: str) -> str:
    """Collapse whitespace so substring assertions tolerate reflowing."""
    return " ".join(text.split())


class TestMcpGuidance:
    def test_toolsearch_mentioned(self) -> None:
        """The prompt must name ToolSearch as the gateway to deferred tools."""
        assert "ToolSearch" in _CLAUDE_MD_CONTENT

    def test_eager_load_pattern_present(self) -> None:
        """The concrete first-turn 'select:mcp__mongodb__...' pattern must appear.

        Diagnosis recommends loading the schemas eagerly so the agent never
        encounters a deferred-tool surprise mid-turn.
        """
        assert "select:mcp__mongodb__" in _CLAUDE_MD_CONTENT

    def test_mcp_tool_naming_pattern_documented(self) -> None:
        """The agent must be told MCP tools surface as ``mcp__<server>__<tool>``."""
        assert "mcp__" in _CLAUDE_MD_CONTENT

    def test_pymongo_fallback_forbidden(self) -> None:
        """The prompt must explicitly forbid the Python pymongo fallback.

        We check both that ``pymongo`` is named and that it appears near a
        prohibition word ("never", "do not", "don't"), so a stray mention in
        an unrelated context wouldn't satisfy the test.
        """
        assert "pymongo" in _CLAUDE_MD_CONTENT.lower()

        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        idx = normalised.find("pymongo")
        assert idx != -1
        # 120-char window before the keyword is enough for "never" / "do not"
        # / "don't" to fit while staying tight enough that an unrelated
        # mention elsewhere wouldn't trigger.
        window = normalised[max(0, idx - 120) : idx + len("pymongo")]
        prohibitions = ("never", "do not", "don't", "do  not")
        assert any(word in window for word in prohibitions), (
            f"Expected a prohibition word near 'pymongo'; window was: {window!r}"
        )
