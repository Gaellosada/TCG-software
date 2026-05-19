import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  CATEGORIES,
  createSignal,
  listSignals,
  getSignal,
  updateSignal,
  archiveSignal,
  createPortfolio,
  listPortfolios,
  getPortfolio,
  updatePortfolio,
  archivePortfolio,
} from './persistence';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

afterEach(() => {
  vi.unstubAllGlobals();
});

function mockFetch(response) {
  const fn = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('fetch', fn);
  return fn;
}

function jsonOk(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function noContent() {
  return { ok: true, status: 204, json: () => Promise.reject(new Error('no body')) };
}

function jsonError(status, body) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: () => Promise.resolve(body),
  };
}

const SIGNAL_FIXTURE = {
  id: 'sig-1',
  type: 'signal',
  name: 'Test Signal',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  inputs: [],
  rules: {},
  settings: {},
  description: '',
};

const PORTFOLIO_FIXTURE = {
  id: 'port-1',
  type: 'portfolio',
  name: 'Test Portfolio',
  category: 'DEV',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [],
  rebalance: 'none',
};

// ---------------------------------------------------------------------------
// CATEGORIES export
// ---------------------------------------------------------------------------

describe('CATEGORIES', () => {
  it('contains exactly the four expected values', () => {
    expect(CATEGORIES).toEqual(['RESEARCH', 'DEV', 'PROD', 'ARCHIVE']);
  });
});

// ---------------------------------------------------------------------------
// Signals — CRUD
// ---------------------------------------------------------------------------

describe('createSignal', () => {
  it('POSTs to /api/persistence/signals with the payload', async () => {
    const fn = mockFetch(jsonOk(SIGNAL_FIXTURE));
    const result = await createSignal({
      id: 'sig-1', name: 'Test Signal', category: 'RESEARCH',
      inputs: [], rules: {}, settings: {}, description: '',
    });
    expect(fn).toHaveBeenCalledOnce();
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/signals');
    expect(init.method).toBe('POST');
    const sent = JSON.parse(init.body);
    expect(sent.id).toBe('sig-1');
    expect(sent.category).toBe('RESEARCH');
    expect(sent.rules).toEqual({});
    expect(result.id).toBe('sig-1');
  });

  it('throws with status on non-2xx', async () => {
    mockFetch(jsonError(422, { detail: 'Validation error' }));
    await expect(createSignal({ id: '', name: '', category: 'RESEARCH' }))
      .rejects.toMatchObject({ status: 422 });
  });
});

describe('listSignals', () => {
  it('GETs /api/persistence/signals?category=RESEARCH', async () => {
    const fn = mockFetch(jsonOk([SIGNAL_FIXTURE]));
    const result = await listSignals('RESEARCH');
    expect(fn).toHaveBeenCalledOnce();
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/signals?category=RESEARCH');
    expect(init.method).toBeUndefined(); // default GET
    expect(result).toHaveLength(1);
    expect(result[0].category).toBe('RESEARCH');
  });

  it('URL-encodes the category parameter', async () => {
    const fn = mockFetch(jsonOk([]));
    await listSignals('ARCHIVE');
    const [url] = fn.mock.calls[0];
    expect(url).toContain('category=ARCHIVE');
  });
});

describe('getSignal', () => {
  it('GETs /api/persistence/signals/{id}', async () => {
    const fn = mockFetch(jsonOk(SIGNAL_FIXTURE));
    const result = await getSignal('sig-1');
    const [url] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/signals/sig-1');
    expect(result.id).toBe('sig-1');
  });

  it('throws 404 when signal does not exist', async () => {
    mockFetch(jsonError(404, { detail: 'signal not found: id=missing' }));
    await expect(getSignal('missing')).rejects.toMatchObject({ status: 404 });
  });
});

describe('updateSignal', () => {
  it('PUTs to /api/persistence/signals/{id} with the full payload', async () => {
    const updated = { ...SIGNAL_FIXTURE, category: 'DEV' };
    const fn = mockFetch(jsonOk(updated));
    const result = await updateSignal('sig-1', {
      name: 'Test Signal', category: 'DEV',
      inputs: [{ id: 'X', instrument: null }],
      rules: { entries: [{ id: 'b1' }], exits: [], resets: [] },
      settings: { dont_repeat: true },
      description: 'doc',
    });
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/signals/sig-1');
    expect(init.method).toBe('PUT');
    const sent = JSON.parse(init.body);
    expect(sent.category).toBe('DEV');
    // CRUCIAL: the full editable state goes over the wire — this is the
    // regression test for the persistence-gap bug.
    expect(sent.rules.entries).toHaveLength(1);
    expect(sent.description).toBe('doc');
    expect(result.category).toBe('DEV');
  });
});

describe('archiveSignal', () => {
  it('DELETEs /api/persistence/signals/{id} and resolves null on 204', async () => {
    const fn = mockFetch(noContent());
    const result = await archiveSignal('sig-1');
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/signals/sig-1');
    expect(init.method).toBe('DELETE');
    expect(result).toBeNull();
  });

  it('throws on non-2xx DELETE', async () => {
    mockFetch(jsonError(404, { detail: 'not found' }));
    await expect(archiveSignal('missing')).rejects.toMatchObject({ status: 404 });
  });
});

// ---------------------------------------------------------------------------
// Portfolios — CRUD
// ---------------------------------------------------------------------------

describe('createPortfolio', () => {
  it('POSTs to /api/persistence/portfolios with the payload', async () => {
    const fn = mockFetch(jsonOk(PORTFOLIO_FIXTURE));
    const result = await createPortfolio({
      id: 'port-1',
      name: 'Test Portfolio',
      category: 'DEV',
      legs: [],
      rebalance: 'none',
    });
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/portfolios');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body).category).toBe('DEV');
    expect(result.id).toBe('port-1');
  });
});

describe('listPortfolios', () => {
  it('GETs /api/persistence/portfolios?category=DEV', async () => {
    const fn = mockFetch(jsonOk([PORTFOLIO_FIXTURE]));
    const result = await listPortfolios('DEV');
    const [url] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/portfolios?category=DEV');
    expect(result).toHaveLength(1);
    expect(result[0].category).toBe('DEV');
  });
});

describe('getPortfolio', () => {
  it('GETs /api/persistence/portfolios/{id}', async () => {
    const fn = mockFetch(jsonOk(PORTFOLIO_FIXTURE));
    const result = await getPortfolio('port-1');
    const [url] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/portfolios/port-1');
    expect(result.id).toBe('port-1');
  });
});

describe('updatePortfolio', () => {
  it('PUTs to /api/persistence/portfolios/{id} with the full payload', async () => {
    const updated = { ...PORTFOLIO_FIXTURE, category: 'PROD' };
    const fn = mockFetch(jsonOk(updated));
    const result = await updatePortfolio('port-1', {
      name: 'Test Portfolio',
      category: 'PROD',
      legs: [{ label: 'L1', type: 'instrument', collection: 'spot_daily', symbol: 'SPY', weight: 100 }],
      rebalance: 'monthly',
    });
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/portfolios/port-1');
    expect(init.method).toBe('PUT');
    const sent = JSON.parse(init.body);
    expect(sent.category).toBe('PROD');
    // Regression: full leg list goes over the wire.
    expect(sent.legs).toHaveLength(1);
    expect(sent.rebalance).toBe('monthly');
    expect(result.category).toBe('PROD');
  });
});

describe('archivePortfolio', () => {
  it('DELETEs /api/persistence/portfolios/{id} and resolves null on 204', async () => {
    const fn = mockFetch(noContent());
    const result = await archivePortfolio('port-1');
    const [url, init] = fn.mock.calls[0];
    expect(url).toBe('/api/persistence/portfolios/port-1');
    expect(init.method).toBe('DELETE');
    expect(result).toBeNull();
  });

  it('throws on non-2xx DELETE', async () => {
    mockFetch(jsonError(404, { detail: 'not found' }));
    await expect(archivePortfolio('missing')).rejects.toMatchObject({ status: 404 });
  });
});
