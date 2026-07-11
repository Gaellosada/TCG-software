// Content-addressed cache key for a /portfolio/compute request body.
//
// The compute endpoint is a pure function of its request body (the body inlines
// the full signal specs AND resolved indicator specs, so it captures the entire
// dependency graph). A canonical SHA-256 of that body is therefore a complete,
// leak-proof cache key: any edit to the portfolio, a signal it uses, or an
// indicator those signals use necessarily changes the body → new key.

/**
 * Recursively sort object keys so the serialization is independent of insertion
 * order (defense-in-depth: insertion order is already stable upstream, but a
 * canonical form makes the key provably order-invariant). Arrays keep their
 * order (order is semantically meaningful — e.g. rebalance dates, leg lists).
 * ``undefined`` values are dropped, matching JSON.stringify / the wire body
 * (api/portfolio.js sends ``start: start || undefined``, which JSON omits).
 */
export function canonicalize(value) {
  if (Array.isArray(value)) {
    return value.map((v) => canonicalize(v));
  }
  if (value && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      const canon = canonicalize(value[key]);
      if (canon !== undefined) out[key] = canon;
    }
    return out;
  }
  // Primitives (and undefined) pass through; undefined is dropped by the caller.
  return value;
}

/**
 * Compute a hex SHA-256 of the canonicalized body object. Deterministic and
 * key-order-invariant: two bodies that differ only in object-key insertion
 * order produce the same key; any semantic difference produces a different key.
 */
export async function computeCacheKey(bodyObj) {
  const canonical = JSON.stringify(canonicalize(bodyObj));
  const bytes = new TextEncoder().encode(canonical);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const view = new Uint8Array(digest);
  let hex = '';
  for (let i = 0; i < view.length; i++) {
    hex += view[i].toString(16).padStart(2, '0');
  }
  return hex;
}
