"""Regression tests for action-honesty, CWD discipline, and network-sandbox
guidance in the agent system prompt.

Round-5 edits (Issues 16a, 17, 18):
- Edit 1 (Issue 16a PRIMARY): step-3 now requires polling and staying on-turn.
- Edit 2 (Issue 16a SECONDARY): new "Action honesty" + "Never end a turn" rules.
- Edit 3 (Issue 17): CWD discipline note on Bash subprocesses.
- Edit 4 (Issue 18): network-sandbox warning near the Project data API table.

Each test was verified RED against the pre-edit prompt (e695c0d baseline)
and GREEN after the edits were applied. See B-prompt_report.md for
RED→GREEN evidence.
"""

from __future__ import annotations

import re

from tcg.core.agent.workspace import _CLAUDE_MD_CONTENT


def _normalise(text: str) -> str:
    """Collapse whitespace so substring assertions tolerate reflowing."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Edit 1 — step-3 "in the same turn" + polling instruction (Issue 16a PRIMARY)
# ---------------------------------------------------------------------------


class TestStep3InSameTurn:
    """Step-3 of the First Turn Protocol must forbid ending the turn early."""

    def test_step3_in_same_turn_phrase(self) -> None:
        """step-3 paragraph must contain 'in the same turn'."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        # Locate step-3 by its stable anchor
        anchor = "start large data fetches immediately"
        idx = normalised.find(anchor)
        assert idx != -1, f"Could not locate step-3 anchor: {anchor!r}"
        # Search forward 600 chars (covers the whole paragraph)
        window = normalised[idx : idx + 600]
        assert "in the same turn" in window, (
            "step-3 must contain 'in the same turn' to prevent the agent "
            "ending the turn while a background Bash is still running. "
            f"Window was: {window!r}"
        )

    def test_step3_do_not_end_turn_while_background_running(self) -> None:
        """step-3 must explicitly say not to end the turn while background Bash runs."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "start large data fetches immediately"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 600]
        assert "do not end the turn while" in window, (
            "step-3 must say 'do not end the turn while' to forbid premature "
            f"end_turn after kicking off run_in_background. Window: {window!r}"
        )

    def test_step3_polling_cue_present(self) -> None:
        """step-3 must include a polling instruction (.output file or poll keyword)."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "start large data fetches immediately"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 600]
        assert any(cue in window for cue in (".output", "poll", "bashoutput")), (
            "step-3 must include a concrete polling cue (.output / poll / BashOutput) "
            "so the agent knows how to wait for the background job. "
            f"Window: {window!r}"
        )

    def test_step3_run_in_background_bias_preserved(self) -> None:
        """Round-4 invariant: step-3 must still bias toward Bash run_in_background."""
        assert "Bash run_in_background" in _CLAUDE_MD_CONTENT, (
            "Round-4 Issue-15 invariant: step-3 must retain the 'Bash run_in_background' "
            "bias for parallel data fetches. This phrase must not be removed."
        )


# ---------------------------------------------------------------------------
# Edit 2 — Action honesty + Never-end-while-background rules (Issue 16a SECONDARY)
# ---------------------------------------------------------------------------


class TestActionHonestyRule:
    """Critical Rules section must contain the action-honesty prohibition."""

    def test_action_honesty_label_present(self) -> None:
        """The prompt must contain 'Action honesty' as a named rule."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "action honesty" in lower, (
            "Critical Rules must include an 'Action honesty' named rule "
            "so the agent can identify the failure mode by name."
        )

    def test_action_honesty_same_message_requirement(self) -> None:
        """The action-honesty rule must require the tool call in the same message."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "action honesty"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 400]
        assert "same assistant message" in window or "same message" in window, (
            "Action honesty rule must say 'same assistant message' or 'same message' "
            f"to be concrete. Window: {window!r}"
        )

    def test_action_honesty_future_tense_examples(self) -> None:
        """The action-honesty rule must name ALL three concrete future-tense trigger phrases.

        Tightened from R-prompt-behavior gap T2: previously this test passed if ANY of
        the three substrings appeared in the window, so a sloppy edit that dropped two
        out of three would still pass. The canonical rule wording lists all three
        ('I'll', 'Let me', 'Now the') as field-reported failure-mode triggers, and a
        future edit that prunes any of them should be a deliberate decision — not
        silently green.
        """
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "action honesty"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 600]
        # All three trigger phrases must appear in the rule paragraph.
        triggers = ("i'll", "let me", "now the")
        missing = [t for t in triggers if t not in window]
        assert not missing, (
            "Action honesty rule must name ALL three future-tense trigger phrases "
            f"('I'll', 'Let me', 'Now the'). Missing: {missing}. Window: {window!r}"
        )

    def test_never_end_turn_while_background_rule_present(self) -> None:
        """Critical Rules must contain a 'Never end a turn while … run_in_background' rule."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "never end a turn while"
        idx = normalised.find(anchor)
        assert idx != -1, (
            "Critical Rules must contain 'Never end a turn while' to forbid "
            "premature end_turn after launching a background Bash job."
        )
        # The rule must be near 'run_in_background'
        window = normalised[idx : idx + 300]
        assert "run_in_background" in window, (
            "The 'never end a turn' rule must reference 'run_in_background' "
            f"so it is clearly linked to the background-Bash pattern. Window: {window!r}"
        )

    def test_never_end_turn_polling_cue(self) -> None:
        """The 'never end a turn' rule must mention polling the .output path."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "never end a turn while"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 300]
        assert ".output" in window or "poll" in window, (
            "The 'never end a turn' rule must provide a concrete polling cue "
            f"(.output or poll). Window: {window!r}"
        )

    def test_not_turn_ending_sentence_example(self) -> None:
        """The rule must give a concrete example of a non-turn-ending sentence.

        Tightened from R-prompt-behavior gap T3: previously this test had a too-generous
        OR fallback that matched 'backtest is running' anywhere in the prompt as long as
        'not' co-occurred. The fallback could trigger on unrelated co-occurrences. The
        tightened version anchors on the 'never end a turn while' rule and requires the
        canonical 'Backtest is running' example to appear *within the rule's window*
        and to be marked as **not** a turn-ending sentence inside the same window.
        """
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "never end a turn while"
        idx = normalised.find(anchor)
        assert idx != -1, (
            "Could not locate 'never end a turn while' anchor; the rule itself is missing."
        )
        # The example must be in the rule's own paragraph (first ~400 chars after the anchor).
        window = normalised[idx : idx + 400]
        assert "backtest is running" in window, (
            "The 'never end a turn' rule must include the canonical 'Backtest is running' "
            f"example within its own paragraph. Window: {window!r}"
        )
        # And the example must be flagged as NOT a turn-ending sentence (literal phrase).
        assert "not" in window and "turn-ending" in window, (
            "The example must be explicitly flagged as 'not' a 'turn-ending' sentence. "
            f"Window: {window!r}"
        )


# ---------------------------------------------------------------------------
# Edit 3 — CWD discipline nudge (Issue 17)
# ---------------------------------------------------------------------------


class TestCwdDiscipline:
    """Critical Rules must document CWD = session workspace and non-persistence."""

    def test_cwd_equals_session_workspace_documented(self) -> None:
        """The prompt must state that Bash subprocesses start with CWD = session workspace."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "session workspace" in lower, (
            "Critical Rules must document that Bash CWD = session workspace."
        )
        normalised = _normalise(lower)
        # Verify it appears near the Path.cwd() bullet
        idx = normalised.find("path.cwd()")
        assert idx != -1
        window = normalised[idx : idx + 400]
        assert "session workspace" in window, (
            "The CWD = session workspace note must appear near the Path.cwd() bullet. "
            f"Window: {window!r}"
        )

    def test_cwd_does_not_persist_documented(self) -> None:
        """The prompt must warn that CWD does not persist across Bash invocations."""
        lower = _CLAUDE_MD_CONTENT.lower()
        # Accept either "does not persist" or "not persist" within context
        assert "does not persist" in lower or "not persist" in lower, (
            "Critical Rules must warn that CWD does NOT persist across separate "
            "Bash invocations within a turn."
        )

    def test_cwd_absolute_paths_recommended(self) -> None:
        """The prompt must recommend absolute paths when CWD persistence is needed."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "cwd does not persist" if "cwd does not persist" in normalised else "not persist"
        idx = normalised.find(anchor)
        if idx == -1:
            idx = normalised.find("path.cwd()")
        assert idx != -1
        window = normalised[idx : idx + 400]
        assert "absolute path" in window, (
            "Near the CWD non-persistence warning, the prompt must recommend "
            f"using absolute paths. Window: {window!r}"
        )


# ---------------------------------------------------------------------------
# Edit 4 — Network sandbox warning (Issue 18)
# ---------------------------------------------------------------------------


class TestNetworkSandboxWarning:
    """A network-sandbox warning must appear near the Project data API table."""

    def test_network_sandbox_warning_present(self) -> None:
        """The prompt must contain a 'Network sandbox warning' or equivalent."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "network sandbox" in lower or "network-isolated" in lower, (
            "The prompt must warn the agent about the Bash network sandbox "
            "(CLI isolation that blocks off-host IPs)."
        )

    def test_sandbox_warning_near_data_api_section(self) -> None:
        """The sandbox warning must appear within the 'Project data API' section.

        The section spans from '## Project data API' to the next '##' heading.
        The table in the section is large, so we search the whole section rather
        than a fixed char window.
        """
        # Work on the raw (non-normalised) content for section slicing
        lower = _CLAUDE_MD_CONTENT.lower()
        section_anchor = "project data api"
        section_idx = lower.find(section_anchor)
        assert section_idx != -1, "Expected 'Project data API' section in prompt"
        # Find the next ## heading after the section starts
        next_heading_idx = lower.find("\n## ", section_idx + len(section_anchor))
        if next_heading_idx == -1:
            next_heading_idx = len(lower)
        section_text = lower[section_idx:next_heading_idx]
        assert "network sandbox" in section_text or "network-isolated" in section_text, (
            "The network-sandbox warning must appear within the 'Project data API' "
            "section (before the next ## heading). It was not found in the section."
        )

    def test_network_unreachable_consequence_stated(self) -> None:
        """The warning must state the consequence: 'Network is unreachable'."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "network is unreachable" in lower, (
            "The network-sandbox warning must quote 'Network is unreachable' so "
            "the agent recognises the error when it occurs."
        )

    def test_sandbox_mcp_workaround_stated(self) -> None:
        """The warning must prescribe the MCP workaround (find/aggregate + local file)."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "network sandbox" if "network sandbox" in normalised else "network-isolated"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 500]
        # Must mention MCP tools as the workaround
        assert "mcp__mongodb__find" in window or "mcp__mongodb__aggregate" in window, (
            "The sandbox warning must prescribe the MCP workaround "
            f"(mcp__mongodb__find/aggregate). Window: {window!r}"
        )

    def test_no_rationalise_lib_broken(self) -> None:
        """The warning must tell the agent not to rationalise the failure as 'lib broken'."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "rationalise" in lower or "rationalize" in lower, (
            "The sandbox warning must explicitly tell the agent not to rationalise "
            "the Network-unreachable error as 'the lib is broken'."
        )

    def test_canonical_import_path_preserved(self) -> None:
        """Round-4 invariant: tcg.backtester.lib canonical path must not be regressed."""
        assert "tcg.backtester.lib" in _CLAUDE_MD_CONTENT, (
            "Round-4 invariant: 'tcg.backtester.lib' canonical import path "
            "must remain in the prompt. Do not regress to tcg_backtester or lib.data_load."
        )

    def test_network_sandbox_warning_is_blockquote(self) -> None:
        """The network-sandbox warning must use markdown blockquote prominence.

        Closes R-prompt-behavior gap T4: the warning's value depends partly on visual
        prominence (rendered as a callout, not a plain paragraph). A future edit that
        collapses the blockquote to a regular paragraph would weaken the cue without
        breaking any other test. This test pins the literal '> **Network sandbox
        warning' blockquote prefix so any demotion must be deliberate.
        """
        # Use raw (non-lowercased, non-normalised) content so the blockquote marker
        # and bold markdown survive intact.
        assert "> **Network sandbox warning" in _CLAUDE_MD_CONTENT, (
            "The network-sandbox warning must remain a markdown blockquote prefixed "
            "with '> **Network sandbox warning' for visual prominence. Do not demote "
            "it to a plain paragraph."
        )


# ---------------------------------------------------------------------------
# Round-6 follow-ups — S1 deferred-completion carve-out, S2 cache recipe,
# S3 polling protocol, NIT N3 action-honesty scope clarification.
# ---------------------------------------------------------------------------


class TestDeferredCompletionCarveOut:
    """Round-6 S1: the 'Never end a turn while background' rule must include a
    carve-out for explicitly user-okayed deferred work."""

    def test_deferred_completion_exception_named(self) -> None:
        """The rule must label the carve-out as a 'deferred completion' exception."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "deferred completion" in lower, (
            "The 'never end a turn' rule must name a 'deferred completion' "
            "exception so the agent can identify the legitimate-end case by name."
        )

    def test_carve_out_requires_explicit_user_acknowledgement(self) -> None:
        """The carve-out must require explicit user acknowledgement, not be implicit."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "deferred completion"
        idx = normalised.find(anchor)
        assert idx != -1, "Carve-out anchor 'deferred completion' missing"
        window = normalised[idx : idx + 500]
        # Must require the user to have explicitly accepted/asked for deferred work.
        assert "explicitly" in window, (
            "The deferred-completion carve-out must require the user to have "
            f"*explicitly* accepted deferred follow-up. Window: {window!r}"
        )
        # Must list at least one of the canonical example phrases so the agent
        # recognises the trigger from natural-language requests.
        examples = ("report back later", "overnight", "check tomorrow", "babysit")
        present = [ex for ex in examples if ex in window]
        assert present, (
            "The carve-out must give at least one canonical example phrase "
            f"(e.g. 'report back later', 'overnight', 'check tomorrow', 'babysit'). "
            f"Window: {window!r}"
        )

    def test_carve_out_requires_closing_artefacts(self) -> None:
        """The carve-out must require the closing message to name path + job id + duration."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "deferred completion"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 500]
        # The closing message must name the .output path, the PID/background_task_id, and
        # the expected duration. We require all three so a future edit dropping any of
        # them is a deliberate decision.
        assert ".output" in window, (
            f"Carve-out closing-message contract must name the '.output' path. Window: {window!r}"
        )
        assert "pid" in window or "background_task_id" in window, (
            "Carve-out closing-message contract must name the job's PID or "
            f"background_task_id. Window: {window!r}"
        )
        assert "duration" in window, (
            f"Carve-out closing-message contract must name the expected duration. Window: {window!r}"
        )

    def test_carve_out_default_is_polling(self) -> None:
        """Without an explicit user okay, the default must remain polling."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "deferred completion"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 500]
        assert "default to polling" in window, (
            "The carve-out must explicitly state that the default (no user acknowledgement) "
            f"is to poll, not to end-turn. Window: {window!r}"
        )


class TestPollingProtocol:
    """Round-6 S3: the polling protocol must specify cadence, terminal-line definition,
    and a max poll budget."""

    def test_polling_protocol_label_present(self) -> None:
        """A 'Polling protocol' label must anchor the new instructions."""
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "polling protocol" in lower, (
            "The prompt must contain a 'Polling protocol' label so the agent can "
            "locate the cadence + terminal-line + budget guidance by name."
        )

    def test_polling_cadence_specified(self) -> None:
        """Cadence must be specified — ~10 s between Reads, with sleep guidance."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "polling protocol"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 800]
        assert "10 s" in window or "10s" in window or "every ~10" in window, (
            "Polling protocol must specify a concrete cadence (~10 s between Reads). "
            f"Window: {window!r}"
        )
        assert "sleep" in window, (
            "Polling protocol must mention `sleep` (Bash sleep between Reads) so the "
            f"agent does not Read in a tight loop. Window: {window!r}"
        )

    def test_terminal_line_definition_complete(self) -> None:
        """Terminal-line definition must enumerate success / exception / stable-file cases."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "polling protocol"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 1200]
        # All three terminal-line cases must be named in the protocol so a clean-exit
        # script with no DONE marker is not ambiguous.
        assert "success" in window, (
            f"Terminal-line definition must mention the 'success' case. Window: {window!r}"
        )
        assert "exception" in window or "traceback" in window, (
            "Terminal-line definition must mention the 'exception' / 'traceback' case. "
            f"Window: {window!r}"
        )
        # Stable-file proxy for clean exit without explicit DONE.
        assert "stable" in window or "identical" in window, (
            "Terminal-line definition must include a stable-file / identical-content "
            f"proxy for a clean exit with no explicit DONE marker. Window: {window!r}"
        )

    def test_max_poll_budget_specified(self) -> None:
        """A maximum poll budget (~10 minutes) must be specified."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "polling protocol"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 1200]
        assert "max poll budget" in window or "max poll" in window, (
            "Polling protocol must label a 'max poll budget' so the agent stops "
            f"busy-polling after a bounded time. Window: {window!r}"
        )
        assert "10 minutes" in window, (
            f"Polling protocol must specify a concrete '10 minutes' budget. Window: {window!r}"
        )

    def test_done_marker_pattern_recommended(self) -> None:
        """Scripts should be patterned to print a DONE marker for deterministic polling."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "polling protocol"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 1200]
        assert "done" in window, (
            "Polling protocol must recommend printing a `DONE` marker on the last line "
            f"so polling is deterministic. Window: {window!r}"
        )


class TestSandboxCacheRecipe:
    """Round-6 S2: the network-sandbox warning must include a concrete cache recipe."""

    def test_cache_recipe_data_folder(self) -> None:
        """The recipe must name `data/` as the cache folder."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "concrete recipe"
        idx = normalised.find(anchor)
        assert idx != -1, "Could not locate 'Concrete recipe' anchor under the warning"
        window = normalised[idx : idx + 800]
        assert "data/" in window, (
            "Cache recipe must name the `data/` workspace subfolder as the canonical "
            f"location for cached MCP exports. Window: {window!r}"
        )

    def test_cache_recipe_jsonl_format(self) -> None:
        """The recipe must specify JSON-lines (`jsonl`) as the cache format."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "concrete recipe"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 800]
        assert "jsonl" in window, (
            "Cache recipe must specify JSON-lines (`.jsonl`) as the cache format so "
            f"the agent does not reinvent the schema each turn. Window: {window!r}"
        )

    def test_cache_recipe_names_aggregate_tool(self) -> None:
        """The recipe must name `mcp__mongodb__aggregate` as the canonical fetch tool."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "concrete recipe"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 800]
        # mcp__mongodb__aggregate is the canonical chunked-fetch tool (verified against
        # the mongodb-mcp-server README — find/aggregate/export are the three Database
        # Tools relevant for read access). aggregate is the most powerful within the
        # MCP byte limit; export is named separately for very large dumps.
        assert "mcp__mongodb__aggregate" in window, (
            "Cache recipe must name `mcp__mongodb__aggregate` as the canonical fetch "
            f"tool for chunked queries within the MCP byte limit. Window: {window!r}"
        )

    def test_cache_recipe_names_export_for_large_dumps(self) -> None:
        """The recipe must name `mcp__mongodb__export` for large server-side dumps."""
        lower = _CLAUDE_MD_CONTENT.lower()
        # export is the right tool for very large dumps — verified by the mongodb-mcp-server
        # README ("Export a query or aggregation results in the specified EJSON format")
        # and by the Wave-D probe trace which used mcp__mongodb__export at event 42.
        assert "mcp__mongodb__export" in lower, (
            "The network-sandbox section must name `mcp__mongodb__export` as the tool "
            "for very large server-side dumps that exceed the find/aggregate byte limit."
        )


class TestActionHonestyScope:
    """Round-6 NIT N3: action-honesty rule must clarify it applies to single-step
    in-message announcements, not multi-turn plans / explanations."""

    def test_action_honesty_scope_clause_present(self) -> None:
        """A scope clause must clarify that multi-turn plans are not violations."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "action honesty"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 1000]
        assert "scope" in window, (
            "Action-honesty rule must contain a 'Scope' clause to bound the rule's "
            f"reach (so it does not over-trip on planning paragraphs). Window: {window!r}"
        )
        # The scope clause must explicitly carve out at least one of: multi-turn plans,
        # recaps, or explanations.
        carve_outs = ("multi-turn plan", "recap", "explanation")
        present = [c for c in carve_outs if c in window]
        assert present, (
            "Scope clause must carve out multi-turn plans / recaps / explanations. "
            f"Window: {window!r}"
        )
