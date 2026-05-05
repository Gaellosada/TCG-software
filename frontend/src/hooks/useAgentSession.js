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
      setIsProcessing(false);
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
          // Restore prior conversation on reconnect.
          // Backend stores messages in Anthropic API format where assistant
          // content is an array of blocks. Transform to display format.
          if (Array.isArray(data.messages)) {
            setMessages(transformHistory(data.messages));
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
          // Current turn cancelled, new one starting
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
    (content, { model } = {}) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Add the user message to local state immediately (optimistic)
        setMessages((prev) => [...prev, { role: 'user', content }]);
        setIsProcessing(true);
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
