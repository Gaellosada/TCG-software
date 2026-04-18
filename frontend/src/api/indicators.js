// Indicators API helpers.
//
// Discovery for the Indicators page must reuse the SAME /api/data/*
// endpoints the Data page already uses — do NOT invent a parallel
// backend resolver here. If the set of supported index symbols ever
// widens, update the matcher below, not the backend.

import { listCollections, listInstruments } from './data';

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
