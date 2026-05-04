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

  _simulateClose() {
    this._emit('close', {});
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
});
