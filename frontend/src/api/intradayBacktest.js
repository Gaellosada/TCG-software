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
