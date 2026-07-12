// Local IndexedDB cache of portfolio-compute results, keyed by the SHA-256 of
// the canonicalized /portfolio/compute request body (see computeCacheKey.js).
//
// SAFETY CONTRACT: caching is best-effort acceleration only. EVERY op is wrapped
// so any IndexedDB/quota/unavailable error degrades to a silent no-op / miss —
// it must NEVER throw into the compute path or block a compute. When IndexedDB
// is unavailable (e.g. private-mode, jsdom, disabled), reads miss and writes are
// dropped, and the caller falls through to a normal compute.
//
// Eviction: capped at MAX_ENTRIES by WRITE recency (``cachedAt`` set on put).
// The store is a pure content-addressed cache, so bounding it to the most
// recently written entries is sufficient; we deliberately avoid access-time
// touches (they would require issuing a write mid-read-transaction, which race
// against IndexedDB's auto-commit-on-await).

const DB_NAME = 'tcg-portfolio-cache';
const DB_VERSION = 1;
const STORE = 'results';
export const MAX_ENTRIES = 50;

function idbImpl() {
  try {
    return typeof indexedDB !== 'undefined' && indexedDB ? indexedDB : null;
  } catch {
    return null;
  }
}

function openDb() {
  return new Promise((resolve, reject) => {
    const impl = idbImpl();
    if (!impl) {
      reject(new Error('IndexedDB unavailable'));
      return;
    }
    let req;
    try {
      req = impl.open(DB_NAME, DB_VERSION);
    } catch (e) {
      reject(e);
      return;
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'key' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error('open failed'));
  });
}

// Promisify a single IDBRequest. Only awaited as the LAST request in a
// transaction (never issue a new request after awaiting one — that races IDB
// auto-commit).
function reqPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('request failed'));
  });
}

// Run ``fn(store)`` inside a fresh transaction, then wait for the transaction to
// commit. Opens/closes a connection per call — simple and correct; compute is
// infrequent so the overhead is irrelevant.
async function withStore(mode, fn) {
  const db = await openDb();
  try {
    const tx = db.transaction(STORE, mode);
    const store = tx.objectStore(STORE);
    const result = await fn(store);
    await new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error('transaction failed'));
      tx.onabort = () => reject(tx.error || new Error('transaction aborted'));
    });
    return result;
  } finally {
    try {
      db.close();
    } catch {
      // ignore
    }
  }
}

/** Choose which keys to drop so the store holds at most ``cap`` entries,
 *  evicting the oldest (smallest ``cachedAt``) first. Pure — unit-tested. */
export function selectEvictionVictims(records, cap) {
  if (!Array.isArray(records) || records.length <= cap) return [];
  const sorted = [...records].sort((a, b) => (a.cachedAt || 0) - (b.cachedAt || 0));
  const excess = sorted.length - cap;
  return sorted.slice(0, excess).map((r) => r.key);
}

/** True iff a result is cached for ``key``. Never throws; misses on any error. */
export async function hasCached(key) {
  if (!key) return false;
  try {
    return await withStore('readonly', async (store) => {
      const rec = await reqPromise(store.get(key));
      return !!rec;
    });
  } catch {
    return false;
  }
}

/** The cached result for ``key`` or null. Never throws; null on any error. */
export async function getCached(key) {
  if (!key) return null;
  try {
    return await withStore('readonly', async (store) => {
      const rec = await reqPromise(store.get(key));
      return rec ? rec.result : null;
    });
  } catch {
    return null;
  }
}

/** Store ``result`` under ``key`` and prune to MAX_ENTRIES. Never throws;
 *  returns false on any error (the compute still succeeded, just uncached). */
export async function putCached(key, portfolioId, result) {
  if (!key) return false;
  try {
    await withStore('readwrite', (store) => {
      store.put({
        key,
        portfolioId: portfolioId ?? null,
        result,
        cachedAt: Date.now(),
      });
    });
    await evict();
    return true;
  } catch {
    return false;
  }
}

// Best-effort pruning in its own transactions (read, then delete) so no write
// is issued after awaiting a read within one transaction.
async function evict() {
  try {
    const all = await withStore('readonly', (store) => reqPromise(store.getAll()));
    const victims = selectEvictionVictims(all, MAX_ENTRIES);
    if (victims.length === 0) return;
    await withStore('readwrite', (store) => {
      for (const k of victims) store.delete(k);
    });
  } catch {
    // Eviction is best-effort; a full store never breaks reads/writes.
  }
}

/** Empty the store. Never throws; returns false on any error. */
export async function clearCache() {
  try {
    await withStore('readwrite', (store) => {
      store.clear();
    });
    return true;
  } catch {
    return false;
  }
}

/** Number of cached entries (0 on any error). Used by tests and diagnostics. */
export async function cacheSize() {
  try {
    const all = await withStore('readonly', (store) => reqPromise(store.getAll()));
    return Array.isArray(all) ? all.length : 0;
  } catch {
    return 0;
  }
}
