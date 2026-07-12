// Decision helper for FIX A (edit-mid-compute race).
//
// A compute is dispatched for a specific config (identified by its cache key).
// If the user edits to a different config while that compute is in flight, the
// landing result is for a NOW-MODIFIED config and must NOT be displayed (the
// intent is "modified → nothing until recompute"). The result is still cached
// (it is valid for its own config), so reverting re-shows it.
//
// This is the pure core of that rule so it can be unit-tested deterministically
// without racing a real compute.

/**
 * @param {Object} p
 * @param {boolean} p.cacheOn    whether the local cache is enabled
 * @param {string|null} p.computeKey  cache key of the config the compute ran for
 * @param {string|null} p.liveKey     cache key of the CURRENT (live) config
 * @returns {boolean} true → display the result; false → drop it (stay blank)
 */
export function shouldDisplayComputeResult({ cacheOn, computeKey, liveKey }) {
  // Cache OFF → today's behavior: always display (no edit-mid-compute concept).
  // computeKey null → the body hash failed / wasn't taken; can't verify, so
  // fall back to displaying (best-effort — never suppress a legitimate result).
  if (!cacheOn || computeKey == null) return true;
  return liveKey === computeKey;
}
