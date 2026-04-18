// Classify a fetch/network failure into a structured shape that the UI
// can render meaningfully. This lives next to the other pure utils so
// every ``api/*`` helper can import it without pulling in React.
//
// Two distinct failure kinds feed into this helper:
//   1. ``fetch`` threw — typically a ``TypeError`` from offline/DNS/CORS.
//      In that case pass the raw error as ``err`` and leave ``res``
//      undefined.
//   2. ``fetch`` resolved but ``!res.ok`` — pass the ``Response`` as
//      ``res``. The error object may be undefined or a parsed body.
//
// The function is intentionally side-effect-free so it can be exercised
// exhaustively in tests (see ``fetchError.test.js``).
//
// Returned shape:
//   { kind, title, message }
//     kind    ∈ 'offline' | 'network' | 'not-found' | 'server' | 'client' | 'aborted' | 'unknown'
//     title   short user-facing heading
//     message longer-form explanation, may embed the server/exc message
//
// Note on ``navigator.onLine``: the browser's ``onLine`` flag is loose —
// it reports network-stack availability, not real reachability. We use
// it as a hint ONLY: if it's ``false`` we're confident we're offline; if
// it's ``true`` we still fall through to the ``TypeError`` branch to
// catch the flaky-wifi / captive-portal case.

// ---- Public API ----------------------------------------------------

export class FetchError extends Error {
  constructor({ kind, title, message, cause, status }) {
    super(message);
    this.name = 'FetchError';
    this.kind = kind;
    this.title = title;
    this.cause = cause;
    this.status = status;
  }
}

/**
 * Inspect the failure and produce a classified descriptor.
 *
 * @param {Error|null|undefined} err    — the thrown error, if fetch rejected
 * @param {Response|null|undefined} res — the Response object, if fetch resolved !ok
 * @param {string|null|undefined} serverMessage — optional parsed message from body
 */
export function classifyFetchError(err, res, serverMessage) {
  // 0. User-cancelled request (AbortController). Neutral — callers that
  // care (e.g. unmounted component, superseded request) should ignore
  // and not render anything.
  if (err && (err.name === 'AbortError' || err.code === 20)) {
    return {
      kind: 'aborted',
      title: 'Request cancelled',
      message: 'The request was cancelled before it completed.',
    };
  }

  // 1. Explicit offline wins — cheapest + most actionable.
  if (isOffline()) {
    return {
      kind: 'offline',
      title: 'You appear to be offline',
      message: 'Check your internet connection and try again.',
    };
  }

  // 2. A Response is present → classify by status code.
  if (res && typeof res.status === 'number') {
    const status = res.status;

    // ``status === 0`` is an opaque/blocked response (CORS, mixed-content,
    // request blocked by an extension). It is NOT a real HTTP status —
    // treat it as a network-layer failure so the UI surfaces the right
    // "couldn't reach server" message rather than a confusing "HTTP 0".
    if (status === 0) {
      return {
        kind: 'network',
        title: 'Could not reach the server',
        message: 'The request was blocked or returned an opaque response (status 0).',
        status: 0,
      };
    }
    const sm = (typeof serverMessage === 'string' && serverMessage.trim())
      ? serverMessage.trim()
      : null;

    if (status === 404) {
      return {
        kind: 'not-found',
        title: 'Data not found',
        message: sm || 'The requested resource was not found on the server.',
        status,
      };
    }
    if (status >= 500) {
      return {
        kind: 'server',
        title: 'Server error',
        message: sm ? `${sm} (HTTP ${status})` : `The server returned HTTP ${status}.`,
        status,
      };
    }
    if (status >= 400) {
      return {
        kind: 'client',
        title: 'Request rejected',
        message: sm || `The server rejected the request (HTTP ${status}).`,
        status,
      };
    }
    // Non-ok but not 4xx/5xx (e.g. 3xx redirect leaked through). Treat as unknown.
    return {
      kind: 'unknown',
      title: 'Unexpected response',
      message: sm || `Unexpected HTTP ${status}.`,
      status,
    };
  }

  // 3. No Response — fetch threw.
  if (err instanceof TypeError) {
    return {
      kind: 'network',
      title: 'Could not reach the server',
      message: err.message || 'Network request failed.',
    };
  }

  // 4. Fall-through: anything else we don't recognize.
  const rawMsg = (err && (err.message || String(err))) || 'An unexpected error occurred.';
  return {
    kind: 'unknown',
    title: 'Unexpected error',
    message: rawMsg,
  };
}

// ---- Internals -----------------------------------------------------

function isOffline() {
  // Guard for non-browser envs (node-vitest default without jsdom).
  if (typeof navigator === 'undefined') return false;
  // ``navigator.onLine`` is ``true`` when unknown — only trust the
  // explicit ``false`` signal.
  return navigator.onLine === false;
}
