import { useState, useEffect, useRef, useCallback } from 'react';
import { getAssumptions } from '../api/agent';

const MAX_RETRIES = 5;
const RECONNECT_DELAY_MS = 3000;
// Issue 12: how often the elapsed-time UI re-renders while a turn is in
// flight. 1 s gives a smooth seconds-counter without thrashing React.
const ELAPSED_TICK_MS = 1000;

/**
 * Humanize a token count for compact UI display (Issue 11).
 * 0..999 → "123"; 1_000..999_999 → "12.3k"; 1_000_000+ → "1.5M".
 * Negative or non-finite inputs are clamped to "0".
 */
export function formatTokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v < 1000) return String(Math.floor(v));
  if (v < 1_000_000) {
    const k = v / 1000;
    // Show one decimal under 100k; integer above for compactness.
    return k < 100 ? `${k.toFixed(1)}k` : `${Math.floor(k)}k`;
  }
  const m = v / 1_000_000;
  return m < 100 ? `${m.toFixed(1)}M` : `${Math.floor(m)}M`;
}

/**
 * Format an elapsed duration in ms to a compact label (Issue 12).
 * < 60 s → "12s"; 60..3599 s → "1m 23s" (zero-padded seconds when minutes
 * are non-zero); ≥ 1 h → "1h 02m".
 */
export function formatElapsed(ms) {
  const v = Number(ms);
  if (!Number.isFinite(v) || v < 0) return '0s';
  const totalSeconds = Math.floor(v / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) {
    return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return `${hours}h ${String(remMinutes).padStart(2, '0')}m`;
}

/**
 * Format elapsed_seconds from turn_complete payload to a compact label (Issue 16b).
 * Delegates to formatElapsed by converting seconds → ms.
 * < 60 s → "12s"; 60+ s → "1m 23s"; 3600+ s → "1h 02m".
 */
export function formatElapsedSeconds(seconds) {
  return formatElapsed(Math.round(Number(seconds) * 1000));
}

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
 *   warningMessage: string|null,
 *   compactBanner: string|null,
 *   processExitInfo: {returncode: int|null, stderrTail: string|null, sessionId: string}|null,
 *   clearProcessExit: () => void,
 *   turnAbortedInfo: {reason: string, hadPartialContent: boolean}|null,
 *   clearTurnAborted: () => void,
 *   subagentCount: number,
 *   tokenUsage: {input: number, output: number, total: number},
 *   elapsedMs: number,
 *   turnStartTimestamp: number|null,
 *   lastTurnComplete: {at: Date, elapsedSeconds: number}|null,
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
  // Transient warning set by oversized_line events; cleared on next non-warning status
  const [warningMessage, setWarningMessage] = useState(null);
  // Transient banner shown briefly after compact_done; auto-clears after 2s
  const [compactBanner, setCompactBanner] = useState(null);
  const compactBannerTimerRef = useRef(null);
  // Non-null when the agent subprocess exited unexpectedly (process_exit event).
  // Shape: { returncode: int|null, stderrTail: string|null, sessionId: string }
  // Cleared by user dismiss (clearProcessExit) or next sendMessage/interruptAgent.
  const [processExitInfo, setProcessExitInfo] = useState(null);
  // Non-null when the BE notified that an in-flight turn was aborted by a
  // WS disconnect (turn_aborted event — Issue 9). Distinct from process_exit:
  // here the subprocess was killed by the BE on connection loss, not by a
  // crash. Shape: { reason: string, hadPartialContent: bool }
  // Cleared by user dismiss (clearTurnAborted) or next sendMessage/interruptAgent.
  const [turnAbortedInfo, setTurnAbortedInfo] = useState(null);
  // Subagent runtime visibility (Issue 10). Stateful: latest count supersedes;
  // 0 means no subagents running.
  const [subagentCount, setSubagentCount] = useState(0);
  // Cumulative session-level token usage (Issue 11). Monotonic non-decreasing.
  const [tokenUsage, setTokenUsage] = useState({ input: 0, output: 0, total: 0 });
  // Elapsed-time visibility (Issue 12). turnStartTimestamp is the wall-clock
  // ms set when the user sends a message; nulled on terminal events. The
  // elapsedMs state ticks every 1s while a turn is in flight so the UI can
  // re-render. We split start-timestamp (ref-like) from elapsedMs (state) so
  // ticking re-renders without recomputing the start.
  const turnStartTimestampRef = useRef(null);
  const [turnStartTimestamp, setTurnStartTimestamp] = useState(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  // Issue 16(b): positive end-of-turn marker (turn_complete event). Non-null
  // when the most recent turn ended cleanly. Shape:
  //   { at: Date, elapsedSeconds: number }
  // Replaced on every new turn_complete event; stays set until next session
  // reset (session switch or explicit sendMessage clears it via session reset).
  const [lastTurnComplete, setLastTurnComplete] = useState(null);

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

  const clearCompactBannerTimer = useCallback(() => {
    if (compactBannerTimerRef.current !== null) {
      clearTimeout(compactBannerTimerRef.current);
      compactBannerTimerRef.current = null;
    }
  }, []);

  const clearProcessExit = useCallback(() => {
    setProcessExitInfo(null);
  }, []);

  const clearTurnAborted = useCallback(() => {
    setTurnAbortedInfo(null);
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
      // Defensive complement to the `turn_aborted` event handler (Issue 9).
      // The BE may emit `turn_aborted` after the next reconnect delivers a
      // `history` payload, but if we keep `hasInFlightTurnRef` latched here
      // the history payload gets dropped (see useAgentSession history-guard
      // at the `case 'history':` branch). Clearing on close means: any
      // reconnect now accepts BE history, and the optional `turn_aborted`
      // event will reinforce the user-visible state with a banner. This is
      // the round-2 design intent updated for round-3 incremental save: BE
      // is source-of-truth on reconnect because partial content is now
      // persisted before the disconnect.
      hasInFlightTurnRef.current = false;
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
          turnStartTimestampRef.current = null;
          setTurnStartTimestamp(null);
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
            // Surface a transient banner with compaction metadata.
            // trigger: 'auto' | 'manual' (undefined falls back to 'auto' label)
            const preTokens = data.pre_tokens;
            const trigger = data.trigger;
            const verb = trigger === 'manual' ? 'Compacted on request' : 'Auto-compacted';
            const tokenLabel = Number.isFinite(Number(preTokens))
              ? ` — ${Math.round(Number(preTokens) / 1000)}k tokens freed`
              : '';
            clearCompactBannerTimer();
            setCompactBanner(`${verb}${tokenLabel}`);
            compactBannerTimerRef.current = setTimeout(() => {
              setCompactBanner(null);
              compactBannerTimerRef.current = null;
            }, 2000);
            break;
          }
          if (compactingRef.current) {
            // Drop heartbeat / idle / oversized noise during compaction.
            break;
          }
          if (next === 'oversized_line') {
            // Surface a human-readable warning instead of the raw event name.
            // The warning is transient (cleared on next non-warning status).
            setWarningMessage('⚠ Line too long — skipped');
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
            setWarningMessage(null);
            setStatus(label);
            break;
          }
          // Clear any stale oversized_line warning when a new normal status arrives
          setWarningMessage(null);
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
          turnStartTimestampRef.current = null;
          setTurnStartTimestamp(null);
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
          turnStartTimestampRef.current = null;
          setTurnStartTimestamp(null);
          setMessages((prev) => [
            ...prev,
            { role: 'error', content: data.message ?? 'Unknown error' },
          ]);
          break;
        }

        case 'process_exit': {
          // Agent subprocess exited without a result (saw_result is always false here).
          // Clear in-flight state so the UI is no longer frozen.
          // Do NOT auto-clear: the banner stays until user dismisses or sends a new message.
          setIsProcessing(false);
          hasInFlightTurnRef.current = false;
          turnStartTimestampRef.current = null;
          setTurnStartTimestamp(null);
          setProcessExitInfo({
            returncode: data.returncode ?? null,
            stderrTail: data.stderr_tail ?? null,
            sessionId: data.session_id,
          });
          break;
        }

        case 'turn_aborted': {
          // Issue 9: BE notifies that the prior in-flight turn was aborted by
          // a WS disconnect. Treated similarly to process_exit but with a
          // distinct banner copy (this is connection-loss, not subprocess
          // crash). Banner stays until user dismisses or sends a new message.
          setIsProcessing(false);
          hasInFlightTurnRef.current = false;
          turnStartTimestampRef.current = null;
          setTurnStartTimestamp(null);
          setTurnAbortedInfo({
            reason: data.reason ?? 'ws_disconnect',
            hadPartialContent: Boolean(data.had_partial_content),
          });
          break;
        }

        case 'subagent_count': {
          // Issue 10: stateful — latest count supersedes. count includes 0
          // (clears the badge). Coerce to non-negative integer; ignore non-
          // numeric payloads.
          const n = Number(data.count);
          if (Number.isFinite(n) && n >= 0) {
            setSubagentCount(Math.floor(n));
          }
          break;
        }

        case 'token_usage': {
          // Issue 11: cumulative session totals. Monotonic non-decreasing per
          // contract; we trust the BE and overwrite. Coerce missing fields to
          // 0 to keep the consumer total-only render path safe.
          const input = Number(data.session_input);
          const output = Number(data.session_output);
          const total = Number(data.session_total);
          setTokenUsage({
            input: Number.isFinite(input) ? input : 0,
            output: Number.isFinite(output) ? output : 0,
            total: Number.isFinite(total) ? total : 0,
          });
          break;
        }

        case 'turn_complete': {
          // Issue 16(b): positive end-of-turn marker. Emitted by BE exactly
          // once after a clean turn (mutually exclusive with process_exit).
          // Store at + elapsedSeconds so AgentPage can render a momentary
          // "Turn complete (Xs)" indicator. Also clears hasInFlightTurnRef
          // defence-in-depth (belt-and-suspenders — message_complete already
          // clears it, but turn_complete is the canonical clean-turn signal).
          const elapsed = Number(data.elapsed_seconds);
          setLastTurnComplete({
            at: new Date(data.timestamp),
            elapsedSeconds: Number.isFinite(elapsed) ? elapsed : 0,
          });
          hasInFlightTurnRef.current = false;
          break;
        }

        default:
          break;
      }
    });
  }, [sessionId, clearCompactBannerTimer]);

  // Connect / disconnect when sessionId changes
  useEffect(() => {
    // Reset state on new session
    setMessages([]);
    setAssumptions([]);
    setStatus('idle');
    setIsConnected(false);
    setIsProcessing(false);
    setNotebookReady(false);
    setWarningMessage(null);
    setCompactBanner(null);
    setProcessExitInfo(null);
    setTurnAbortedInfo(null);
    setSubagentCount(0);
    setTokenUsage({ input: 0, output: 0, total: 0 });
    setTurnStartTimestamp(null);
    setElapsedMs(0);
    setLastTurnComplete(null);
    streamingRef.current = null;
    compactingRef.current = false;
    hasInFlightTurnRef.current = false;
    turnStartTimestampRef.current = null;
    retriesRef.current = 0;
    clearReconnectTimer();
    clearCompactBannerTimer();

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
      clearCompactBannerTimer();
      retriesRef.current = 0;
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sessionId, connect, clearReconnectTimer, clearCompactBannerTimer]);

  // Issue 12: tick elapsed time every ELAPSED_TICK_MS while a turn is in
  // flight. Cleared on unmount AND whenever the conditions go false (turn
  // ends or session reset nulls turnStartTimestamp). The interval reads from
  // the ref so timestamp updates do not require restarting the timer.
  useEffect(() => {
    if (!isProcessing || turnStartTimestamp === null) {
      // Not processing → no ticker. setElapsedMs is intentionally NOT reset
      // here so that terminal handlers can decide whether to freeze or zero
      // the displayed value (we zero it in those handlers explicitly).
      return undefined;
    }
    // Establish initial value immediately so the UI shows "0s" without a 1 s
    // gap.
    setElapsedMs(Date.now() - turnStartTimestamp);
    const intervalId = setInterval(() => {
      const start = turnStartTimestampRef.current;
      if (start === null) return;
      setElapsedMs(Date.now() - start);
    }, ELAPSED_TICK_MS);
    return () => clearInterval(intervalId);
  }, [isProcessing, turnStartTimestamp]);

  const sendMessage = useCallback(
    (content, { model } = {}) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        // Clear any stale process_exit / turn_aborted banner — new user
        // message supersedes them.
        setProcessExitInfo(null);
        setTurnAbortedInfo(null);
        // Add the user message to local state immediately (optimistic)
        setMessages((prev) => [...prev, { role: 'user', content }]);
        setIsProcessing(true);
        // Mark turn in flight — reconnect 'history' replays must not
        // clobber the optimistic state until message_complete fires.
        hasInFlightTurnRef.current = true;
        // Issue 12: start the elapsed-time clock for this turn.
        const now = Date.now();
        turnStartTimestampRef.current = now;
        setTurnStartTimestamp(now);
        setElapsedMs(0);
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
        // Clear any stale process_exit / turn_aborted banner — new user
        // message supersedes them.
        setProcessExitInfo(null);
        setTurnAbortedInfo(null);
        setMessages((prev) => [...prev, { role: 'user', content }]);
        setIsProcessing(true);
        hasInFlightTurnRef.current = true;
        // Issue 12: restart the elapsed clock for the interrupt turn.
        const now = Date.now();
        turnStartTimestampRef.current = now;
        setTurnStartTimestamp(now);
        setElapsedMs(0);
        const payload = { type: 'interrupt', content };
        if (model) payload.model = model;
        wsRef.current.send(JSON.stringify(payload));
      }
    },
    [],
  );

  return {
    messages,
    assumptions,
    status,
    warningMessage,
    compactBanner,
    processExitInfo,
    clearProcessExit,
    turnAbortedInfo,
    clearTurnAborted,
    subagentCount,
    tokenUsage,
    elapsedMs,
    turnStartTimestamp,
    lastTurnComplete,
    isConnected,
    isProcessing,
    sendMessage,
    stopAgent,
    interruptAgent,
    notebookReady,
  };
}


export default useAgentSession;
