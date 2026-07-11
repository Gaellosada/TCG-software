// @vitest-environment jsdom
//
// Unit tests for the IndexedDB portfolio-result cache.
//
// jsdom provides NO IndexedDB and the project forbids new npm deps, so the
// real round-trip (put/get/has/clear/evict) is exercised against a small
// in-memory IndexedDB double installed as globalThis.indexedDB, and the
// authoritative real-browser round-trip lives in the Playwright e2e. The
// graceful-fallback path (no IndexedDB) is tested directly by leaving
// globalThis.indexedDB undefined — this is the safety-critical property (a
// cache failure must never break a compute).

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  hasCached,
  getCached,
  putCached,
  clearCache,
  cacheSize,
  selectEvictionVictims,
  MAX_ENTRIES,
} from './portfolioCache';

/* ─────────────────────────── in-memory IDB double ─────────────────────────── */
// Faithful to the tiny subset portfolioCache uses: open/upgrade/success,
// transaction + objectStore, get/put/getAll/delete/clear, and asynchronous
// request + transaction-complete events (queueMicrotask so handlers assigned
// synchronously after the call still fire).

function makeFakeIndexedDB() {
  const dbs = new Map(); // dbName -> Map(storeName -> Map(key -> record))

  function makeStore(dataMap, tx) {
    const request = (execute) => {
      const req = { onsuccess: null, onerror: null, result: undefined, error: null };
      tx._pending += 1;
      queueMicrotask(() => {
        try {
          req.result = execute();
          tx._pending -= 1;
          if (req.onsuccess) req.onsuccess({ target: req });
          tx._maybeComplete();
        } catch (e) {
          tx._pending -= 1;
          req.error = e;
          if (req.onerror) req.onerror({ target: req });
          tx._maybeComplete();
        }
      });
      return req;
    };
    return {
      get: (key) => request(() => dataMap.get(key)),
      put: (record) => request(() => { dataMap.set(record.key, record); return record.key; }),
      getAll: () => request(() => Array.from(dataMap.values())),
      delete: (key) => request(() => { dataMap.delete(key); }),
      clear: () => request(() => { dataMap.clear(); }),
    };
  }

  function makeTransaction(dataMap) {
    const tx = {
      _pending: 0,
      _completed: false,
      _oncompleteFn: null,
      onerror: null,
      onabort: null,
      error: null,
      objectStore: () => makeStore(dataMap, tx),
      _maybeComplete() {
        if (this._pending === 0 && !this._completed) {
          this._completed = true;
          if (this._oncompleteFn) queueMicrotask(() => this._oncompleteFn());
        }
      },
    };
    Object.defineProperty(tx, 'oncomplete', {
      get() { return this._oncompleteFn; },
      set(fn) {
        this._oncompleteFn = fn;
        if (this._completed && fn) queueMicrotask(() => fn());
      },
    });
    return tx;
  }

  return {
    open(name /* , version */) {
      const req = { onupgradeneeded: null, onsuccess: null, onerror: null, result: null };
      const isNew = !dbs.has(name);
      if (isNew) dbs.set(name, new Map());
      const stores = dbs.get(name);
      const db = {
        objectStoreNames: { contains: (s) => stores.has(s) },
        createObjectStore: (s) => { if (!stores.has(s)) stores.set(s, new Map()); },
        transaction: (storeName) => makeTransaction(stores.get(storeName)),
        close: () => {},
      };
      req.result = db;
      queueMicrotask(() => {
        if (isNew && req.onupgradeneeded) req.onupgradeneeded({ target: req });
        if (req.onsuccess) req.onsuccess({ target: req });
      });
      return req;
    },
  };
}

/* ─────────────────────────── selectEvictionVictims ─────────────────────────── */

describe('selectEvictionVictims()', () => {
  it('returns [] when at or under the cap', () => {
    expect(selectEvictionVictims([], 3)).toEqual([]);
    const three = [{ key: 'a', cachedAt: 1 }, { key: 'b', cachedAt: 2 }, { key: 'c', cachedAt: 3 }];
    expect(selectEvictionVictims(three, 3)).toEqual([]);
  });

  it('evicts the oldest (smallest cachedAt) first, keeping cap newest', () => {
    const recs = [
      { key: 'old', cachedAt: 10 },
      { key: 'mid', cachedAt: 20 },
      { key: 'new', cachedAt: 30 },
    ];
    expect(selectEvictionVictims(recs, 2)).toEqual(['old']);
    expect(selectEvictionVictims(recs, 1)).toEqual(['old', 'mid']);
  });

  it('is defensive against a non-array', () => {
    expect(selectEvictionVictims(null, 5)).toEqual([]);
    expect(selectEvictionVictims(undefined, 5)).toEqual([]);
  });
});

/* ─────────────────────────── graceful fallback (no IDB) ─────────────────────────── */

describe('graceful fallback when IndexedDB is unavailable', () => {
  beforeEach(() => {
    // jsdom leaves indexedDB undefined; make it explicit.
    delete globalThis.indexedDB;
  });

  it('hasCached resolves false without throwing', async () => {
    await expect(hasCached('k')).resolves.toBe(false);
  });
  it('getCached resolves null without throwing', async () => {
    await expect(getCached('k')).resolves.toBe(null);
  });
  it('putCached resolves false without throwing', async () => {
    await expect(putCached('k', 'pid', { equity: [1, 2] })).resolves.toBe(false);
  });
  it('clearCache resolves false without throwing', async () => {
    await expect(clearCache()).resolves.toBe(false);
  });
  it('cacheSize resolves 0 without throwing', async () => {
    await expect(cacheSize()).resolves.toBe(0);
  });

  it('does not throw even if indexedDB.open itself throws', async () => {
    globalThis.indexedDB = { open() { throw new Error('SecurityError'); } };
    await expect(hasCached('k')).resolves.toBe(false);
    await expect(getCached('k')).resolves.toBe(null);
    await expect(putCached('k', null, {})).resolves.toBe(false);
    delete globalThis.indexedDB;
  });
});

/* ─────────────────────────── real round-trip (in-memory double) ─────────────────────────── */

describe('round-trip against an in-memory IndexedDB double', () => {
  beforeEach(async () => {
    globalThis.indexedDB = makeFakeIndexedDB();
    await clearCache();
  });
  afterEach(() => {
    delete globalThis.indexedDB;
  });

  it('put → has → get returns the stored result; miss is false/null', async () => {
    expect(await hasCached('missing')).toBe(false);
    expect(await getCached('missing')).toBe(null);

    const result = { portfolio_equity: [1, 1.1, 1.2], dates: ['2020-01-01'] };
    expect(await putCached('key1', 'pf-1', result)).toBe(true);

    expect(await hasCached('key1')).toBe(true);
    expect(await getCached('key1')).toEqual(result);
  });

  it('clearCache empties the store', async () => {
    await putCached('a', null, { x: 1 });
    await putCached('b', null, { x: 2 });
    expect(await cacheSize()).toBe(2);

    expect(await clearCache()).toBe(true);
    expect(await cacheSize()).toBe(0);
    expect(await hasCached('a')).toBe(false);
  });

  it('evicts the oldest entries beyond MAX_ENTRIES', async () => {
    // Write MAX_ENTRIES + 5 distinct keys with strictly increasing cachedAt.
    let t = 1000;
    const realNow = Date.now;
    Date.now = () => t;
    try {
      for (let i = 0; i < MAX_ENTRIES + 5; i++) {
        t += 1; // guarantee unique, increasing cachedAt for deterministic eviction
        // eslint-disable-next-line no-await-in-loop
        await putCached(`k${i}`, null, { i });
      }
    } finally {
      Date.now = realNow;
    }

    expect(await cacheSize()).toBe(MAX_ENTRIES);
    // The 5 oldest keys were evicted; the newest survive.
    expect(await hasCached('k0')).toBe(false);
    expect(await hasCached('k4')).toBe(false);
    expect(await hasCached(`k${MAX_ENTRIES + 4}`)).toBe(true);
  });
});
