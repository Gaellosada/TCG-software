import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import useAgentSession from './useAgentSession';

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
});
