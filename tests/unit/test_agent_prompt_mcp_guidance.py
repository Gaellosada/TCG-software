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

from pathlib import Path

from tcg.core.agent.pipeline_guide import PIPELINE_GUIDE_MD
from tcg.core.agent.workspace import AgentWorkspace, _CLAUDE_MD_CONTENT


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


class TestFirstTurnBulkDiscovery:
    """Issue 1 (Wave B-prompt): the First Turn Protocol must teach bulk discovery.

    Diagnosis (workspace/tasks/agent-context-and-streaming/output/issue1-diagnosis.md
    §3-5) showed a measured 47-tool first turn with 4 separate ToolSearch calls
    and 6-tool grep crawls instead of bulk-loaded schemas + parallel reads.
    """

    def test_bulk_toolsearch_directive_present(self) -> None:
        """The prompt must direct a single bulk ToolSearch call covering many tools.

        We assert that the ``select:`` clause in the prompt names ≥4 distinct
        ``mcp__mongodb__*`` tools, which forces the eager-load pattern.
        """
        normalised = _normalise(_CLAUDE_MD_CONTENT)
        # Find the select: clause and count mongodb tools inside the same
        # parenthesised ToolSearch invocation.
        select_idx = normalised.find("select:mcp__mongodb__")
        assert select_idx != -1, "Expected select:mcp__mongodb__... in prompt"
        # Window forward to the next closing quote/paren — heuristic but tight.
        window = normalised[select_idx : select_idx + 600]
        mongo_tools = window.count("mcp__mongodb__")
        assert mongo_tools >= 4, (
            f"Expected the bulk ToolSearch to list >=4 mcp__mongodb__ tools "
            f"in one select: clause; counted {mongo_tools}. Window: {window!r}"
        )

    def test_one_call_not_multiple(self) -> None:
        """The prompt must explicitly say one ToolSearch call, not multiple.

        Diagnosis showed the agent making 4 ToolSearch calls; the prompt must
        commit it to one.
        """
        lower = _CLAUDE_MD_CONTENT.lower()
        # Either "one ToolSearch", "one call", "in ONE call", or
        # "do not call ToolSearch more than once" all qualify.
        assert (
            "one toolsearch" in lower
            or "in one call" in lower
            or "in one assistant turn" in lower
            or "more than once" in lower
        ), "Prompt must commit the agent to a single bulk ToolSearch call"

    def test_parallel_read_directive_present(self) -> None:
        """The prompt must direct multiple Reads in a single assistant message."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "parallel" in lower or "single assistant message" in lower or (
            "single message" in lower
        ), "Prompt must direct parallel reads on first turn"

    def test_schema_md_referenced_in_prompt(self) -> None:
        """SCHEMA.md must be named in the prompt so the agent reads it on first turn."""
        assert "SCHEMA.md" in _CLAUDE_MD_CONTENT, (
            "Prompt must reference SCHEMA.md (scaffolded into the workspace)"
        )


class TestProjectDataApiAwareness:
    """Issue 4 (Wave B-prompt): the agent must know which data module to import.

    Diagnosis (output/issue4-diagnosis.md §0, §3, §4) showed the agent
    fabricating "MongoDB unreachable from scripts in this sandbox" because
    the prompt did not disambiguate ``tcg.data`` (async, FastAPI service)
    from ``tcg.backtester.lib.data_load`` (sync, the right answer).
    """

    def test_data_load_module_named(self) -> None:
        """The sync data API module must be named explicitly in the prompt."""
        assert "tcg.backtester.lib.data_load" in _CLAUDE_MD_CONTENT

    def test_fetch_index_bars_named(self) -> None:
        """At least one verified data_load function must be named in the prompt."""
        assert "fetch_index_bars" in _CLAUDE_MD_CONTENT

    def test_tcg_data_excluded_from_scripts(self) -> None:
        """The prompt must explicitly mark tcg.data as not-for-scripts.

        The user's bug was the agent treating ``tcg.data`` as the script-side
        API. The prompt must contain a literal exclusion near the name.
        """
        assert "tcg.data" in _CLAUDE_MD_CONTENT
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        # Locate the (lowercased) "tcg.data" mention that is NOT part of
        # "tcg.data_load" or "tcg.backtester...". Search for a free-standing
        # token (preceded by space or pipe in a markdown table cell).
        # We accept any mention as long as a "not" / "never" / "do not" /
        # "not for scripts" qualifier sits within the same 80-char window.
        idx = 0
        found_exclusion = False
        while True:
            idx = normalised.find("tcg.data", idx)
            if idx == -1:
                break
            # Skip if it's "tcg.data_load" or "tcg.backtester..."
            tail = normalised[idx : idx + len("tcg.data") + 5]
            preceding = normalised[max(0, idx - 12) : idx]
            if "_load" in tail or "backtester" in preceding:
                idx += 1
                continue
            window = normalised[max(0, idx - 120) : idx + 200]
            exclusions = ("never", "not for scripts", "do not", "don't", "no ")
            if any(w in window for w in exclusions):
                found_exclusion = True
                break
            idx += 1
        assert found_exclusion, (
            "Prompt must mark `tcg.data` as not-for-scripts (e.g. 'NEVER', "
            "'not for scripts', 'do not', 'don't' near the name)."
        )


class TestIncrementalAssumptionsDirective:
    """Issue 3 follow-on: the prompt must direct incremental ASSUMPTIONS.json writes.

    Without this, the streaming watchdog has nothing to stream — the agent
    batches all assumptions and writes once at turn end.
    """

    def test_claude_md_says_incremental(self) -> None:
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "assumptions.json" in lower
        # Look for "immediately" or "as you decide" or "do not batch" near
        # the ASSUMPTIONS.json mentions.
        assert (
            "immediately" in lower or "as you decide" in lower or "do not batch" in lower
        ), "claude_md.md must direct incremental ASSUMPTIONS.json writes"

    def test_pipeline_guide_says_incremental(self) -> None:
        lower = PIPELINE_GUIDE_MD.lower()
        assert "assumptions.json" in lower
        assert (
            "immediately" in lower
            or "incrementally" in lower
            or "do not batch" in lower
        ), "pipeline_guide.md must direct incremental ASSUMPTIONS.json writes"


class TestPhantomToolRemoved:
    """Issue 3 §3: pipeline_guide.md must not reference write_assumptions tool.

    No such tool exists in this CLI architecture; the agent has only
    Bash/Read/Write/Edit/Glob/Grep + the MongoDB MCP server. Instructing it to
    use a non-existent tool is a prompt-correctness bug.
    """

    def test_pipeline_guide_does_not_reference_write_assumptions(self) -> None:
        assert "write_assumptions" not in PIPELINE_GUIDE_MD, (
            "pipeline_guide.md must not reference the phantom write_assumptions "
            "tool. Use Write/Edit on ASSUMPTIONS.json instead."
        )

    def test_claude_md_does_not_reference_write_assumptions(self) -> None:
        assert "write_assumptions" not in _CLAUDE_MD_CONTENT


class TestSchemaMdScaffolded:
    """Issue 1+4 scaffolding contract: SCHEMA.md must land in each session.

    The library docs (BACKTESTER_GUIDE.md) reference SCHEMA.md. If the file
    isn't in the workspace, the agent crawls collections one find{limit:1} at
    a time. This test pins the contract.
    """

    def test_create_session_writes_schema_md(self, tmp_path: Path) -> None:
        ws = AgentWorkspace(root=tmp_path)
        result = ws.create_session(name="schema-md-scaffold-check")
        session_dir = Path(result["workspace_path"])
        schema_path = session_dir / "SCHEMA.md"
        assert schema_path.exists(), (
            "SCHEMA.md must be scaffolded into every new session workspace"
        )
        content = schema_path.read_text(encoding="utf-8")
        assert len(content) > 0, "SCHEMA.md must not be empty"
        # Must match source — copy, not rewrite.
        src = (
            Path(__file__).resolve().parents[2]
            / "tcg"
            / "backtester"
            / "lib"
            / "data"
            / "SCHEMA.md"
        )
        assert src.exists(), f"Source SCHEMA.md not found at {src}"
        assert content == src.read_text(encoding="utf-8"), (
            "SCHEMA.md content must match the source tcg/backtester/lib/data/SCHEMA.md"
        )


# ---------------------------------------------------------------------------
# R-1 (B3, C2-recurrence-audit): Pin End-of-turn handoff marker section
#
# A future prompt edit that removes the End-of-turn handoff marker section
# or the literal <<<TURN_HANDOFF_DONE>>> token would cause the agent to
# silently stop emitting the marker. The harness would then re-dispatch on
# every single turn until the cap fires. These tests fail loudly if any of
# the pinned sections is removed.
# ---------------------------------------------------------------------------


class TestHandoffMarkerPromptSection:
    """R-1: End-of-turn handoff marker section must be present in claude_md.md."""

    def test_handoff_marker_section_present(self) -> None:
        """Both the section header and the literal marker token must appear."""
        assert "End-of-turn handoff marker" in _CLAUDE_MD_CONTENT, (
            "claude_md.md must contain a section titled 'End-of-turn handoff marker'. "
            "Removing it causes the agent to never emit the marker -> cap fires every turn."
        )
        assert "<<<TURN_HANDOFF_DONE>>>" in _CLAUDE_MD_CONTENT, (
            "claude_md.md must contain the literal marker token '<<<TURN_HANDOFF_DONE>>>'. "
            "Removing it means the agent has no canonical token to emit."
        )

    def test_action_honesty_section_present(self) -> None:
        """The Critical Rules / action-honesty section must still be present.

        Key phrase pinned: 'Action honesty' within the Critical Rules block.
        This is the Round-5 action-honesty rule that prevents the agent from
        announcing work and then ending the turn without doing it.
        """
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "critical rules" in lower, (
            "claude_md.md must contain a 'Critical Rules' section. "
            "Removing it silently drops the action-honesty prohibition."
        )
        assert "action honesty" in lower, (
            "Critical Rules must include an 'Action honesty' rule. "
            "This is the Round-5 prompt-paired defense against announce-and-skip."
        )

    def test_first_turn_protocol_present(self) -> None:
        """The First-Turn-Protocol heading must be present.

        The First-Turn-Protocol drives bulk discovery on turn 1; removing it
        degrades session startup quality without any observable error.
        """
        assert "First Turn Protocol" in _CLAUDE_MD_CONTENT, (
            "claude_md.md must contain a 'First Turn Protocol' section heading. "
            "Removing it silently regresses bulk-discovery behavior on session start."
        )
