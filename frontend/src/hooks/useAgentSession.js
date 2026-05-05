import { useState, useEffect, useRef, useCallback } from 'react';

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

  const wsRef = useRef(null);
  const retriesRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  // Track the current streaming (partial) assistant message
  const streamingRef = useRef(null);

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
    });

    ws.addEventListener('close', () => {
      setIsConnected(false);
      wsRef.current = null;

      if (retriesRef.current < MAX_RETRIES) {
        retriesRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
      }
    });

    ws.addEventListener('error', () => {
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
          // Append token text to the current streaming message (immutable update)
          setMessages((prev) => {
            if (!streamingRef.current) {
              streamingRef.current = true;
              return [...prev, { role: 'assistant', content: data.content ?? '', streaming: true }];
            }
            const last = prev[prev.length - 1];
            const updated = { ...last, content: last.content + (data.content ?? '') };
            return [...prev.slice(0, -1), updated];
          });
          break;
        }

        case 'message_complete': {
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
          // Restore prior conversation on reconnect
          if (Array.isArray(data.messages)) {
            setMessages(data.messages);
          }
          break;
        }

        case 'assumptions_update': {
          setAssumptions(data.assumptions ?? []);
          break;
        }

        case 'status': {
          setStatus(data.status ?? 'idle');
          break;
        }

        case 'notebook_ready': {
          setNotebookReady(true);
          break;
        }

        case 'tool_call': {
          setMessages((prev) => [
            ...prev,
            { role: 'tool', name: data.name, input: data.input },
          ]);
          break;
        }

        case 'error': {
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
    setNotebookReady(false);
    streamingRef.current = null;
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
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sessionId, connect, clearReconnectTimer]);

  const sendMessage = useCallback(
    (content) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Add the user message to local state immediately (optimistic)
        setMessages((prev) => [...prev, { role: 'user', content }]);
        wsRef.current.send(JSON.stringify({ type: 'message', content }));
      }
    },
    [],
  );

  return { messages, assumptions, status, isConnected, sendMessage, notebookReady };
}

export default useAgentSession;
