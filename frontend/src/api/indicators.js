// Indicators API helpers.
//
// Discovery for the Indicators page must reuse the SAME /api/data/*
// endpoints the Data page already uses — do NOT invent a parallel
// backend resolver here. If the set of supported index symbols ever
// widens, update the matcher below, not the backend.

import { API_BASE } from './base';
import { listCollections, listInstruments } from './data';

/**
 * POST an indicator-compute request and return the parsed response.
 *
 * Mirrors ``computeSignal`` in ``api/signals.js``: on a non-2xx, the
 * parsed JSON body is attached to the thrown Error as ``err.body`` and
 * the HTTP status as ``err.status``. Network errors propagate untouched
 * so the caller can classify via ``utils/fetchError``.
 *
 * Wire format: body is ``{code, params, series, asset_type?,
 * compatible_asset_types?, start?, end?}`` posted to
 * ``/api/indicators/compute``.
 *
 * ``asset_type`` and ``compatible_asset_types`` are optional — when
 * supplied the backend cross-checks them and may reject with HTTP 422
 * + ``error_code: 'INDICATOR_INCOMPATIBLE_ASSET'``. Omit them to keep
 * the legacy code-only request shape (e.g. ad-hoc compute calls that
 * have no indicator-registry context).
 *
 * ``start`` / ``end`` are ISO ``YYYY-MM-DD`` date strings. Required by
 * the ``option_stream`` resolver (which iterates per business day);
 * ignored by spot/continuous resolvers. Omit when no option_stream
 * series is involved to keep the request shape minimal.
 */
export async function computeIndicator(
  { code, params, series, asset_type, compatible_asset_types, start, end },
  { signal, onProgress } = {},
) {
  const body = { code, params, series };
  // Only attach when explicitly provided so we don't bait the backend
  // compatibility check on requests that have no registry context.
  if (typeof asset_type === 'string' && asset_type) {
    body.asset_type = asset_type;
  }
  if (Array.isArray(compatible_asset_types)) {
    body.compatible_asset_types = compatible_asset_types;
  }
  // ISO date range — only forward when both ends are populated.
  if (typeof start === 'string' && start && typeof end === 'string' && end) {
    body.start = start;
    body.end = end;
  }

  // Progress polling: when ``onProgress`` is supplied, generate a
  // task_id, attach it to the request, and poll
  // ``/api/indicators/progress/{task_id}`` every 250 ms while the
  // compute is running. The backend only registers the task when the
  // request involves an option_stream ref (the slow path) — otherwise
  // the poll always returns zeros. Cleanup happens server-side via a
  // BackgroundTask after the main response is sent.
  //
  // Every poll calls ``onProgress`` regardless of fraction value so the
  // UI can reflect "polling is alive at 0%" vs "no polling at all".
  // The first poll fires immediately (no 250 ms warm-up gap) so users
  // see a number as soon as the backend has registered the task.
  let pollTimer = null;
  if (typeof onProgress === 'function') {
    body.task_id = makeTaskId();
    const pollOnce = async () => {
      try {
        const r = await fetch(`${API_BASE}/indicators/progress/${body.task_id}`, { signal });
        if (!r.ok) return;
        const data = await r.json();
        const frac = typeof data.fraction === 'number' ? data.fraction : 0;
        onProgress(frac);
      } catch {
        // Silent: poll errors must not fail the main request. The
        // progress UI just stops updating; the compute itself still
        // completes via the main fetch below.
      }
    };
    // Kick off an immediate poll so the user gets feedback within the
    // first ~tens of ms rather than waiting a full interval.
    pollOnce();
    pollTimer = setInterval(pollOnce, 250);
  }

  try {
    const res = await fetch(`${API_BASE}/indicators/compute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => null);
      const err = new Error((errBody && errBody.message) || res.statusText || 'Request failed');
      err.body = errBody;
      err.status = res.status;
      throw err;
    }
    return res.json();
  } finally {
    if (pollTimer !== null) clearInterval(pollTimer);
  }
}

// crypto.randomUUID() is widely supported (Chrome 92+, Firefox 95+,
// Safari 15.4+, jsdom 22+); the manual fallback is purely defensive
// for ancient runtimes. The collision domain is per-session so even
// the weak fallback is acceptable.
function makeTaskId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Case-insensitive loose matcher for S&P 500 spot-index symbols across
// vendors. Known variants seen in the wild / in our own DB:
//   ^GSPC, .GSPC, GSPC, SPX, .SPX, SP500, S&P500, S&P 500, SP_500,
//   IND_SP_500 (our own MongoDB deployment uses this prefix form).
// We normalise by uppercasing and stripping whitespace + underscores,
// then check for ``SP500`` / ``GSPC`` / ``SPX`` / literal ``S&P500``.
// Intentionally permissive — being overly strict is what caused the
// iter-1 bug where ``IND_SP_500`` was not recognised at all.
export function isSnpSymbol(rawSymbol) {
  if (!rawSymbol) return false;
  const upper = String(rawSymbol).toUpperCase();
  const squashed = upper.replace(/[\s_]/g, '');
  if (squashed.includes('GSPC')) return true;
  if (squashed.includes('SPX')) return true;
  if (squashed.includes('SP500')) return true;
  // ``S&P500`` after stripping whitespace+underscore.
  if (squashed.includes('S&P500')) return true;
  // Defensive last-resort: symbol mentions both ``S&P`` and ``500``.
  if (upper.includes('S&P') && upper.includes('500')) return true;
  return false;
}

/**
 * Resolve the default index instrument to pre-select in the Indicators
 * page (typically the S&P 500 spot index).
 *
 * Walks every INDEX collection returned by the backend, scanning its
 * instruments for a symbol that matches the loose S&P 500 heuristic.
 * First match wins.
 *
 * Returns: { ok: true, data: {collection, instrument_id, symbol} | null }
 *        | { ok: false, error: {kind, title, message} }
 *
 * The ``ok:false`` branch surfaces the classified fetch error so the
 * page can render a meaningful banner ("offline", "can't reach server",
 * etc.) instead of silently falling through to the "pick manually" message.
 */
export async function resolveDefaultIndexInstrument() {
  // Use the same discovery pattern as the Data page's CategoryBrowser —
  // fetch ALL collections (no server-side filter) and intersect
  // client-side with the hardcoded INDEX bucket.
  let allCollections;
  try {
    allCollections = await listCollections();
  } catch (err) {
    return {
      ok: false,
      error: {
        kind: err?.kind || 'unknown',
        title: err?.title || 'Unexpected error',
        message: err?.message || 'Failed to load collections',
      },
    };
  }
  if (!allCollections || allCollections.length === 0) {
    return { ok: true, data: null };
  }
  const indexCollections = allCollections.filter((c) => c === 'INDEX');
  if (indexCollections.length === 0) {
    return { ok: true, data: null };
  }

  for (const collection of indexCollections) {
    let page;
    try {
      // 500 is the MongoDB-side cap; a single page is enough in practice.
      page = await listInstruments(collection, { skip: 0, limit: 500 });
    } catch (err) {
      // Don't hard-fail the whole resolve if a single collection errors —
      // but if it's an offline/network kind, surface it.
      if (err?.kind === 'offline' || err?.kind === 'network') {
        return {
          ok: false,
          error: { kind: err.kind, title: err.title, message: err.message },
        };
      }
      continue;
    }
    const items = (page && page.items) || [];
    for (const inst of items) {
      if (isSnpSymbol(inst.symbol)) {
        return {
          ok: true,
          data: {
            collection,
            instrument_id: inst.symbol,
            symbol: inst.symbol,
          },
        };
      }
    }
  }
  return { ok: true, data: null };
}
