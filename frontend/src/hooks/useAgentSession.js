import { useState, useEffect, useRef, useCallback } from 'react';
import { getAssumptions } from '../api/agent';

const MAX_RETRIES = 5;
const RECONNECT_DELAY_MS = 3000;

/**
 * Build the WebSocket URL for an agent session.
 *
 * Priority:
 *  1. VITE_WS_URL env var (full ws:// base, e.g. "ws://localhost:8000")
 *  2. Derive from window.location (works behind Vite proxy when /ws is proxied)
 */
function buildWsUrl(sessionId) {
  const envBase =
    typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_WS_URL;
  if (envBase) {
    const base = envBase.replace(/\/$/, '');
    return `${base}/ws/agent/${sessionId}`;
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/agent/${sessionId}`;
}

/**
 * Transform Anthropic API-format conversation history into display messages.
 *
 * API format: assistant content is [{type:"text",text:...},{type:"tool_use",...}]
 * Display format: flat array of {role:"assistant",content:string} and {role:"tool",...}
 */
function transformHistory(apiMessages) {
  const display = [];
  for (const msg of apiMessages) {
    if (msg.role === 'user') {
      // User content can be a string or array of tool_result blocks
      if (typeof msg.content === 'string') {
        display.push({ role: 'user', content: msg.content });
      }
      // tool_result arrays (internal API state) are not shown to user
      continue;
    }
    if (msg.role === 'assistant') {
      // content is an array of content blocks
      if (Array.isArray(msg.content)) {
        const textParts = msg.content
          .filter((b) => b.type === 'text')
          .map((b) => b.text || '');
        const text = textParts.join('');
        if (text) {
          display.push({ role: 'assistant', content: text, streaming: false });
        }
        // Surface tool_use blocks as tool messages
        for (const block of msg.content) {
          if (block.type === 'tool_use') {
            display.push({ role: 'tool', name: block.name, input: block.input });
          }
        }
      } else if (typeof msg.content === 'string') {
        display.push({ role: 'assistant', content: msg.content, streaming: false });
      }
    }
  }
  return display;
}

/**
 * React hook that manages a WebSocket connection to the agent backend.
 *
 * @param {string|null} sessionId - Connect when truthy, disconnect when falsy.
 * @returns {{
 *   messages: Array,
 *   assumptions: Array,
 *   status: string,
 *   isConnected: boolean,
 *   sendMessage: (content: string) => void,
 *   notebookReady: boolean,
 * }}
 */
function useAgentSession(sessionId) {
  const [messages, setMessages] = useState([]);
  const [assumptions, setAssumptions] = useState([]);
  const [status, setStatus] = useState('idle');
  const [isConnected, setIsConnected] = useState(false);
  const [notebookReady, setNotebookReady] = useState(false);
  // True from user send until message_complete (covers thinking + streaming + tool loops)
  const [isProcessing, setIsProcessing] = useState(false);

  const wsRef = useRef(null);
  const retriesRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  // Track the current streaming (partial) assistant message
  const streamingRef = useRef(null);
  // Sticky flag: true between BE 'compacting' status and 'compact_done'.
  // While truthy, heartbeat/idle status writes are suppressed so the
  // "Compacting…" badge stays visible until the BE confirms it ended.
  const compactingRef = useRef(false);
  // Tracks whether a user-initiated turn is currently in flight (between
  // sendMessage / interruptAgent and message_complete / stopped / error).
  // Used to drop reconnect 'history' replays that would otherwise clobber
  // the in-flight turn (the BE persists conversation only after the turn
  // ends, so reconnect-time history is stale until that point — see
  // workspace/tasks/agent-context-and-streaming/output/issue2-diagnosis.md
  // §4 candidate 1).
  const hasInFlightTurnRef = useRef(false);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (!sessionId) return;

    const url = buildWsUrl(sessionId);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.addEventListener('open', () => {
      setIsConnected(true);
      retriesRef.current = 0;
      // Pre-load existing assumptions so they survive page refresh / session switch
      getAssumptions(sessionId)
        .then((data) => {
          if (wsRef.current !== ws) return; // Guard: this connection may already be stale
          const list = Array.isArray(data) ? data : (data?.assumptions ?? []);
          setAssumptions(list);
        })
        .catch(() => {
          // Non-fatal: assumptions will still arrive via WebSocket events
        });
    });

    ws.addEventListener('close', (event) => {
      // Guard: ignore close events from a superseded connection
      if (wsRef.current !== ws) return;

      setIsConnected(false);
      setIsProcessing(false);
      wsRef.current = null;

      // Only reconnect on abnormal closure codes.
      // Code 1000 = normal close, 1008 = policy violation (e.g. server rejects
      // duplicate connection). Both indicate an intentional close — do not retry.
      const RECONNECT_CODES = new Set([1006, 1011, 1012, 1013]);
      if (RECONNECT_CODES.has(event.code) && retriesRef.current < MAX_RETRIES) {
        retriesRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
      }
    });

    ws.addEventListener('error', () => {
      // Guard: ignore error events from a superseded connection
      if (wsRef.current !== ws) return;
      // The close event will fire after error, which handles reconnect.
    });

    ws.addEventListener('message', (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (data.type) {
        case 'token': {
          setIsProcessing(true); // Re-assert for queued turns
          // Append token text to the current streaming assistant message
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (streamingRef.current && last && last.role === 'assistant' && last.streaming) {
              // Append to the existing streaming assistant message
              return [...prev.slice(0, -1), { ...last, content: last.content + (data.content ?? '') }];
            }
            // Create a new assistant message
            streamingRef.current = true;
            return [...prev, { role: 'assistant', content: data.content ?? '', streaming: true }];
          });
          break;
        }

        case 'message_complete': {
          setIsProcessing(false);
          hasInFlightTurnRef.current = false;
          if (streamingRef.current) {
            streamingRef.current = null;
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const last = prev[prev.length - 1];
              return [...prev.slice(0, -1), { ...last, streaming: false }];
            });
          }
          break;
        }

        case 'history': {
          // Restore prior conversation. Fired on every WS connect, including
          // reconnects mid-turn (see api/agent.py:354-358). The BE only
          // persists conversation AFTER the turn completes (api/agent.py:281-286),
          // so during an in-flight turn the BE-replayed history is STALE — it
          // omits the user message + any streamed assistant tokens emitted
          // during the current turn. Replacing local state with stale history
          // wipes the in-flight turn from the UI ("interface reverted"
          // symptom). Drop the replay if a user turn is currently in flight.
          // First connect / post-turn reconnect: hasInFlightTurnRef is false,
          // history is applied as before.
          if (hasInFlightTurnRef.current) {
            // Reconnect mid-turn. BE history is stale; keep our optimistic
            // state intact. Once message_complete fires, the BE will persist
            // and any future reconnect will see authoritative history.
            break;
          }
          if (Array.isArray(data.messages)) {
            setMessages(transformHistory(data.messages));
          }
          break;
        }

        case 'assumptions_update': {
          // Full snapshot replace — BE emits a complete list each time.
          // Mid-turn arrivals are safe (idempotent: same array shape, no
          // side effects on messages or processing state).
          setAssumptions(data.assumptions ?? []);
          break;
        }

        case 'status': {
          const next = data.status ?? 'idle';
          // Compaction state machine: 'compacting' is sticky (BE may re-emit
          // every 30s while compaction runs — see issue2-diagnosis.md §1
          // and the keepalive race in §3). 'compact_done' is terminal and
          // releases the lock. While compacting, suppress heartbeat
          // overwrites ('processing'/'idle') so the badge stays stable.
          if (next === 'compacting') {
            compactingRef.current = true;
            setStatus('compacting');
            break;
          }
          if (next === 'compact_done') {
            compactingRef.current = false;
            // Hand control back to the normal status state machine. Use
            // 'processing' if the turn is still in flight (compaction always
            // happens mid-turn), else 'idle'.
            setStatus(hasInFlightTurnRef.current ? 'processing' : 'idle');
            break;
          }
          if (compactingRef.current) {
            // Drop heartbeat / idle / oversized noise during compaction.
            break;
          }
          if (next === 'idle_warning') {
            // BE emits cumulative seconds-since-last-CLI-output within a
            // single stalled stretch (see agent-runtime-bugs PROBLEMS.md
            // and tcg/core/agent/session.py:371-378). Each event is the
            // running total, NOT an increment — so OVERWRITE the displayed
            // duration, never append. Resets implicitly when CLI output
            // resumes (BE emits a non-idle status on the next event).
            const seconds = Number(data.seconds);
            const label = Number.isFinite(seconds)
              ? `Agent silent for ${seconds}s…`
              : 'Agent silent…';
            setStatus(label);
            break;
          }
          setStatus(next);
          break;
        }

        case 'notebook_ready': {
          setNotebookReady(true);
          break;
        }

        case 'tool_call': {
          setIsProcessing(true); // Re-assert for queued turns
          // Finalize any streaming assistant message before adding tool message
          if (streamingRef.current) {
            streamingRef.current = null;
          }
          setMessages((prev) => {
            const updated = prev.length > 0 && prev[prev.length - 1].streaming
              ? [...prev.slice(0, -1), { ...prev[prev.length - 1], streaming: false }]
              : prev;
            return [...updated, { role: 'tool', name: data.name, input: data.input }];
          });
          break;
        }

        case 'stopped': {
          setIsProcessing(false);
          hasInFlightTurnRef.current = false;
          compactingRef.current = false;
          streamingRef.current = null;
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const last = prev[prev.length - 1];
            if (last.streaming) {
              return [...prev.slice(0, -1), { ...last, streaming: false }];
            }
            return prev;
          });
          break;
        }

        case 'queued': {
          // Message was queued — no UI action needed
          break;
        }

        case 'interrupted': {
          // Current turn cancelled, new one starting. hasInFlightTurnRef
          // stays true (a new turn is starting); compaction state, if any,
          // belongs to the cancelled turn — clear it.
          compactingRef.current = false;
          streamingRef.current = null;
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const last = prev[prev.length - 1];
            if (last.streaming) {
              return [...prev.slice(0, -1), { ...last, streaming: false }];
            }
            return prev;
          });
          // isProcessing stays true — new turn starting
          break;
        }

        case 'error': {
          setIsProcessing(false);
          hasInFlightTurnRef.current = false;
          compactingRef.current = false;
          setMessages((prev) => [
            ...prev,
            { role: 'error', content: data.message ?? 'Unknown error' },
          ]);
          break;
        }

        default:
          break;
      }
    });
  }, [sessionId]);

  // Connect / disconnect when sessionId changes
  useEffect(() => {
    // Reset state on new session
    setMessages([]);
    setAssumptions([]);
    setStatus('idle');
    setIsConnected(false);
    setIsProcessing(false);
    setNotebookReady(false);
    streamingRef.current = null;
    compactingRef.current = false;
    hasInFlightTurnRef.current = false;
    retriesRef.current = 0;
    clearReconnectTimer();

    if (!sessionId) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }

    connect();

    return () => {
      clearReconnectTimer();
      retriesRef.current = 0;
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sessionId, connect, clearReconnectTimer]);

  const sendMessage = useCallback(
    (content, { model } = {}) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Add the user message to local state immediately (optimistic)
        setMessages((prev) => [...prev, { role: 'user', content }]);
        setIsProcessing(true);
        // Mark turn in flight — reconnect 'history' replays must not
        // clobber the optimistic state until message_complete fires.
        hasInFlightTurnRef.current = true;
        const payload = { type: 'message', content };
        if (model) payload.model = model;
        wsRef.current.send(JSON.stringify(payload));
      }
    },
    [],
  );

  const stopAgent = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'stop' }));
    }
  }, []);

  const interruptAgent = useCallback(
    (content, { model } = {}) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        setMessages((prev) => [...prev, { role: 'user', content }]);
        setIsProcessing(true);
        hasInFlightTurnRef.current = true;
        const payload = { type: 'interrupt', content };
        if (model) payload.model = model;
        wsRef.current.send(JSON.stringify(payload));
      }
    },
    [],
  );

  return { messages, assumptions, status, isConnected, isProcessing, sendMessage, stopAgent, interruptAgent, notebookReady };
}

export default useAgentSession;
