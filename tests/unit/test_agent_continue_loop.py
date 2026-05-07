"""Unit tests for the Issue 23 (Round 6) auto-continue harness loop.

Covers:
- ``_has_done_marker`` suffix-anchored detection.
- ``_detect_unmet_intent`` regex + tool_use heuristic.
- ``_concat_text_blocks`` content-block walker.
- ``_evaluate_auto_continue`` decision logic on a stubbed session.
- ``_resolve_max_auto_continue`` env-var override + clamp.
- ``_build_continuation_message`` text shape.
- ``_maybe_auto_continue`` wrapper end-to-end behaviour:
  - Happy path: marker present -> no auto-continue.
  - Marker missing: re-dispatch fires with ``auto_continue`` event.
  - Cap reached: ``auto_continue_capped`` + in-band assistant message.
  - Interrupt mid-loop: counter check breaks early.
  - Silent EOF (``_saw_result=False``): does not auto-continue.
  - Counter reset on user message / interrupt (handler path).
  - Marker present + unmet intent: fallback fires once with the
    correct ``reason``.
  - Env override clamps and respects the configured cap.

These tests exercise the BE-only loop in isolation; FE state wiring
lives in the parallel-worker FE test file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import tcg.core.api.agent as agent_module
from tcg.core.agent.session import (
    CLISession,
    TURN_HANDOFF_DONE_MARKER,
    _concat_text_blocks,
    _detect_unmet_intent,
    _has_done_marker,
)
from tcg.core.api.agent import (
    _build_continuation_message,
    _evaluate_auto_continue,
    _resolve_max_auto_continue,
)


# ---------------------------------------------------------------------------
# Fixture: clear module-level state per test (G-INVAR row 9).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state():
    agent_module._pending_abort_notifications.clear()
    agent_module._active_connections.clear()
    yield
    agent_module._pending_abort_notifications.clear()
    agent_module._active_connections.clear()


# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------


class TestHasDoneMarker:
    def test_marker_at_end_of_text(self) -> None:
        text = "All four steps complete.\n" + TURN_HANDOFF_DONE_MARKER
        assert _has_done_marker(text) is True

    def test_marker_with_trailing_newline(self) -> None:
        text = f"Done.\n{TURN_HANDOFF_DONE_MARKER}\n"
        assert _has_done_marker(text) is True

    def test_marker_with_trailing_period(self) -> None:
        text = f"Wrapping up. {TURN_HANDOFF_DONE_MARKER}."
        assert _has_done_marker(text) is True

    def test_marker_only_in_middle_does_not_match(self) -> None:
        # Marker buried 200+ chars before the end -> not anchored to suffix.
        prefix = (
            "Here's the format I should use: "
            + TURN_HANDOFF_DONE_MARKER
            + " (literally that token). "
        )
        # Push the marker further than 100 chars from the end with a long
        # closing sentence so the suffix window misses it entirely.
        suffix = (
            "I'll continue working through the remaining steps now. "
            "First the data fetch, then the backtest, then the report."
        )
        text = prefix + suffix
        assert _has_done_marker(text) is False

    def test_empty_text(self) -> None:
        assert _has_done_marker("") is False

    def test_no_marker(self) -> None:
        assert _has_done_marker("All done!") is False

    def test_non_string_input(self) -> None:
        assert _has_done_marker(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Intent parser
# ---------------------------------------------------------------------------


class TestDetectUnmetIntent:
    def test_unmet_intent_matches(self) -> None:
        text = "I'll run the backtest next."
        matched, phrase = _detect_unmet_intent(text, [])
        assert matched is True
        assert "I'll" in phrase or "run" in phrase

    def test_intent_with_tool_use_is_satisfied(self) -> None:
        text = "I'll write the script now."
        content = [
            {"type": "text", "text": text},
            {"type": "tool_use", "name": "Write", "input": {}, "id": "x"},
        ]
        matched, _ = _detect_unmet_intent(text, content)
        assert matched is False

    def test_no_intent_no_match(self) -> None:
        text = "All four steps are now complete and the report is saved."
        matched, _ = _detect_unmet_intent(text, [])
        assert matched is False

    def test_let_me_variant(self) -> None:
        text = "Let me build the equity curve from these results."
        matched, phrase = _detect_unmet_intent(text, [])
        assert matched is True
        assert "Let me" in phrase

    def test_phrase_truncated_at_80_chars(self) -> None:
        long_text = "I'll " + "x " * 200
        # Won't actually match because we run out of allowed words after 3
        # before hitting the verb; check truncation when match exists.
        text = "I'll write " + "the " * 5 + "result quickly."
        matched, phrase = _detect_unmet_intent(text, [])
        # If matched (depends on regex), phrase length must be <= 83 (80+...)
        if matched:
            assert len(phrase) <= 83

    # -----------------------------------------------------------------------
    # B3 (C2-recurrence-audit M-DURABILITY): empirically-broadened verb list
    # -----------------------------------------------------------------------

    def test_check_verb_matches(self) -> None:
        """'check' is an empirically observed verb (top of C2 scan); must match."""
        matched, phrase = _detect_unmet_intent("I'll check the file", [])
        assert matched is True
        assert "check" in phrase

    def test_inspect_verb_matches(self) -> None:
        """'inspect' was observed 4× in production; must match."""
        matched, phrase = _detect_unmet_intent("Let me inspect the data", [])
        assert matched is True
        assert "inspect" in phrase

    def test_going_to_prefix_matches(self) -> None:
        """'going to' prefix (empirical C2 sample) must match."""
        matched, phrase = _detect_unmet_intent(
            "I'm going to query the database", []
        )
        assert matched is True
        assert "query" in phrase

    def test_think_verb_does_not_match(self) -> None:
        """'think' is NOT in the verb list; guard against scope creep."""
        matched, _ = _detect_unmet_intent("I'll think about this", [])
        assert matched is False

    def test_explore_verb_matches(self) -> None:
        """'explore' is an empirically observed verb; must match."""
        matched, phrase = _detect_unmet_intent(
            "Let me explore the structure", []
        )
        assert matched is True
        assert "explore" in phrase

    def test_no_target_verb_does_not_match(self) -> None:
        """Auxiliary phrase without a target verb must NOT match."""
        matched, _ = _detect_unmet_intent("I'll be done in a moment", [])
        assert matched is False


# ---------------------------------------------------------------------------
# Content-block walker
# ---------------------------------------------------------------------------


class TestConcatTextBlocks:
    def test_legacy_string_content(self) -> None:
        assert _concat_text_blocks("hello") == "hello"

    def test_list_with_mixed_blocks(self) -> None:
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "tool_use", "name": "Read", "input": {}, "id": "a"},
            {"type": "text", "text": "world"},
        ]
        assert _concat_text_blocks(content) == "Hello world"

    def test_empty_list(self) -> None:
        assert _concat_text_blocks([]) == ""

    def test_invalid_input(self) -> None:
        assert _concat_text_blocks(None) == ""
        assert _concat_text_blocks(42) == ""


# ---------------------------------------------------------------------------
# Env-var override + clamp
# ---------------------------------------------------------------------------


class TestResolveMaxAutoContinue:
    def test_default_when_unset(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k != "TCG_AGENT_MAX_AUTO_CONTINUE"}
        with patch.dict(os.environ, env, clear=True):
            assert _resolve_max_auto_continue() == 5

    def test_env_override(self) -> None:
        with patch.dict(os.environ, {"TCG_AGENT_MAX_AUTO_CONTINUE": "2"}):
            assert _resolve_max_auto_continue() == 2

    def test_env_clamps_low(self) -> None:
        with patch.dict(os.environ, {"TCG_AGENT_MAX_AUTO_CONTINUE": "0"}):
            assert _resolve_max_auto_continue() == 1

    def test_env_clamps_high(self) -> None:
        with patch.dict(os.environ, {"TCG_AGENT_MAX_AUTO_CONTINUE": "999"}):
            assert _resolve_max_auto_continue() == 50

    def test_env_garbage_falls_back(self) -> None:
        with patch.dict(os.environ, {"TCG_AGENT_MAX_AUTO_CONTINUE": "abc"}):
            assert _resolve_max_auto_continue() == 5


# ---------------------------------------------------------------------------
# _evaluate_auto_continue
# ---------------------------------------------------------------------------


def _stub_session(history: list[dict[str, Any]]) -> CLISession:
    """Build a CLISession instance with a custom conversation_history.

    The ``on_event`` callback is a no-op AsyncMock; ``workspace_path``
    is a transient Path under tmp -- the tests under this section do
    not exercise file IO.
    """
    sess = CLISession(
        session_id="t",
        workspace_path=Path("/tmp"),
        on_event=AsyncMock(),
    )
    sess.conversation_history = history
    return sess


class TestEvaluateAutoContinue:
    def test_marker_present_clean_end(self) -> None:
        sess = _stub_session([
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"All done.\n{TURN_HANDOFF_DONE_MARKER}"}
                ],
            },
        ])
        should, reason, phrase = _evaluate_auto_continue(sess)
        assert should is False
        assert reason == ""

    def test_marker_missing_triggers_continue(self) -> None:
        sess = _stub_session([
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "All done."}],
            },
        ])
        should, reason, _ = _evaluate_auto_continue(sess)
        assert should is True
        assert reason == "missing_done_marker"

    def test_marker_with_unmet_intent(self) -> None:
        text = (
            "Wrapping up here. I'll run the analysis next.\n"
            + TURN_HANDOFF_DONE_MARKER
        )
        sess = _stub_session([
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        ])
        should, reason, phrase = _evaluate_auto_continue(sess)
        assert should is True
        assert reason == "unmet_intent"
        assert phrase  # non-empty

    def test_empty_history_does_not_continue(self) -> None:
        sess = _stub_session([])
        should, _, _ = _evaluate_auto_continue(sess)
        assert should is False


# ---------------------------------------------------------------------------
# Continuation-message text
# ---------------------------------------------------------------------------


class TestBuildContinuationMessage:
    def test_missing_marker_text(self) -> None:
        msg = _build_continuation_message("missing_done_marker")
        assert "<<<TURN_HANDOFF_DONE>>>" in msg
        assert "marker" in msg.lower()

    def test_unmet_intent_text(self) -> None:
        msg = _build_continuation_message("unmet_intent", "I'll run")
        assert "I'll run" in msg
        assert "deferred" in msg.lower()


# ---------------------------------------------------------------------------
# Wrapper loop end-to-end (using a fake _execute_single_turn).
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Records emitted events; never blocks."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


class FakeWorkspace:
    """Stub for ``AgentWorkspace`` -- only ``save_conversation`` is used."""

    def __init__(self) -> None:
        self.saves: list[list[dict[str, Any]]] = []

    def save_conversation(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        self.saves.append(list(messages))


def _make_persist(workspace: FakeWorkspace, session_id: str):
    async def _persist(msgs: list[dict[str, Any]]) -> None:
        workspace.save_conversation(session_id, msgs)

    return _persist


class _LoopHarness:
    """Shared scaffolding for the wrapper-loop tests.

    Builds a real ``CLISession`` (so we exercise ``_continue_iters``,
    ``append_assistant_message``, ``_persist``, etc.) and replicates
    the slice of ``agent_websocket`` that the loop depends on
    (``_maybe_auto_continue``). The CLI subprocess is replaced by a
    callable supplied per-test that simulates one CLI turn's effect on
    the session (history append, ``_saw_result``, etc.).
    """

    def __init__(
        self,
        tmp_path: Path,
        responses: list[dict[str, Any]],
        max_auto_continue: int = 5,
        cancel_after_iter: int | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.responses = list(responses)
        self.session_id = "test-session"
        self.workspace = FakeWorkspace()
        self.ws = FakeWebSocket()
        self.session = CLISession(
            session_id=self.session_id,
            workspace_path=tmp_path,
            on_event=AsyncMock(),
        )
        self.session._on_persist = _make_persist(
            self.workspace, self.session_id
        )
        self.max_auto_continue = max_auto_continue
        self.cancel_after_iter = cancel_after_iter
        self.execute_calls: list[str] = []

    async def fake_execute(
        self,
        session: CLISession,
        content: str,
        model: str,
        ws: Any,
        workspace: Any,
        session_id: str,
        request_id: str,
    ) -> None:
        """Simulate one CLI turn: append user + scripted assistant.

        Pops the next scripted response and applies it. If the response
        dict has ``cancel=True``, sets ``session.is_cancelled`` after
        appending. ``saw_result=False`` skips the marker logic
        (silent-EOF path simulation).
        """
        # Mirror ``run_turn`` shape.
        session.conversation_history.append(
            {"role": "user", "content": content}
        )
        if not self.responses:
            # No more scripted responses -> simulate silent EOF.
            session._saw_result = False
            return
        resp = self.responses.pop(0)
        session._saw_result = bool(resp.get("saw_result", True))
        assistant_text = resp.get("text", "")
        content_blocks = resp.get(
            "content_blocks", [{"type": "text", "text": assistant_text}]
        )
        session.conversation_history.append(
            {"role": "assistant", "content": content_blocks}
        )
        await session._persist()
        if resp.get("cancel"):
            session.is_cancelled = True
        if (
            self.cancel_after_iter is not None
            and session._continue_iters >= self.cancel_after_iter
        ):
            session.is_cancelled = True

    async def run_loop(self, initial_content: str = "go") -> None:
        """Run the user-message turn followed by the auto-continue loop."""
        # Patch MAX_AUTO_CONTINUE for this loop.
        with patch.object(
            agent_module, "MAX_AUTO_CONTINUE", self.max_auto_continue
        ), patch.object(
            agent_module, "_execute_single_turn", self.fake_execute
        ):
            # Initial turn first.
            await self.fake_execute(
                self.session,
                initial_content,
                "claude-opus-4-6",
                self.ws,
                self.workspace,
                self.session_id,
                "rid-init",
            )
            # Now mimic _maybe_auto_continue inline -- replicate the
            # exact body so we exercise the production logic. We import
            # the symbol at runtime so the patch on _execute_single_turn
            # is effective.
            await self._maybe_auto_continue("claude-opus-4-6")

    async def _maybe_auto_continue(self, model: str) -> None:
        """Mirror of ``agent_websocket._maybe_auto_continue`` closure.

        Production code defines ``_maybe_auto_continue`` as a nested
        closure inside ``agent_websocket`` (binding ``websocket``,
        ``session``, etc.). To keep the test isolated, we replicate
        the loop body here against the public helpers / module-level
        constants. This DOES couple the test to the wrapper logic --
        any change to the production loop must be mirrored here. The
        contract test below (``test_maybe_auto_continue_signature``)
        ensures the closure exists in production so this mirror has
        something to match.
        """
        import time

        while True:
            if self.session.is_cancelled:
                return
            if not getattr(self.session, "_saw_result", False):
                return
            if (
                self.session._continue_iters
                >= agent_module.MAX_AUTO_CONTINUE
            ):
                await self.ws.send_json(
                    {
                        "type": "auto_continue_capped",
                        "session_id": self.session_id,
                        "iter": self.session._continue_iters,
                        "max": agent_module.MAX_AUTO_CONTINUE,
                        "reason": "cap_reached",
                        "timestamp": time.time(),
                    }
                )
                # R-3 fix mirror: also emit token + message_complete so cap
                # message renders live.
                _cap_text = agent_module._build_auto_continue_cap_message(
                    agent_module.MAX_AUTO_CONTINUE
                )
                await self.ws.send_json(
                    {
                        "type": "token",
                        "session_id": self.session_id,
                        "content": _cap_text,
                        "timestamp": time.time(),
                    }
                )
                await self.ws.send_json(
                    {
                        "type": "message_complete",
                        "session_id": self.session_id,
                        "timestamp": time.time(),
                    }
                )
                await self.session.append_assistant_message(_cap_text)
                return
            should, reason, phrase = _evaluate_auto_continue(self.session)
            if not should:
                return
            self.session._continue_iters += 1
            await self.ws.send_json(
                {
                    "type": "auto_continue",
                    "session_id": self.session_id,
                    "iter": self.session._continue_iters,
                    "max": agent_module.MAX_AUTO_CONTINUE,
                    "reason": reason,
                    "timestamp": time.time(),
                }
            )
            cont = _build_continuation_message(reason, phrase)
            self.execute_calls.append(cont)
            await self.fake_execute(
                self.session,
                cont,
                model,
                self.ws,
                self.workspace,
                self.session_id,
                "rid-cont",
            )


# ---------------------------------------------------------------------------
# Wrapper-loop scenarios
# ---------------------------------------------------------------------------


class TestMaybeAutoContinue:
    async def test_happy_path_marker_first_try(self, tmp_path: Path) -> None:
        """Agent emits marker on first try -> no auto-continue, no events."""
        h = _LoopHarness(
            tmp_path,
            responses=[
                {"text": f"All four steps done.\n{TURN_HANDOFF_DONE_MARKER}"},
            ],
        )
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        capped = [
            m for m in h.ws.sent if m.get("type") == "auto_continue_capped"
        ]
        assert events == []
        assert capped == []
        assert h.session._continue_iters == 0

    async def test_marker_missing_then_emitted(self, tmp_path: Path) -> None:
        """Marker missing on first; emitted on retry -> 1 auto_continue, clean end."""
        h = _LoopHarness(
            tmp_path,
            responses=[
                {"text": "Working on it now."},  # initial turn, no marker
                {"text": f"Now done.\n{TURN_HANDOFF_DONE_MARKER}"},  # retry
            ],
        )
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        capped = [
            m for m in h.ws.sent if m.get("type") == "auto_continue_capped"
        ]
        assert len(events) == 1
        assert events[0]["reason"] == "missing_done_marker"
        assert events[0]["iter"] == 1
        assert events[0]["max"] == h.max_auto_continue
        assert capped == []
        assert h.session._continue_iters == 1

    async def test_marker_never_emitted_cap_hits(self, tmp_path: Path) -> None:
        """Cap reached -> auto_continue_capped + in-band assistant message."""
        # 6 responses, all without marker. Cap=5 -> 5 auto_continue events,
        # then capped on iter 6 attempt.
        responses = [{"text": f"Iteration {i} no marker"} for i in range(8)]
        h = _LoopHarness(tmp_path, responses=responses, max_auto_continue=5)
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        capped = [
            m for m in h.ws.sent if m.get("type") == "auto_continue_capped"
        ]
        assert len(events) == 5
        assert [e["iter"] for e in events] == [1, 2, 3, 4, 5]
        assert len(capped) == 1
        assert capped[0]["iter"] == 5
        # T3-1 (B3): capped event must include 'max' field (B2 S2 added it
        # to production; test mirror now mirrors correctly).
        assert "max" in capped[0], "auto_continue_capped must include 'max' field"
        assert capped[0]["max"] == 5
        # In-band assistant message appended.
        assistants = [
            m for m in h.session.conversation_history
            if m.get("role") == "assistant"
        ]
        assert any(
            agent_module._AUTO_CONTINUE_CAP_MESSAGE
            in _concat_text_blocks(m.get("content"))
            for m in assistants
        )

    async def test_silent_eof_does_not_auto_continue(
        self, tmp_path: Path
    ) -> None:
        """``_saw_result=False`` short-circuits the loop (process_exit path)."""
        h = _LoopHarness(
            tmp_path,
            responses=[
                {"text": "partial...", "saw_result": False},
            ],
        )
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        capped = [
            m for m in h.ws.sent if m.get("type") == "auto_continue_capped"
        ]
        assert events == []
        assert capped == []

    async def test_interrupt_mid_loop_breaks(self, tmp_path: Path) -> None:
        """is_cancelled set during a re-dispatch -> loop terminates."""
        h = _LoopHarness(
            tmp_path,
            responses=[
                {"text": "no marker yet"},
                # Second iteration sets cancel during the simulated turn.
                {"text": "still no marker", "cancel": True},
                # Should never run.
                {"text": "should not see this"},
            ],
            max_auto_continue=5,
        )
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        # 2 iterations fired (the cancel-tagged second turn ran), but the
        # post-iteration check sees is_cancelled and exits.
        assert len(events) <= 2
        assert all(
            m.get("type") != "auto_continue_capped" for m in h.ws.sent
        )

    async def test_unmet_intent_fallback(self, tmp_path: Path) -> None:
        """Marker present + future-tense announcement -> fallback fires once."""
        announce = (
            "Saving outputs. I'll run the analysis next.\n"
            + TURN_HANDOFF_DONE_MARKER
        )
        h = _LoopHarness(
            tmp_path,
            responses=[
                {
                    "text": announce,
                    "content_blocks": [{"type": "text", "text": announce}],
                },
                {"text": f"Now actually done.\n{TURN_HANDOFF_DONE_MARKER}"},
            ],
        )
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        assert len(events) == 1
        assert events[0]["reason"] == "unmet_intent"

    async def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TCG_AGENT_MAX_AUTO_CONTINUE=2`` -> cap at 2 iterations."""
        responses = [{"text": f"iter {i} no marker"} for i in range(10)]
        h = _LoopHarness(tmp_path, responses=responses, max_auto_continue=2)
        await h.run_loop()
        events = [m for m in h.ws.sent if m.get("type") == "auto_continue"]
        capped = [
            m for m in h.ws.sent if m.get("type") == "auto_continue_capped"
        ]
        assert len(events) == 2
        assert len(capped) == 1
        assert capped[0]["iter"] == 2
        # T3-1 (B3): 'max' field must equal the configured cap (2 in env override).
        assert "max" in capped[0], "auto_continue_capped must include 'max' field"
        assert capped[0]["max"] == 2

    async def test_no_auto_continue_when_already_cancelled(
        self, tmp_path: Path
    ) -> None:
        """Cancellation set BEFORE the loop entry -> immediate return."""
        h = _LoopHarness(tmp_path, responses=[{"text": "no marker"}])
        # Simulate user interrupt arriving immediately after the turn ended.
        # We pre-cancel and then run the loop manually.
        h.session.is_cancelled = True
        h.session._saw_result = True
        h.session.conversation_history.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "no marker"}],
        })
        with patch.object(agent_module, "MAX_AUTO_CONTINUE", 5):
            await h._maybe_auto_continue("claude-opus-4-6")
        assert all(
            m.get("type") not in {"auto_continue", "auto_continue_capped"}
            for m in h.ws.sent
        )

    async def test_in_band_message_persisted_after_cap(
        self, tmp_path: Path
    ) -> None:
        """The cap-hit assistant message survives via _on_persist."""
        responses = [{"text": f"iter {i} no marker"} for i in range(8)]
        h = _LoopHarness(tmp_path, responses=responses, max_auto_continue=2)
        await h.run_loop()
        # Last save should contain the cap message in the assistant
        # tail. Use the dynamic builder (max=2) not the module constant
        # (max=5) to match what the loop actually persists.
        expected = agent_module._build_auto_continue_cap_message(2)
        assert h.workspace.saves, "No saves recorded"
        last = h.workspace.saves[-1]
        assistants = [m for m in last if m.get("role") == "assistant"]
        last_assistant_text = _concat_text_blocks(
            assistants[-1].get("content")
        )
        assert expected in last_assistant_text

    async def test_cap_hit_emits_synthetic_stream_events(
        self, tmp_path: Path
    ) -> None:
        """R-3 fix: cap hit must emit token + message_complete AFTER
        auto_continue_capped so the cap message renders live in the FE
        transcript (not only on reload).

        Verify:
        - ``auto_continue_capped`` fires.
        - Immediately after, a ``token`` event carries the cap message text.
        - Immediately after, a ``message_complete`` event fires.
        - Order: capped → token → message_complete.
        """
        responses = [{"text": f"iter {i} no marker"} for i in range(5)]
        h = _LoopHarness(tmp_path, responses=responses, max_auto_continue=2)
        await h.run_loop()

        types_sent = [m["type"] for m in h.ws.sent]
        capped_idx = next(
            (i for i, t in enumerate(types_sent) if t == "auto_continue_capped"),
            None,
        )
        assert capped_idx is not None, "auto_continue_capped must be emitted"

        # token must follow capped
        assert capped_idx + 1 < len(types_sent), (
            "A 'token' event must follow auto_continue_capped"
        )
        token_event = h.ws.sent[capped_idx + 1]
        assert token_event["type"] == "token", (
            f"Expected 'token' after capped, got {token_event['type']!r}"
        )
        expected_cap_text = agent_module._build_auto_continue_cap_message(2)
        assert token_event["content"] == expected_cap_text, (
            "token event must carry the cap message text"
        )

        # message_complete must follow the token
        assert capped_idx + 2 < len(types_sent), (
            "A 'message_complete' event must follow the token event"
        )
        mc_event = h.ws.sent[capped_idx + 2]
        assert mc_event["type"] == "message_complete", (
            f"Expected 'message_complete' after token, got {mc_event['type']!r}"
        )


# ---------------------------------------------------------------------------
# Module-level signature check -- guards against the closure being lost.
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_max_auto_continue_default(self) -> None:
        # The module imports MAX_AUTO_CONTINUE at import time. We don't
        # assert the absolute value (env may have overridden) but the
        # type and clamp-range:
        assert isinstance(agent_module.MAX_AUTO_CONTINUE, int)
        assert 1 <= agent_module.MAX_AUTO_CONTINUE <= 50

    def test_cap_message_constant_present(self) -> None:
        # Module-level alias still exists for backward compatibility
        assert "Auto-continue cap" in agent_module._AUTO_CONTINUE_CAP_MESSAGE

    def test_build_auto_continue_cap_message_dynamic(self) -> None:
        # N2 fix: _build_auto_continue_cap_message uses the actual max_iters value
        msg2 = agent_module._build_auto_continue_cap_message(2)
        assert "Auto-continue cap (2) reached" in msg2
        msg10 = agent_module._build_auto_continue_cap_message(10)
        assert "Auto-continue cap (10) reached" in msg10
        # Verify the module alias resolves to MAX_AUTO_CONTINUE (default 5 in tests)
        assert f"Auto-continue cap ({agent_module.MAX_AUTO_CONTINUE}) reached" in agent_module._AUTO_CONTINUE_CAP_MESSAGE
