import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import useAgentSession, { formatTokens, formatElapsed } from './useAgentSession';

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
