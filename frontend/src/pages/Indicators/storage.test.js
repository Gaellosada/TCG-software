import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadState, saveState, STORAGE_KEY, SCHEMA_VERSION } from './storage';

// Minimal in-memory localStorage stub. Installed via ``vi.stubGlobal``
// because the Vitest node env does not ship a DOM.
function createStorageStub() {
  const store = new Map();
  return {
    getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
    removeItem: vi.fn((k) => { store.delete(k); }),
    clear: vi.fn(() => { store.clear(); }),
    _store: store,
  };
}

let storage;

beforeEach(() => {
  storage = createStorageStub();
  vi.stubGlobal('localStorage', storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('loadState', () => {
  it('returns empty state when nothing persisted', () => {
    expect(loadState()).toEqual({ indicators: [], defaultState: {} });
  });

  it('returns empty state when JSON is malformed', () => {
    storage.setItem(STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ indicators: [], defaultState: {} });
  });

  it('returns empty state when schema version does not match', () => {
    storage.setItem(STORAGE_KEY, JSON.stringify({
      version: 999,
      indicators: [{ id: 'x', name: 'X', code: '', params: {}, seriesMap: {} }],
      defaultState: {},
    }));
    expect(loadState()).toEqual({ indicators: [], defaultState: {} });
  });

  it('round-trips a saved state', () => {
    const state = {
      indicators: [
        {
          id: 'i1',
          name: 'My ind',
          code: "def compute(series):\n    return series['price']",
          params: { window: 20 },
          seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } },
        },
      ],
      defaultState: {
        'sma-20': {
          params: { window: 30 },
          seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } },
        },
      },
    };
    saveState(state);
    const loaded = loadState();
    expect(loaded).toEqual(state);
  });

  it('sanitises indicators missing id', () => {
    storage.setItem(STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      indicators: [
        { name: 'no id' },
        { id: 'ok', name: 'ok', code: '', params: {}, seriesMap: {} },
      ],
      defaultState: {},
    }));
    const out = loadState();
    expect(out.indicators).toHaveLength(1);
    expect(out.indicators[0].id).toBe('ok');
  });

  it('strips any readonly entries from the persisted indicators list', () => {
    storage.setItem(STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      indicators: [
        { id: 'sma-20', name: '20-day SMA', code: '', params: {}, seriesMap: {}, readonly: true },
        { id: 'user1', name: 'User', code: '', params: {}, seriesMap: {} },
      ],
      defaultState: {},
    }));
    const out = loadState();
    expect(out.indicators.map((i) => i.id)).toEqual(['user1']);
  });

  it('tolerates non-object defaultState entries', () => {
    storage.setItem(STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      indicators: [],
      defaultState: { 'sma-20': null, 'sma-10': 'garbage', 'sma-5': { params: { w: 1 } } },
    }));
    const out = loadState();
    expect(Object.keys(out.defaultState)).toEqual(['sma-5']);
    expect(out.defaultState['sma-5'].params).toEqual({ w: 1 });
  });

  it('returns empty state when localStorage is absent', () => {
    vi.unstubAllGlobals();
    vi.stubGlobal('localStorage', undefined);
    expect(loadState()).toEqual({ indicators: [], defaultState: {} });
  });

  it('returns empty state when getItem throws', () => {
    storage.getItem.mockImplementation(() => { throw new Error('blocked'); });
    expect(loadState()).toEqual({ indicators: [], defaultState: {} });
  });
});

describe('saveState', () => {
  it('strips readonly entries before writing', () => {
    saveState({
      indicators: [
        { id: 'sma-20', name: '20-day SMA', code: 'x', params: {}, seriesMap: {}, readonly: true },
        { id: 'user1', name: 'User', code: 'y', params: {}, seriesMap: {} },
      ],
      defaultState: {},
    });
    const raw = storage.getItem(STORAGE_KEY);
    const parsed = JSON.parse(raw);
    expect(parsed.version).toBe(SCHEMA_VERSION);
    expect(parsed.indicators.map((i) => i.id)).toEqual(['user1']);
  });

  it('persists defaultState separately from indicators[]', () => {
    saveState({
      indicators: [],
      defaultState: {
        'sma-20': {
          params: { window: 50 },
          seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } },
        },
      },
    });
    const parsed = JSON.parse(storage.getItem(STORAGE_KEY));
    expect(parsed.defaultState['sma-20'].params).toEqual({ window: 50 });
    expect(parsed.indicators).toEqual([]);
  });

  it('tolerates missing fields on the input object', () => {
    expect(() => saveState({})).not.toThrow();
    const parsed = JSON.parse(storage.getItem(STORAGE_KEY));
    expect(parsed.version).toBe(SCHEMA_VERSION);
    expect(parsed.indicators).toEqual([]);
    expect(parsed.defaultState).toEqual({});
  });

  it('does not throw when setItem throws', () => {
    storage.setItem.mockImplementation(() => { throw new Error('quota'); });
    expect(() => saveState({ indicators: [], defaultState: {} })).not.toThrow();
  });
});

