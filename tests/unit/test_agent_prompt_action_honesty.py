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
        """The action-honesty rule must name concrete future-tense trigger phrases."""
        normalised = _normalise(_CLAUDE_MD_CONTENT.lower())
        anchor = "action honesty"
        idx = normalised.find(anchor)
        assert idx != -1
        window = normalised[idx : idx + 400]
        # Must name at least one of the trigger phrases from the field reports
        triggers = ("i'll", "let me", "now the")
        assert any(t in window for t in triggers), (
            "Action honesty rule must name at least one future-tense trigger phrase "
            f"('I'll', 'Let me', 'Now the'). Window: {window!r}"
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
        """The rule must give an example of a non-turn-ending sentence."""
        # Checks for "not turn-ending" / "not a turn-ending sentence" pattern
        lower = _CLAUDE_MD_CONTENT.lower()
        assert "not turn-ending" in lower or "not a turn-ending" in lower or (
            "backtest is running" in lower and "not" in lower
        ), (
            "The 'never end a turn' rule should give an example of a sentence "
            "that is NOT a valid turn-ending message (e.g. 'Backtest is running')."
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
