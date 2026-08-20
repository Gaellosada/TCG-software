import { fetchApi } from './client';
import { classifyFetchError, FetchError } from '../utils/fetchError';

// ---------------------------------------------------------------------------
// Intraday options backtest API client.
//
// Mirrors the style of ``options.js`` / ``portfolio.js``: thin functions over
// ``fetchApi`` that re-throw network/HTTP failures as a classified
// ``FetchError`` so callers get a friendly ``kind`` + message. The wire shapes
// are PINNED in ``workspace/tasks/intraday-options-backtest/output/DESIGN.md``.
// ---------------------------------------------------------------------------

// Re-throw any error as a classified ``FetchError`` (preserving AbortError).
// Local copy — identical to the helper in ``options.js`` so neither module
// depends on the other.
function rethrowClassified(err) {
  if (err && err.name === 'AbortError') throw err;
  if (err && err.name === 'ApiError') {
    if (err.errorType === 'network_error') {
      const classified = classifyFetchError(new TypeError(err.message));
      throw new FetchError({ ...classified, cause: err });
    }
    const status = (err.details && err.details.status)
      || (err.errorType === 'not_found' ? 404 : null)
      || (err.errorType === 'validation' ? 400 : null)
      || (err.errorType === 'server_error' ? 500 : null)
      || null;
    if (status) {
      const classified = classifyFetchError(null, { status }, err.message);
      throw new FetchError({ ...classified, cause: err });
    }
  }
  const classified = classifyFetchError(err);
  throw new FetchError({ ...classified, cause: err });
}

// ---------------------------------------------------------------------------
// GET /api/intraday-backtest/meta
//   → { window:{min_date,max_date}, expiry_modes:[...], roots:[...],
//       hedge_instrument, multiplier, timezone }
// ---------------------------------------------------------------------------
export async function getIntradayBacktestMeta({ signal } = {}) {
  try {
    return await fetchApi('/intraday-backtest/meta', signal ? { signal } : {});
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// GET /api/intraday-backtest/event-calendar
//   → { event_types:[...], events:{FOMC:[{date,tentative}],NFP,CPI},
//       all_dates:[...], tentative_dates:[...] }
// The curated static macro event dates (F3.1) backing the date-allowlist /
// event-day controls (and the A3 attribution view). No dwh — always available.
// ---------------------------------------------------------------------------
export async function getIntradayEventCalendar({ signal } = {}) {
  try {
    return await fetchApi(
      '/intraday-backtest/event-calendar',
      signal ? { signal } : {},
    );
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// POST /api/intraday-backtest/run
//   body: the PINNED request schema (see DESIGN.md).
//   → per-day + aggregate + warnings response.
// ---------------------------------------------------------------------------
export async function runIntradayBacktest(params, { signal } = {}) {
  try {
    return await fetchApi('/intraday-backtest/run', {
      method: 'POST',
      body: JSON.stringify(params),
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// POST /api/intraday-backtest/run-async
//   body: the PINNED request schema (identical to /run).
//   → { job_id } — start a background run; poll progress below.
// Validation errors (out-of-window, T2<=T1) still 400 synchronously here.
// ---------------------------------------------------------------------------
export async function startIntradayBacktest(params, { signal } = {}) {
  try {
    return await fetchApi('/intraday-backtest/run-async', {
      method: 'POST',
      body: JSON.stringify(params),
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// GET /api/intraday-backtest/progress/{jobId}
//   → { status: 'running'|'done'|'error', days_done, total_days,
//       result: <full /run response when done, else null>, error }
// ---------------------------------------------------------------------------
export async function getIntradayBacktestProgress(jobId, { signal } = {}) {
  try {
    return await fetchApi(
      `/intraday-backtest/progress/${encodeURIComponent(jobId)}`,
      signal ? { signal } : {},
    );
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// POST /api/intraday-backtest/cache/get
//   body: the RunRequest JSON (the SAME body ``buildPayload`` builds, so the
//         backend key matches a prior /run of the same config).
//   → HIT: the full result object (params_echo / window / days / aggregate /
//          warnings / from_cache:true) — SAME shape the /progress ``result``
//          carries, so it feeds the identical rendering path.
//     MISS: ``{ cached: false }`` (HTTP 200) — read-only, never computes.
// Mirrors ``portfolio.getPortfolioCachedResult``. Backs the auto-display on
// Load: a HIT renders the equity curve + metrics instantly; a MISS leaves the
// results empty (the user clicks Run).
// ---------------------------------------------------------------------------
export async function getIntradayBacktestCachedResult(params, { signal } = {}) {
  try {
    return await fetchApi('/intraday-backtest/cache/get', {
      method: 'POST',
      body: JSON.stringify(params),
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// POST /api/intraday-backtest/cache/status
//   body: the RunRequest JSON (same body as above).
//   → { cached: bool } — a PURE key lookup (no compute).
// Mirrors ``portfolio.getPortfolioCacheStatus``.
// ---------------------------------------------------------------------------
export async function getIntradayBacktestCacheStatus(params, { signal } = {}) {
  try {
    return await fetchApi('/intraday-backtest/cache/status', {
      method: 'POST',
      body: JSON.stringify(params),
      ...(signal ? { signal } : {}),
    });
  } catch (err) {
    rethrowClassified(err);
  }
}
