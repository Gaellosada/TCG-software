"""R7 (Issue 25 BETA): tool-aware intent satisfaction.

Tests the verb -> tool-set heuristic in
``tcg.core.agent.session._detect_unmet_intent``. The pre-R7 logic
treated ANY ``tool_use`` block as satisfying the announced intent --
so a multi-verb intent like *"I'll write a script and run it"* with
only a ``Write`` tool_use would (incorrectly) return ``(False, "")``.
The R7 logic maps each announced verb to a set of tool names that
satisfy it (Write -> {write, create, build, ...}; Bash -> {run,
execute, ...}; etc.), and any unmet verb wins.

Empirical justification (G-EMPIRICAL R7): A25 Phase 2b scanned 26
production conversations and produced the verb -> tool counts that
populate ``_VERB_TOOL_MAP``. Conversation cc96f2b4 (verbatim probe,
post-R6) is the canonical phrasing source -- ``"Now let me write..."``
is the empirical leading clause that the pre-R7 case-sensitive regex
missed.
"""

from __future__ import annotations

from tcg.core.agent.session import (
    _MCP_VERB_SET,
    _VERB_TOOL_MAP,
    _detect_unmet_intent,
)


# ---------------------------------------------------------------------------
# Spec test cases (from B25 brief)
# ---------------------------------------------------------------------------


class TestSpecCases:
    """The exact test cases the B25 brief calls out as required."""

    def test_write_and_run_with_only_write_is_unmet(self) -> None:
        """Multi-verb 'write and run' covered only by Write -> unmet 'run'."""
        text = "I'll write a Python script and run it"
        content = [
            {"type": "text", "text": text},
            {"type": "tool_use", "name": "Write", "input": {}, "id": "x"},
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()

    def test_write_and_run_with_write_plus_bash_satisfied(self) -> None:
        """'write and run' covered by Write + Bash -> satisfied."""
        text = "I'll write a Python script and run it"
        content = [
            {"type": "text", "text": text},
            {"type": "tool_use", "name": "Write", "input": {}, "id": "x"},
            {"type": "tool_use", "name": "Bash", "input": {}, "id": "y"},
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is False, f"unexpected unmet: {phrase!r}"

    def test_now_let_me_write_with_write_satisfied(self) -> None:
        """Single verb 'write' announced, Write tool used -> satisfied.

        Empirical: cc96f2b4 msg[1] tail is *"...Now let me write the
        full strategy and backtest script."* The pre-R7 regex missed
        this entirely (case-sensitive ``Let me`` did not match the
        lowercase ``let me`` after ``Now ``). R7 IGNORECASE fix +
        broadened leading-clause group covers this.
        """
        text = "Now let me write the strategy"
        content = [
            {"type": "tool_use", "name": "Write", "input": {}, "id": "x"},
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is False, f"unexpected unmet: {phrase!r}"

    def test_now_let_me_investigate_satisfied_by_mcp(self) -> None:
        """MCP tools satisfy verbs in ``_MCP_VERB_SET`` (read/inspect/...)."""
        text = "Now let me investigate the data"
        content = [
            {
                "type": "tool_use",
                "name": "mcp__mongodb__find",
                "input": {},
                "id": "x",
            },
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is False, f"unexpected unmet: {phrase!r}"

    def test_case_insensitive_now_let_me_run_with_bash(self) -> None:
        """Case-insensitive: 'now let me run it' + Bash -> satisfied."""
        text = "now let me run it"
        content = [
            {"type": "tool_use", "name": "Bash", "input": {}, "id": "x"},
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is False, f"unexpected unmet: {phrase!r}"

    def test_no_regex_match_returns_false_empty(self) -> None:
        """Text with no announced action -> (False, "")."""
        matched, phrase = _detect_unmet_intent(
            "All four steps complete; results in metrics.json.", []
        )
        assert matched is False
        assert phrase == ""

    def test_empty_text_returns_false_empty(self) -> None:
        matched, phrase = _detect_unmet_intent("", [])
        assert matched is False
        assert phrase == ""

    def test_non_string_text_returns_false_empty(self) -> None:
        matched, phrase = _detect_unmet_intent(None, [])  # type: ignore[arg-type]
        assert matched is False
        assert phrase == ""


# ---------------------------------------------------------------------------
# Verb -> tool regression fixtures (>= 10 from production phrasings)
# ---------------------------------------------------------------------------
#
# Each fixture is sourced from production conversations (cc96f2b4 + R6
# scan) or is a synthetic equivalent of an observed phrasing. The
# ``content`` shape mirrors what the CLI emits in ``assistant`` events.


class TestVerbToolRegression:
    """At least 10 verb -> tool fixtures from production phrasings."""

    def test_let_me_run_with_bash_satisfied(self) -> None:
        text = "Let me run the backtest"
        content = [{"type": "tool_use", "name": "Bash", "input": {}, "id": "1"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_let_me_run_without_tools_unmet(self) -> None:
        text = "Let me run the backtest"
        matched, phrase = _detect_unmet_intent(text, [])
        assert matched is True
        assert "run" in phrase.lower()

    def test_let_me_run_with_only_write_unmet(self) -> None:
        """'run' is NOT satisfied by Write -- needs Bash."""
        text = "Let me run the backtest"
        content = [{"type": "tool_use", "name": "Write", "input": {}, "id": "1"}]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()

    def test_im_going_to_query_with_mcp_satisfied(self) -> None:
        """'going to query the database' + mcp__mongodb__find -> satisfied."""
        text = "I'm going to query the database for option chains"
        content = [
            {"type": "tool_use", "name": "mcp__mongodb__aggregate",
             "input": {}, "id": "q"}
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_first_ill_inspect_with_read_satisfied(self) -> None:
        text = "First, I'll inspect the schema before continuing"
        content = [{"type": "tool_use", "name": "Read", "input": {}, "id": "r"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_now_ill_compile_with_bash_satisfied(self) -> None:
        text = "Now I'll compile the notebook"
        content = [{"type": "tool_use", "name": "Bash", "input": {}, "id": "b"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_alright_let_me_build_with_write_satisfied(self) -> None:
        """Empirical leading clause 'Alright, let me ...' -- post-R6 broaden."""
        text = "Alright, let me build the strategy"
        content = [
            {"type": "tool_use", "name": "Write", "input": {}, "id": "w"}
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_so_let_me_check_with_read_satisfied(self) -> None:
        """'check' covered by Read/Bash/Grep/Glob -- multi-tool verb."""
        text = "So let me check the manifest"
        content = [{"type": "tool_use", "name": "Read", "input": {}, "id": "r"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_im_about_to_fetch_with_webfetch_satisfied(self) -> None:
        text = "I'm about to fetch the documentation"
        content = [
            {"type": "tool_use", "name": "WebFetch", "input": {}, "id": "f"}
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_let_me_search_with_grep_satisfied(self) -> None:
        text = "Let me search the codebase for the pattern"
        content = [{"type": "tool_use", "name": "Grep", "input": {}, "id": "g"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_let_me_explore_with_glob_satisfied(self) -> None:
        text = "Let me explore the directory layout"
        content = [{"type": "tool_use", "name": "Glob", "input": {}, "id": "g"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_im_going_to_implement_with_edit_satisfied(self) -> None:
        text = "I'm going to implement the helper"
        content = [{"type": "tool_use", "name": "Edit", "input": {}, "id": "e"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_now_let_me_create_with_write_satisfied(self) -> None:
        """Empirical: post-tool-result phrasing ``Now let me ...``."""
        text = "Good. Now let me create the script"
        content = [{"type": "tool_use", "name": "Write", "input": {}, "id": "w"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_then_ill_test_with_bash_satisfied(self) -> None:
        text = "Then I'll test the integration"
        content = [{"type": "tool_use", "name": "Bash", "input": {}, "id": "b"}]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_let_me_validate_with_bash_satisfied(self) -> None:
        text = "Let me validate the manifest format"
        content = [{"type": "tool_use", "name": "Bash", "input": {}, "id": "b"}]
        assert _detect_unmet_intent(text, content) == (False, "")


# ---------------------------------------------------------------------------
# Multi-verb cases -- the heart of the R7 fix
# ---------------------------------------------------------------------------


class TestMultiVerbIntent:
    def test_write_and_run_with_only_write_is_unmet(self) -> None:
        """Spec case: 'and run' coordinator detected even without lead clause."""
        text = "I'll write the strategy and run the backtest"
        content = [{"type": "tool_use", "name": "Write", "input": {}, "id": "w"}]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()

    def test_write_and_run_satisfied_by_write_plus_bash(self) -> None:
        text = "I'll write the strategy and run the backtest"
        content = [
            {"type": "tool_use", "name": "Write", "input": {}, "id": "w"},
            {"type": "tool_use", "name": "Bash", "input": {}, "id": "b"},
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_query_and_then_run_only_mcp_unmet(self) -> None:
        """'query the DB and then run analysis' satisfied for query (MCP) but not run."""
        text = "I'll query the database and then run the analysis"
        content = [
            {"type": "tool_use", "name": "mcp__mongodb__find",
             "input": {}, "id": "q"}
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()

    def test_separate_sentences_each_with_lead_clause(self) -> None:
        """Two separate intent clauses; one satisfied, one not."""
        text = "I'll write the script. Then I'll run it."
        content = [{"type": "tool_use", "name": "Write", "input": {}, "id": "w"}]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()


# ---------------------------------------------------------------------------
# MCP tool coverage
# ---------------------------------------------------------------------------


class TestMcpCoverage:
    def test_mcp_satisfies_query_verb(self) -> None:
        text = "Let me query the database"
        content = [
            {"type": "tool_use", "name": "mcp__custom__find",
             "input": {}, "id": "q"}
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_mcp_satisfies_inspect_verb(self) -> None:
        text = "Let me inspect the document"
        content = [
            {"type": "tool_use", "name": "mcp__mongodb__collection-schema",
             "input": {}, "id": "i"}
        ]
        assert _detect_unmet_intent(text, content) == (False, "")

    def test_mcp_does_not_satisfy_run_verb(self) -> None:
        """'run' is NOT in _MCP_VERB_SET -- needs Bash specifically."""
        text = "Let me run the script"
        content = [
            {"type": "tool_use", "name": "mcp__mongodb__find",
             "input": {}, "id": "x"}
        ]
        matched, phrase = _detect_unmet_intent(text, content)
        assert matched is True
        assert "run" in phrase.lower()


# ---------------------------------------------------------------------------
# Case insensitivity
# ---------------------------------------------------------------------------


class TestCaseInsensitivity:
    def test_lowercase_let_me_matches(self) -> None:
        text = "now let me write something"
        matched, phrase = _detect_unmet_intent(text, [])
        assert matched is True
        assert "write" in phrase.lower()

    def test_uppercase_LET_ME_matches(self) -> None:
        text = "LET ME WRITE THE STRATEGY"
        matched, _ = _detect_unmet_intent(text, [])
        assert matched is True

    def test_mixed_case_now_Let_me_matches(self) -> None:
        text = "Now Let Me Build The Notebook"
        matched, _ = _detect_unmet_intent(text, [])
        assert matched is True


# ---------------------------------------------------------------------------
# Verb map sanity (ensures the map is wired correctly)
# ---------------------------------------------------------------------------


class TestVerbToolMapShape:
    def test_write_maps_to_write_and_notebookedit(self) -> None:
        assert "Write" in _VERB_TOOL_MAP["write"]
        assert "NotebookEdit" in _VERB_TOOL_MAP["write"]

    def test_run_maps_to_bash(self) -> None:
        assert _VERB_TOOL_MAP["run"] == {"Bash"}

    def test_query_in_mcp_verb_set(self) -> None:
        assert "query" in _MCP_VERB_SET

    def test_run_not_in_mcp_verb_set(self) -> None:
        """'run' is for code execution -- must NOT be MCP-satisfied."""
        assert "run" not in _MCP_VERB_SET


# ---------------------------------------------------------------------------
# Production phrasing fixtures (verbatim from cc96f2b4 + R6 scan)
# ---------------------------------------------------------------------------


class TestProductionPhrasings:
    """Verbatim snippets observed in the agent_workspaces conversations."""

    def test_cc96f2b4_msg1_now_let_me_write(self) -> None:
        """cc96f2b4 msg[1] tail: '...Now let me write the full strategy...'"""
        text = (
            "All 8 exports done. Let me copy everything to the workspace. "
            "Now let me write the full strategy and backtest script."
        )
        # No tool_use -> at least one verb is unmet.
        matched, phrase = _detect_unmet_intent(text, [])
        assert matched is True
        # 'write' is the canonical announce here.
        assert "write" in phrase.lower() or "let me" in phrase.lower()

    def test_cc96f2b4_msg3_now_let_me_generate(self) -> None:
        """cc96f2b4 msg[3]: '...Now let me generate the full results...'"""
        text = "Now let me generate the full results and compile the notebook."
        matched, _ = _detect_unmet_intent(text, [])
        assert matched is True

    def test_cc96f2b4_msg5_let_me_build(self) -> None:
        """cc96f2b4 msg[5]: 'Let me build the notebook directly.'"""
        text = "Let me build the notebook directly."
        matched, _ = _detect_unmet_intent(text, [])
        assert matched is True

    def test_cc96f2b4_msg5_let_me_build_satisfied_by_write(self) -> None:
        """If the agent ACTUALLY writes the notebook in the same message, satisfied."""
        text = "Let me build the notebook directly."
        content = [
            {"type": "tool_use", "name": "Write", "input": {}, "id": "w"}
        ]
        # 'build' -> {Write, NotebookEdit, Bash}; Write satisfies.
        assert _detect_unmet_intent(text, content) == (False, "")


# ---------------------------------------------------------------------------
# Lint test (C-arch SHOULD): every verb listed in ``_UNMET_INTENT_REGEX``
# must be covered by ``_VERB_TOOL_MAP``. Catches future drift where a verb
# is added to the regex without a corresponding tool mapping. The verb
# list below is the EXACT alternation in session.py:112-116; if the regex
# changes, this test must be updated -- that is the desired friction.
# ---------------------------------------------------------------------------

_REGEX_VERBS_AS_OF_R7: frozenset[str] = frozenset(
    {
        "build", "check", "complete", "compute", "continue", "create",
        "deploy", "download", "ensure", "examine", "execute", "explore",
        "fetch", "finalize", "finish", "fix", "generate", "implement",
        "inspect", "investigate", "look", "process", "produce", "query",
        "read", "report", "rewrite", "run", "search", "set", "start",
        "test", "try", "validate", "verify", "write",
    }
)


def test_every_regex_verb_is_in_verb_tool_map() -> None:
    """C-arch SHOULD: regex verb set must be subset of _VERB_TOOL_MAP keys.

    Without this test, a future round can add a verb to the regex
    (broadening detection) and forget to map it to a tool, which would
    cause _detect_unmet_intent to fall into the 'unmapped verb' fallback
    path and silently accept any tool_use as satisfying. That is exactly
    the over-permissiveness R7 BETA fixed.
    """
    missing = _REGEX_VERBS_AS_OF_R7 - set(_VERB_TOOL_MAP.keys())
    assert not missing, (
        f"Regex verbs not in _VERB_TOOL_MAP: {sorted(missing)}. "
        f"Add them to _VERB_TOOL_MAP with the appropriate tool set."
    )
