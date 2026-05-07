import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import useAgentSession, { formatTokens, formatElapsed, stripDoneMarker } from './useAgentSession';

/* ---------- Minimal WebSocket mock ---------- */

class MockWebSocket {
  static OPEN = 1;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this._listeners = {};
    this.sentMessages = [];
    MockWebSocket.instances.push(this);
  }

  addEventListener(type, fn) {
    (this._listeners[type] ??= []).push(fn);
  }

  removeEventListener(type, fn) {
    const list = this._listeners[type];
    if (list) {
      this._listeners[type] = list.filter((f) => f !== fn);
    }
  }

  send(data) {
    this.sentMessages.push(data);
  }

  close() {
    this.readyState = 3; // CLOSED
  }

  // Test helpers
  _emit(type, data) {
    for (const fn of this._listeners[type] ?? []) fn(data);
  }

  _simulateOpen() {
    this._emit('open', {});
  }

  _simulateMessage(payload) {
    this._emit('message', { data: JSON.stringify(payload) });
  }

  _simulateClose(code = 1006) {
    this._emit('close', { code });
  }
}

/* ---------- Tests ---------- */

describe('useAgentSession', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('does not connect when sessionId is null', () => {
    renderHook(() => useAgentSession(null));
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('connects and sets isConnected on open', () => {
    const { result } = renderHook(() => useAgentSession('sess-1'));
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.isConnected).toBe(false);

    act(() => MockWebSocket.instances[0]._simulateOpen());
    expect(result.current.isConnected).toBe(true);
  });

  it('builds the correct WS URL', () => {
    renderHook(() => useAgentSession('abc'));
    expect(MockWebSocket.instances[0].url).toContain('/ws/agent/abc');
  });

  it('handles token + message_complete flow', () => {
    const { result } = renderHook(() => useAgentSession('sess-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'token', content: 'Hello' }));
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe('Hello');
    expect(result.current.messages[0].streaming).toBe(true);

    act(() => ws._simulateMessage({ type: 'token', content: ' world' }));
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe('Hello world');

    act(() => ws._simulateMessage({ type: 'message_complete' }));
    expect(result.current.messages[0].streaming).toBe(false);
  });

  it('handles assumptions_update', () => {
    const { result } = renderHook(() => useAgentSession('sess-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'assumptions_update',
        assumptions: ['use production db'],
      }),
    );
    expect(result.current.assumptions).toEqual(['use production db']);
  });

  it('handles status updates', () => {
    const { result } = renderHook(() => useAgentSession('sess-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'thinking' }));
    expect(result.current.status).toBe('thinking');
  });

  it('handles notebook_ready', () => {
    const { result } = renderHook(() => useAgentSession('sess-5'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());
    expect(result.current.notebookReady).toBe(false);

    act(() => ws._simulateMessage({ type: 'notebook_ready' }));
    expect(result.current.notebookReady).toBe(true);
  });

  it('handles tool_call', () => {
    const { result } = renderHook(() => useAgentSession('sess-6'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'tool_call',
        name: 'run_query',
        input: { query: '{}' },
      }),
    );
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]).toEqual({
      role: 'tool',
      name: 'run_query',
      input: { query: '{}' },
    });
  });

  it('handles error messages', () => {
    const { result } = renderHook(() => useAgentSession('sess-7'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({ type: 'error', message: 'Something failed' }),
    );
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]).toEqual({
      role: 'error',
      content: 'Something failed',
    });
  });

  it('sendMessage sends JSON via WebSocket', () => {
    const { result } = renderHook(() => useAgentSession('sess-8'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => result.current.sendMessage('show collections'));
    expect(ws.sentMessages).toHaveLength(1);
    expect(JSON.parse(ws.sentMessages[0])).toEqual({
      type: 'message',
      content: 'show collections',
    });
  });

  it('reconnects on close with max 5 retries', () => {
    renderHook(() => useAgentSession('sess-9'));
    expect(MockWebSocket.instances).toHaveLength(1);

    // Simulate close -> should schedule reconnect
    act(() => MockWebSocket.instances[0]._simulateClose());
    act(() => vi.advanceTimersByTime(3000));
    expect(MockWebSocket.instances).toHaveLength(2);

    // Close again 4 more times (total 5 retries)
    for (let i = 0; i < 4; i++) {
      act(() => MockWebSocket.instances.at(-1)._simulateClose());
      act(() => vi.advanceTimersByTime(3000));
    }
    expect(MockWebSocket.instances).toHaveLength(6); // 1 original + 5 retries

    // 6th close should NOT reconnect (max retries reached)
    act(() => MockWebSocket.instances.at(-1)._simulateClose());
    act(() => vi.advanceTimersByTime(3000));
    expect(MockWebSocket.instances).toHaveLength(6);
  });

  it('resets state when sessionId changes', () => {
    const { result, rerender } = renderHook(
      ({ id }) => useAgentSession(id),
      { initialProps: { id: 'sess-a' } },
    );
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());
    act(() => ws._simulateMessage({ type: 'token', content: 'hi' }));
    expect(result.current.messages).toHaveLength(1);

    // Change session
    rerender({ id: 'sess-b' });
    expect(result.current.messages).toHaveLength(0);
    expect(result.current.isConnected).toBe(false);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('cleans up WebSocket on unmount', () => {
    const { unmount } = renderHook(() => useAgentSession('sess-cleanup'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    unmount();
    expect(ws.readyState).toBe(3); // CLOSED
  });

  /* ---------- Compaction status state machine ---------- */

  it('sets sticky compacting status and suppresses heartbeat overwrites', () => {
    const { result } = renderHook(() => useAgentSession('sess-compact-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    expect(result.current.status).toBe('compacting');

    // Heartbeat from BE keepalive must NOT overwrite the sticky compacting badge.
    act(() => ws._simulateMessage({ type: 'status', status: 'processing' }));
    expect(result.current.status).toBe('compacting');

    act(() => ws._simulateMessage({ type: 'status', status: 'idle' }));
    expect(result.current.status).toBe('compacting');

    // Re-emitted compacting (BE re-fires every 30s while compaction runs)
    // must remain idempotent.
    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    expect(result.current.status).toBe('compacting');
  });

  it('clears compacting state on compact_done and resumes normal status flow', () => {
    const { result } = renderHook(() => useAgentSession('sess-compact-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Simulate a turn in flight before compaction
    act(() => result.current.sendMessage('do work'));
    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    expect(result.current.status).toBe('compacting');

    act(() =>
      ws._simulateMessage({
        type: 'status',
        status: 'compact_done',
        trigger: 'auto',
        pre_tokens: 175296,
      }),
    );
    // Turn still in flight → revert to processing.
    expect(result.current.status).toBe('processing');

    // Subsequent heartbeat should now write through (sticky lock released).
    act(() => ws._simulateMessage({ type: 'status', status: 'thinking' }));
    expect(result.current.status).toBe('thinking');
  });

  it('compact_done resolves to idle when no turn is in flight', () => {
    const { result } = renderHook(() => useAgentSession('sess-compact-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    act(() => ws._simulateMessage({ type: 'status', status: 'compact_done' }));
    expect(result.current.status).toBe('idle');
  });

  /* ---------- idle_warning friendly label ---------- */

  it('formats idle_warning into a friendly label and OVERWRITES on each event', () => {
    const { result } = renderHook(() => useAgentSession('sess-idle-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'idle_warning', seconds: 120 }));
    expect(result.current.status).toBe('Agent silent for 120s…');

    // Cumulative seconds (NOT increment) — second event must OVERWRITE,
    // not stack/append. A 6-min stall produces 120, 240, 360.
    act(() => ws._simulateMessage({ type: 'status', status: 'idle_warning', seconds: 240 }));
    expect(result.current.status).toBe('Agent silent for 240s…');

    act(() => ws._simulateMessage({ type: 'status', status: 'idle_warning', seconds: 360 }));
    expect(result.current.status).toBe('Agent silent for 360s…');

    // BE emits a non-idle status once activity resumes — handler must
    // accept it (no sticky lock on idle_warning).
    act(() => ws._simulateMessage({ type: 'status', status: 'processing' }));
    expect(result.current.status).toBe('processing');
  });

  it('falls back to a generic silent label when seconds is missing or non-numeric', () => {
    const { result } = renderHook(() => useAgentSession('sess-idle-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'idle_warning' }));
    expect(result.current.status).toBe('Agent silent…');
  });

  /* ---------- oversized_line warning ---------- */

  it('oversized_line sets warningMessage and does NOT touch status', () => {
    const { result } = renderHook(() => useAgentSession('sess-oversized-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'processing' }));
    expect(result.current.status).toBe('processing');
    expect(result.current.warningMessage).toBeNull();

    act(() => ws._simulateMessage({ type: 'status', status: 'oversized_line' }));
    // status must not become the raw event name
    expect(result.current.status).toBe('processing');
    // warningMessage is set to the human-readable label
    expect(result.current.warningMessage).toBe('⚠ Line too long — skipped');
  });

  it('warningMessage is cleared when a subsequent normal status arrives', () => {
    const { result } = renderHook(() => useAgentSession('sess-oversized-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'oversized_line' }));
    expect(result.current.warningMessage).toBe('⚠ Line too long — skipped');

    act(() => ws._simulateMessage({ type: 'status', status: 'processing' }));
    expect(result.current.warningMessage).toBeNull();
  });

  /* ---------- compact_done transient banner ---------- */

  it('compact_done sets compactBanner with auto trigger label', () => {
    const { result } = renderHook(() => useAgentSession('sess-banner-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    act(() =>
      ws._simulateMessage({
        type: 'status',
        status: 'compact_done',
        trigger: 'auto',
        pre_tokens: 120000,
      }),
    );
    expect(result.current.compactBanner).toBe('Auto-compacted — 120k tokens freed');
  });

  it('compact_done sets compactBanner with manual trigger label', () => {
    const { result } = renderHook(() => useAgentSession('sess-banner-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    act(() =>
      ws._simulateMessage({
        type: 'status',
        status: 'compact_done',
        trigger: 'manual',
        pre_tokens: 50000,
      }),
    );
    expect(result.current.compactBanner).toBe('Compacted on request — 50k tokens freed');
  });

  it('compact_done banner auto-clears after 2 seconds', () => {
    const { result } = renderHook(() => useAgentSession('sess-banner-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    act(() =>
      ws._simulateMessage({
        type: 'status',
        status: 'compact_done',
        trigger: 'auto',
        pre_tokens: 80000,
      }),
    );
    expect(result.current.compactBanner).toBe('Auto-compacted — 80k tokens freed');

    // Advance to just before 2s — banner still visible
    act(() => vi.advanceTimersByTime(1999));
    expect(result.current.compactBanner).toBe('Auto-compacted — 80k tokens freed');

    // Advance past 2s — banner auto-clears
    act(() => vi.advanceTimersByTime(1));
    expect(result.current.compactBanner).toBeNull();
  });

  it('compact_done banner shows without token count when pre_tokens missing', () => {
    const { result } = renderHook(() => useAgentSession('sess-banner-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'status', status: 'compacting' }));
    act(() =>
      ws._simulateMessage({
        type: 'status',
        status: 'compact_done',
      }),
    );
    // Falls back to 'auto' label, no token count
    expect(result.current.compactBanner).toBe('Auto-compacted');
  });

  /* ---------- history-clobber guard on reconnect mid-turn ---------- */

  it('drops history replay when a user turn is in flight (reconnect-clobber guard)', () => {
    const { result } = renderHook(() => useAgentSession('sess-replay-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // User sends a message → optimistic state (1 user message), turn in flight.
    act(() => result.current.sendMessage('analyze SP500'));
    expect(result.current.messages).toHaveLength(1);

    // BE streams a partial assistant token before the WS drops.
    act(() => ws._simulateMessage({ type: 'token', content: 'Looking at ' }));
    expect(result.current.messages).toHaveLength(2);

    // Simulate BE replaying disk-persisted history mid-turn — disk only
    // contains messages from PRIOR completed turns (empty here).
    // Without the guard, this would wipe the in-flight user + assistant.
    act(() => ws._simulateMessage({ type: 'history', messages: [] }));
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toEqual({ role: 'user', content: 'analyze SP500' });
    expect(result.current.messages[1].role).toBe('assistant');
    expect(result.current.messages[1].content).toBe('Looking at ');
  });

  it('applies history when no turn is in flight (initial connect / post-turn)', () => {
    const { result } = renderHook(() => useAgentSession('sess-replay-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // No turn in flight → history replay should populate messages.
    const apiHistory = [
      { role: 'user', content: 'hello' },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'hi there' }],
      },
    ];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toEqual({ role: 'user', content: 'hello' });
    expect(result.current.messages[1].content).toBe('hi there');
  });

  it('re-applies history after message_complete clears the in-flight flag', () => {
    const { result } = renderHook(() => useAgentSession('sess-replay-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => result.current.sendMessage('first'));
    act(() => ws._simulateMessage({ type: 'token', content: 'ok' }));
    act(() => ws._simulateMessage({ type: 'message_complete' }));

    // After message_complete, in-flight flag is cleared. The next history
    // replay (e.g. a clean reconnect) should be honored, since the BE has
    // now persisted the turn and any replay would be authoritative.
    act(() =>
      ws._simulateMessage({
        type: 'history',
        messages: [
          { role: 'user', content: 'first' },
          { role: 'assistant', content: [{ type: 'text', text: 'ok' }] },
          { role: 'user', content: 'second' },
        ],
      }),
    );
    expect(result.current.messages).toHaveLength(3);
    expect(result.current.messages[2]).toEqual({ role: 'user', content: 'second' });
  });

  it('clears in-flight flag on stopped and on error', () => {
    const { result } = renderHook(() => useAgentSession('sess-replay-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // stopped → flag cleared, history replay should now apply.
    act(() => result.current.sendMessage('a'));
    act(() => ws._simulateMessage({ type: 'stopped' }));
    act(() =>
      ws._simulateMessage({
        type: 'history',
        messages: [{ role: 'user', content: 'persisted' }],
      }),
    );
    expect(result.current.messages).toEqual([{ role: 'user', content: 'persisted' }]);

    // error → flag cleared, history replay should now apply.
    act(() => result.current.sendMessage('b'));
    act(() => ws._simulateMessage({ type: 'error', message: 'boom' }));
    act(() =>
      ws._simulateMessage({
        type: 'history',
        messages: [{ role: 'user', content: 'after-error' }],
      }),
    );
    // Last setMessages from history replaces all → just the after-error user.
    expect(result.current.messages).toEqual([{ role: 'user', content: 'after-error' }]);
  });

  /* ---------- process_exit event ---------- */

  it('process_exit event sets processExitInfo with returncode, stderrTail, sessionId', () => {
    const { result } = renderHook(() => useAgentSession('sess-exit-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.processExitInfo).toBeNull();

    act(() =>
      ws._simulateMessage({
        type: 'process_exit',
        returncode: 1,
        saw_result: false,
        session_id: 'sess-exit-1',
        had_content: false,
        stderr_tail: 'Traceback (most recent call last):\n  File "run.py", line 5\nKeyError: x',
      }),
    );

    expect(result.current.processExitInfo).toEqual({
      returncode: 1,
      stderrTail: 'Traceback (most recent call last):\n  File "run.py", line 5\nKeyError: x',
      sessionId: 'sess-exit-1',
    });
    // isProcessing must be cleared
    expect(result.current.isProcessing).toBe(false);
  });

  it('process_exit does NOT auto-clear (no timer fires)', () => {
    const { result } = renderHook(() => useAgentSession('sess-exit-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'process_exit',
        returncode: -1,
        saw_result: false,
        session_id: 'sess-exit-2',
        had_content: false,
        stderr_tail: null,
      }),
    );

    expect(result.current.processExitInfo).not.toBeNull();

    // Advance timers well past any plausible auto-clear threshold
    act(() => vi.advanceTimersByTime(30000));

    // Banner must still be set — only explicit dismiss or new message clears it
    expect(result.current.processExitInfo).not.toBeNull();
    expect(result.current.processExitInfo.returncode).toBe(-1);
  });

  it('clearProcessExit callback resets processExitInfo to null', () => {
    const { result } = renderHook(() => useAgentSession('sess-exit-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'process_exit',
        returncode: 137,
        saw_result: false,
        session_id: 'sess-exit-3',
        had_content: true,
        stderr_tail: 'killed',
      }),
    );
    expect(result.current.processExitInfo).not.toBeNull();

    act(() => result.current.clearProcessExit());
    expect(result.current.processExitInfo).toBeNull();
  });

  /* ---------- turn_aborted event (Issue 9) ---------- */

  it('turn_aborted event clears in-flight state and sets turnAbortedInfo', () => {
    const { result } = renderHook(() => useAgentSession('sess-aborted-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Put a turn in flight first so we can verify the cleanup.
    act(() => result.current.sendMessage('analyze'));
    expect(result.current.isProcessing).toBe(true);
    expect(result.current.turnAbortedInfo).toBeNull();

    act(() =>
      ws._simulateMessage({
        type: 'turn_aborted',
        reason: 'ws_disconnect',
        session_id: 'sess-aborted-1',
        had_partial_content: true,
      }),
    );

    expect(result.current.isProcessing).toBe(false);
    expect(result.current.turnAbortedInfo).toEqual({
      reason: 'ws_disconnect',
      hadPartialContent: true,
    });
  });

  it('clearTurnAborted resets turnAbortedInfo to null', () => {
    const { result } = renderHook(() => useAgentSession('sess-aborted-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'turn_aborted',
        reason: 'ws_disconnect',
        session_id: 'sess-aborted-2',
        had_partial_content: false,
      }),
    );
    expect(result.current.turnAbortedInfo).not.toBeNull();

    act(() => result.current.clearTurnAborted());
    expect(result.current.turnAbortedInfo).toBeNull();
  });

  it('sending a new user message clears turnAbortedInfo', () => {
    const { result } = renderHook(() => useAgentSession('sess-aborted-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'turn_aborted',
        reason: 'ws_disconnect',
        session_id: 'sess-aborted-3',
        had_partial_content: true,
      }),
    );
    expect(result.current.turnAbortedInfo).not.toBeNull();

    act(() => result.current.sendMessage('try again'));
    expect(result.current.turnAbortedInfo).toBeNull();
  });

  it('after turn_aborted, a subsequent history payload is applied (in-flight ref cleared)', () => {
    // Issue 9 success path: turn_aborted clears hasInFlightTurnRef so the BE
    // reconnect-history replay populates messages instead of being dropped.
    const { result } = renderHook(() => useAgentSession('sess-aborted-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => result.current.sendMessage('long task'));
    act(() =>
      ws._simulateMessage({
        type: 'turn_aborted',
        reason: 'ws_disconnect',
        session_id: 'sess-aborted-4',
        had_partial_content: true,
      }),
    );

    act(() =>
      ws._simulateMessage({
        type: 'history',
        messages: [
          { role: 'user', content: 'long task' },
          { role: 'assistant', content: [{ type: 'text', text: 'partial' }] },
        ],
      }),
    );
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toEqual({ role: 'user', content: 'long task' });
    expect(result.current.messages[1].content).toBe('partial');
  });

  it('WS-close handler clears the in-flight ref so a subsequent history is applied', () => {
    // Defensive complement of Issue 9: even without turn_aborted, the close
    // handler should clear hasInFlightTurnRef.
    const { result } = renderHook(() => useAgentSession('sess-close-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => result.current.sendMessage('first'));
    expect(result.current.messages).toHaveLength(1);

    // Simulate disconnect → reconnect → history payload.
    act(() => ws._simulateClose());
    act(() => vi.advanceTimersByTime(3000));
    const ws2 = MockWebSocket.instances[1];
    act(() => ws2._simulateOpen());
    act(() =>
      ws2._simulateMessage({
        type: 'history',
        messages: [{ role: 'user', content: 'persisted-from-be' }],
      }),
    );
    // History was applied (would have been dropped if hasInFlightTurnRef
    // were still latched).
    expect(result.current.messages).toEqual([{ role: 'user', content: 'persisted-from-be' }]);
  });

  /* ---------- subagent_count event (Issue 10) ---------- */

  it('subagent_count event updates subagentCount state', () => {
    const { result } = renderHook(() => useAgentSession('sess-subagent-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.subagentCount).toBe(0);

    act(() => ws._simulateMessage({ type: 'subagent_count', count: 3 }));
    expect(result.current.subagentCount).toBe(3);

    act(() => ws._simulateMessage({ type: 'subagent_count', count: 1 }));
    expect(result.current.subagentCount).toBe(1);

    // count = 0 must clear the badge (i.e. supersede prior nonzero state).
    act(() => ws._simulateMessage({ type: 'subagent_count', count: 0 }));
    expect(result.current.subagentCount).toBe(0);
  });

  it('subagent_count ignores malformed (non-numeric) payloads', () => {
    const { result } = renderHook(() => useAgentSession('sess-subagent-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'subagent_count', count: 5 }));
    expect(result.current.subagentCount).toBe(5);

    act(() => ws._simulateMessage({ type: 'subagent_count', count: 'oops' }));
    // Malformed payload is ignored; prior value preserved.
    expect(result.current.subagentCount).toBe(5);
  });

  /* ---------- token_usage event (Issue 11) ---------- */

  it('token_usage event updates tokenUsage state', () => {
    const { result } = renderHook(() => useAgentSession('sess-tokens-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.tokenUsage).toEqual({ input: 0, output: 0, total: 0 });

    act(() =>
      ws._simulateMessage({
        type: 'token_usage',
        session_input: 1000,
        session_output: 500,
        session_total: 1500,
      }),
    );
    expect(result.current.tokenUsage).toEqual({ input: 1000, output: 500, total: 1500 });

    // Subsequent emission overwrites (cumulative monotonic per contract).
    act(() =>
      ws._simulateMessage({
        type: 'token_usage',
        session_input: 12345,
        session_output: 4700,
        session_total: 17045,
      }),
    );
    expect(result.current.tokenUsage).toEqual({ input: 12345, output: 4700, total: 17045 });
  });

  /* ---------- elapsed-time ticker (Issue 12) ---------- */

  it('elapsedMs starts at 0 and ticks forward while processing', () => {
    const { result } = renderHook(() => useAgentSession('sess-elapsed-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.elapsedMs).toBe(0);

    act(() => result.current.sendMessage('go'));
    // Right after send, elapsed is ~0.
    expect(result.current.elapsedMs).toBeLessThan(50);
    expect(result.current.turnStartTimestamp).not.toBeNull();

    // Advance fake timers by 12s — the interval ticks each second; React
    // only re-renders on each setElapsedMs call, but the final state should
    // reflect ~12_000 ms.
    act(() => vi.advanceTimersByTime(12_000));
    // Allow some slack: elapsed is computed from Date.now() - start, both
    // of which advance with fake timers.
    expect(result.current.elapsedMs).toBeGreaterThanOrEqual(11_000);
    expect(result.current.elapsedMs).toBeLessThanOrEqual(13_000);
  });

  it('elapsedMs clears on message_complete', () => {
    const { result } = renderHook(() => useAgentSession('sess-elapsed-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => result.current.sendMessage('go'));
    act(() => vi.advanceTimersByTime(5_000));
    expect(result.current.elapsedMs).toBeGreaterThan(0);

    act(() => ws._simulateMessage({ type: 'message_complete' }));
    expect(result.current.turnStartTimestamp).toBeNull();
    // After message_complete the ticker stops; advancing time should not
    // change elapsedMs further (it's frozen / zero, depending on terminal
    // handler — current contract clears turnStartTimestamp which stops the
    // ticker effect).
    const beforeAdvance = result.current.elapsedMs;
    act(() => vi.advanceTimersByTime(10_000));
    expect(result.current.elapsedMs).toBe(beforeAdvance);
  });

  it('elapsedMs ticker clears on turn_aborted, error, stopped, process_exit', () => {
    const terminals = [
      { type: 'turn_aborted', reason: 'ws_disconnect', session_id: 's', had_partial_content: false },
      { type: 'error', message: 'boom' },
      { type: 'stopped' },
      { type: 'process_exit', returncode: 1, saw_result: false, session_id: 's', had_content: false, stderr_tail: null },
    ];
    for (const terminal of terminals) {
      MockWebSocket.instances = [];
      const { result, unmount } = renderHook(() => useAgentSession(`sess-elapsed-term-${terminal.type}`));
      const ws = MockWebSocket.instances[0];
      act(() => ws._simulateOpen());
      act(() => result.current.sendMessage('go'));
      act(() => vi.advanceTimersByTime(2_000));
      expect(result.current.turnStartTimestamp).not.toBeNull();
      act(() => ws._simulateMessage(terminal));
      expect(result.current.turnStartTimestamp).toBeNull();
      unmount();
    }
  });

  it('elapsed ticker is cleaned up on unmount (no leaked interval)', () => {
    const { result, unmount } = renderHook(() => useAgentSession('sess-elapsed-cleanup'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());
    act(() => result.current.sendMessage('go'));
    act(() => vi.advanceTimersByTime(3_000));
    const before = result.current.elapsedMs;
    unmount();
    // After unmount, advancing timers must NOT throw or trigger setState on
    // an unmounted component (vitest will surface React act() warnings).
    act(() => vi.advanceTimersByTime(10_000));
    // The hook return is the last-rendered snapshot; we just confirm we got
    // here without errors and the snapshot is reasonable.
    expect(result.current.elapsedMs).toBe(before);
  });

  /* ---------- format helpers (Issue 11/12) ---------- */

  it('formatTokens humanizes counts as "123" / "12.3k" / "1.5M"', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(123)).toBe('123');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1_000)).toBe('1.0k');
    expect(formatTokens(12_345)).toBe('12.3k');
    expect(formatTokens(99_999)).toBe('100.0k');
    expect(formatTokens(150_000)).toBe('150k');
    expect(formatTokens(1_500_000)).toBe('1.5M');
    expect(formatTokens(-5)).toBe('0');
    expect(formatTokens('not-a-number')).toBe('0');
  });

  it('formatElapsed produces "Xs" / "Xm Ys" / "Xh Ym"', () => {
    expect(formatElapsed(0)).toBe('0s');
    expect(formatElapsed(12_000)).toBe('12s');
    expect(formatElapsed(59_999)).toBe('59s');
    expect(formatElapsed(60_000)).toBe('1m 00s');
    expect(formatElapsed(83_000)).toBe('1m 23s');
    expect(formatElapsed(302_000)).toBe('5m 02s');
    expect(formatElapsed(3_600_000)).toBe('1h 00m');
    expect(formatElapsed(3_720_000)).toBe('1h 02m');
    expect(formatElapsed(-100)).toBe('0s');
  });

  /* ---------- turn_complete event (Issue 16b) ---------- */

  it('turn_complete event sets lastTurnComplete with at and elapsedSeconds', () => {
    const { result } = renderHook(() => useAgentSession('sess-tc-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.lastTurnComplete).toBeNull();

    const timestamp = '2026-05-07T12:34:56.789Z';
    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-1',
        elapsed_seconds: 83.4,
        timestamp,
      }),
    );

    expect(result.current.lastTurnComplete).not.toBeNull();
    expect(result.current.lastTurnComplete.elapsedSeconds).toBeCloseTo(83.4, 1);
    expect(result.current.lastTurnComplete.at).toBeInstanceOf(Date);
    expect(result.current.lastTurnComplete.at.toISOString()).toBe(timestamp);
  });

  it('turn_complete elapsed_seconds is a positive number', () => {
    const { result } = renderHook(() => useAgentSession('sess-tc-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-2',
        elapsed_seconds: 5.7,
        timestamp: new Date().toISOString(),
      }),
    );

    expect(result.current.lastTurnComplete.elapsedSeconds).toBeGreaterThan(0);
    expect(typeof result.current.lastTurnComplete.elapsedSeconds).toBe('number');
  });

  it('turn_complete replaces prior lastTurnComplete (not cumulative)', () => {
    const { result } = renderHook(() => useAgentSession('sess-tc-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-3',
        elapsed_seconds: 10,
        timestamp: '2026-05-07T10:00:00.000Z',
      }),
    );
    expect(result.current.lastTurnComplete.elapsedSeconds).toBe(10);

    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-3',
        elapsed_seconds: 25,
        timestamp: '2026-05-07T10:01:00.000Z',
      }),
    );
    // Second event replaces first — not additive.
    expect(result.current.lastTurnComplete.elapsedSeconds).toBe(25);
    expect(result.current.lastTurnComplete.at.toISOString()).toBe('2026-05-07T10:01:00.000Z');
  });

  it('turn_complete clears hasInFlightTurnRef (defence-in-depth)', () => {
    // Indirect test: after turn_complete, a subsequent history replay is
    // applied (proving hasInFlightTurnRef was cleared).
    const { result } = renderHook(() => useAgentSession('sess-tc-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Put a turn in flight.
    act(() => result.current.sendMessage('do work'));
    expect(result.current.messages).toHaveLength(1);

    // BE emits turn_complete (clean turn ended).
    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-4',
        elapsed_seconds: 4.2,
        timestamp: new Date().toISOString(),
      }),
    );

    // In-flight ref is cleared → subsequent history replay must be applied.
    act(() =>
      ws._simulateMessage({
        type: 'history',
        messages: [{ role: 'user', content: 'persisted' }],
      }),
    );
    expect(result.current.messages).toEqual([{ role: 'user', content: 'persisted' }]);
  });

  it('turn_complete is reset on session change', () => {
    const { result, rerender } = renderHook(
      ({ id }) => useAgentSession(id),
      { initialProps: { id: 'sess-tc-5a' } },
    );
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-tc-5a',
        elapsed_seconds: 8,
        timestamp: new Date().toISOString(),
      }),
    );
    expect(result.current.lastTurnComplete).not.toBeNull();

    // Switch session → must reset.
    rerender({ id: 'sess-tc-5b' });
    expect(result.current.lastTurnComplete).toBeNull();
  });

  it('sending a new user message clears processExitInfo', () => {
    const { result } = renderHook(() => useAgentSession('sess-exit-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'process_exit',
        returncode: 1,
        saw_result: false,
        session_id: 'sess-exit-4',
        had_content: false,
        stderr_tail: null,
      }),
    );
    expect(result.current.processExitInfo).not.toBeNull();

    // Sending a new message should clear the banner
    act(() => result.current.sendMessage('try again'));
    expect(result.current.processExitInfo).toBeNull();
  });
});

/* ============================================================
   Issue 21 — transformHistory rewrite tests
   ============================================================ */

describe('transformHistory (Issue 21 — via history event)', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('preserves interleaved text/tool_use ordering on replay', () => {
    const { result } = renderHook(() => useAgentSession('sess-i21-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // API format: text → tool → text → tool → text (5 blocks)
    const apiHistory = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'Step 1 text.' },
          { type: 'tool_use', name: 'Bash', input: { cmd: 'ls' } },
          { type: 'text', text: 'Step 2 text.' },
          { type: 'tool_use', name: 'Read', input: { file: 'foo.py' } },
          { type: 'text', text: 'Step 3 text.' },
        ],
      },
    ];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));

    const msgs = result.current.messages;
    // 5 content blocks → 5 display entries (3 text + 2 tool)
    expect(msgs).toHaveLength(5);
    expect(msgs[0]).toEqual({ role: 'assistant', content: 'Step 1 text.', streaming: false });
    expect(msgs[1]).toEqual({ role: 'tool', name: 'Bash', input: { cmd: 'ls' } });
    expect(msgs[2]).toEqual({ role: 'assistant', content: 'Step 2 text.', streaming: false });
    expect(msgs[3]).toEqual({ role: 'tool', name: 'Read', input: { file: 'foo.py' } });
    expect(msgs[4]).toEqual({ role: 'assistant', content: 'Step 3 text.', streaming: false });
  });

  it('non-adjacent text blocks remain separate display entries', () => {
    const { result } = renderHook(() => useAgentSession('sess-i21-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    const apiHistory = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'Before tool.' },
          { type: 'tool_use', name: 'Bash', input: {} },
          { type: 'text', text: 'After tool.' },
        ],
      },
    ];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));

    const msgs = result.current.messages;
    expect(msgs).toHaveLength(3);
    expect(msgs[0].content).toBe('Before tool.');
    expect(msgs[1].role).toBe('tool');
    expect(msgs[2].content).toBe('After tool.');
  });

  it('adjacent text blocks are coalesced (acceptable behavior)', () => {
    const { result } = renderHook(() => useAgentSession('sess-i21-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Two adjacent text blocks (no tool_use between them) — may be coalesced.
    const apiHistory = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'First. ' },
          { type: 'text', text: 'Second.' },
        ],
      },
    ];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));

    const msgs = result.current.messages;
    // Either 1 or 2 entries is acceptable; content must be present.
    expect(msgs.length).toBeGreaterThanOrEqual(1);
    const combinedText = msgs.map((m) => m.content).join('');
    expect(combinedText).toContain('First.');
    expect(combinedText).toContain('Second.');
  });

  it('DONE marker is stripped from displayed text (replay path)', () => {
    const { result } = renderHook(() => useAgentSession('sess-i21-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    const apiHistory = [
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'All done.\n<<<TURN_HANDOFF_DONE>>>\n' },
        ],
      },
    ];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));

    const msgs = result.current.messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('All done.\n');
    expect(msgs[0].content).not.toContain('<<<TURN_HANDOFF_DONE>>>');
  });

  it('raw stored data is unmodified (persistence keeps marker)', () => {
    // Verify that the raw API history object passed to transformHistory is not
    // mutated — the marker strip only creates new display strings.
    const { result } = renderHook(() => useAgentSession('sess-i21-5'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    const rawMsg = {
      role: 'assistant',
      content: [{ type: 'text', text: 'Done.<<<TURN_HANDOFF_DONE>>>' }],
    };
    const apiHistory = [rawMsg];
    act(() => ws._simulateMessage({ type: 'history', messages: apiHistory }));

    // The display text should have marker stripped.
    expect(result.current.messages[0].content).not.toContain('<<<TURN_HANDOFF_DONE>>>');
    // The original object must not be mutated.
    expect(rawMsg.content[0].text).toContain('<<<TURN_HANDOFF_DONE>>>');
  });
});

/* ============================================================
   stripDoneMarker unit tests
   ============================================================ */

describe('stripDoneMarker', () => {
  it('strips marker and trailing newline', () => {
    expect(stripDoneMarker('All done.\n<<<TURN_HANDOFF_DONE>>>\n'))
      .toBe('All done.\n');
  });

  it('strips marker without trailing newline', () => {
    expect(stripDoneMarker('Done.<<<TURN_HANDOFF_DONE>>>'))
      .toBe('Done.');
  });

  it('leaves text unchanged when marker is absent', () => {
    expect(stripDoneMarker('No marker here.')).toBe('No marker here.');
  });

  it('strips marker mid-text (multiple occurrences)', () => {
    const result = stripDoneMarker('A<<<TURN_HANDOFF_DONE>>>B<<<TURN_HANDOFF_DONE>>>C');
    expect(result).toBe('ABC');
  });

  it('does not strip adjacent punctuation before marker', () => {
    expect(stripDoneMarker('Complete!\n<<<TURN_HANDOFF_DONE>>>'))
      .toBe('Complete!\n');
  });
});

/* ============================================================
   DONE marker stripping in live token stream (message_complete path)
   ============================================================ */

describe('DONE marker stripping — live path (message_complete)', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('strips marker from streaming bubble on message_complete', () => {
    const { result } = renderHook(() => useAgentSession('sess-strip-live-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() => ws._simulateMessage({ type: 'token', content: 'Work done.' }));
    act(() => ws._simulateMessage({ type: 'token', content: '<<<TURN_HANDOFF_DONE>>>' }));
    act(() => ws._simulateMessage({ type: 'message_complete' }));

    const msgs = result.current.messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('Work done.');
    expect(msgs[0].streaming).toBe(false);
    expect(msgs[0].content).not.toContain('<<<TURN_HANDOFF_DONE>>>');
  });
});

/* ============================================================
   Issue 23 — auto-continue UX tests
   ============================================================ */

describe('auto_continue events (Issue 23)', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('auto_continue event sets autoContinueInfo', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.autoContinueInfo).toBeNull();

    act(() =>
      ws._simulateMessage({
        type: 'auto_continue',
        session_id: 'sess-ac-1',
        iter: 1,
        max: 5,
        reason: 'missing_done_marker',
        timestamp: Date.now() / 1000,
      }),
    );

    expect(result.current.autoContinueInfo).toEqual({
      iter: 1,
      max: 5,
      reason: 'missing_done_marker',
    });
  });

  it('auto_continue_capped event sets autoContinueCapped to {iter, max} (S2 fix)', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // S2 fix: initial state is null (not false)
    expect(result.current.autoContinueCapped).toBeNull();

    act(() =>
      ws._simulateMessage({
        type: 'auto_continue_capped',
        session_id: 'sess-ac-2',
        iter: 2,
        max: 2,
        reason: 'cap_reached',
        timestamp: Date.now() / 1000,
      }),
    );

    // S2 fix: autoContinueCapped is now {iter, max} not boolean true
    expect(result.current.autoContinueCapped).toEqual({ iter: 2, max: 2 });
    // Capped event clears autoContinueInfo
    expect(result.current.autoContinueInfo).toBeNull();
  });

  it('user message clears autoContinueInfo and autoContinueCapped', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Set up capped state
    act(() =>
      ws._simulateMessage({
        type: 'auto_continue',
        session_id: 'sess-ac-3',
        iter: 2,
        max: 5,
        reason: 'unmet_intent',
        timestamp: Date.now() / 1000,
      }),
    );
    act(() =>
      ws._simulateMessage({
        type: 'auto_continue_capped',
        session_id: 'sess-ac-3',
        iter: 5,
        max: 5,
        reason: 'cap_reached',
        timestamp: Date.now() / 1000,
      }),
    );
    // S2 fix: autoContinueCapped is {iter, max} not boolean
    expect(result.current.autoContinueCapped).toEqual({ iter: 5, max: 5 });

    // User sends a new message → both should clear
    act(() => result.current.sendMessage('try again'));
    expect(result.current.autoContinueInfo).toBeNull();
    expect(result.current.autoContinueCapped).toBeNull();
  });

  it('turn_complete after auto_continue clears autoContinueInfo', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'auto_continue',
        session_id: 'sess-ac-4',
        iter: 1,
        max: 5,
        reason: 'missing_done_marker',
        timestamp: Date.now() / 1000,
      }),
    );
    expect(result.current.autoContinueInfo).not.toBeNull();

    // Simulate the continuation turn completing
    act(() =>
      ws._simulateMessage({
        type: 'turn_complete',
        session_id: 'sess-ac-4',
        elapsed_seconds: 5,
        timestamp: new Date().toISOString(),
      }),
    );

    // turn_complete after autoContinueInfo was set should clear it
    expect(result.current.autoContinueInfo).toBeNull();
  });

  it('auto_continue sets correct reason field', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-5'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'auto_continue',
        session_id: 'sess-ac-5',
        iter: 2,
        max: 5,
        reason: 'unmet_intent',
        timestamp: Date.now() / 1000,
      }),
    );

    expect(result.current.autoContinueInfo.reason).toBe('unmet_intent');
    expect(result.current.autoContinueInfo.iter).toBe(2);
    expect(result.current.autoContinueInfo.max).toBe(5);
  });

  it('interrupt (interruptAgent) clears auto-continue state', () => {
    const { result } = renderHook(() => useAgentSession('sess-ac-6'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'auto_continue',
        session_id: 'sess-ac-6',
        iter: 1,
        max: 5,
        reason: 'missing_done_marker',
        timestamp: Date.now() / 1000,
      }),
    );
    expect(result.current.autoContinueInfo).not.toBeNull();

    act(() => result.current.interruptAgent('stop'));
    expect(result.current.autoContinueInfo).toBeNull();
    // S2 fix: cleared to null (not false)
    expect(result.current.autoContinueCapped).toBeNull();
  });
});

/* ============================================================
   Issue 27 F3 — notebook_failed event handler tests
   ============================================================ */

describe('notebook_failed event (Issue 27 F3)', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('notebook_failed event sets notebookFailedInfo and clears notebookReady', () => {
    const { result } = renderHook(() => useAgentSession('sess-nf-1'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    expect(result.current.notebookFailedInfo).toBeNull();
    expect(result.current.notebookReady).toBe(false);

    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-1',
        reason: 'no_outputs',
        detail: 'Notebook has 7 code cells, all with empty outputs[].',
        timestamp: '2026-05-07T16:00:00Z',
      }),
    );

    expect(result.current.notebookFailedInfo).toEqual({
      reason: 'no_outputs',
      detail: 'Notebook has 7 code cells, all with empty outputs[].',
      timestamp: '2026-05-07T16:00:00Z',
    });
    expect(result.current.notebookReady).toBe(false);
  });

  it('notebook_failed parse_error reason is stored correctly', () => {
    const { result } = renderHook(() => useAgentSession('sess-nf-2'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-2',
        reason: 'parse_error',
        detail: 'JSON parse error at position 142',
        timestamp: '2026-05-07T16:01:00Z',
      }),
    );

    expect(result.current.notebookFailedInfo.reason).toBe('parse_error');
    expect(result.current.notebookFailedInfo.detail).toBe('JSON parse error at position 142');
  });

  it('notebook_failed with missing optional fields uses defaults', () => {
    const { result } = renderHook(() => useAgentSession('sess-nf-3'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-3',
        // reason and timestamp omitted to test defaults
      }),
    );

    expect(result.current.notebookFailedInfo.reason).toBe('no_outputs');
    expect(result.current.notebookFailedInfo.detail).toBeNull();
    expect(typeof result.current.notebookFailedInfo.timestamp).toBe('string');
  });

  it('notebook_failed sets notebookReady=false even if it was previously true', () => {
    const { result } = renderHook(() => useAgentSession('sess-nf-4'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // First mark as ready.
    act(() =>
      ws._simulateMessage({ type: 'notebook_ready', session_id: 'sess-nf-4' }),
    );
    expect(result.current.notebookReady).toBe(true);
    expect(result.current.notebookFailedInfo).toBeNull();

    // Then fail.
    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-4',
        reason: 'no_outputs',
        timestamp: '2026-05-07T16:02:00Z',
      }),
    );
    expect(result.current.notebookReady).toBe(false);
    expect(result.current.notebookFailedInfo).not.toBeNull();
  });

  it('subsequent notebook_ready clears notebookFailedInfo (mutex)', () => {
    const { result } = renderHook(() => useAgentSession('sess-nf-5'));
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    // Fail first.
    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-5',
        reason: 'no_outputs',
        timestamp: '2026-05-07T16:03:00Z',
      }),
    );
    expect(result.current.notebookFailedInfo).not.toBeNull();

    // Then notebook_ready clears it.
    act(() =>
      ws._simulateMessage({ type: 'notebook_ready', session_id: 'sess-nf-5' }),
    );
    expect(result.current.notebookReady).toBe(true);
    expect(result.current.notebookFailedInfo).toBeNull();
  });

  it('notebookFailedInfo is reset on session change', () => {
    const { result, rerender } = renderHook(
      ({ id }) => useAgentSession(id),
      { initialProps: { id: 'sess-nf-6a' } },
    );
    const ws = MockWebSocket.instances[0];
    act(() => ws._simulateOpen());

    act(() =>
      ws._simulateMessage({
        type: 'notebook_failed',
        session_id: 'sess-nf-6a',
        reason: 'no_outputs',
        timestamp: '2026-05-07T16:04:00Z',
      }),
    );
    expect(result.current.notebookFailedInfo).not.toBeNull();

    // Switch session — must reset.
    rerender({ id: 'sess-nf-6b' });
    expect(result.current.notebookFailedInfo).toBeNull();
  });
});

/* ============================================================
   Issue 28b — useUnfocusedTitleAlert hook tests
   (tested in isolation via the hook's own test file)
   Minimal smoke tests here to validate document.title integration.
   ============================================================ */

describe('useAgentSession notebookReady returns correct shape', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('hook return includes notebookFailedInfo field', () => {
    const { result } = renderHook(() => useAgentSession('sess-shape-1'));
    // notebookFailedInfo should be present and null by default.
    expect('notebookFailedInfo' in result.current).toBe(true);
    expect(result.current.notebookFailedInfo).toBeNull();
  });
});
