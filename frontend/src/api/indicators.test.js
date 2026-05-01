import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the data helpers — resolveDefaultIndexInstrument calls
// ``listCollections()`` (no args) and ``listInstruments(collection)``.
// The mocks are redefined per test via ``mockImplementation``.
vi.mock('./data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
}));

import { resolveDefaultIndexInstrument, isSnpSymbol, computeIndicator } from './indicators';
import { listCollections, listInstruments } from './data';

describe('isSnpSymbol', () => {
  it('matches common S&P 500 variants', () => {
    expect(isSnpSymbol('^GSPC')).toBe(true);
    expect(isSnpSymbol('.GSPC')).toBe(true);
    expect(isSnpSymbol('GSPC')).toBe(true);
    expect(isSnpSymbol('SPX')).toBe(true);
    expect(isSnpSymbol('.SPX')).toBe(true);
    expect(isSnpSymbol('SP500')).toBe(true);
    expect(isSnpSymbol('SP_500')).toBe(true);
    expect(isSnpSymbol('IND_SP_500')).toBe(true);
    expect(isSnpSymbol('S&P 500')).toBe(true);
    expect(isSnpSymbol('S&P500')).toBe(true);
    expect(isSnpSymbol('sp500')).toBe(true);
  });

  it('rejects unrelated symbols', () => {
    expect(isSnpSymbol('NDX')).toBe(false);
    expect(isSnpSymbol('DJI')).toBe(false);
    expect(isSnpSymbol('ETF_SPY')).toBe(false);
    expect(isSnpSymbol('')).toBe(false);
    expect(isSnpSymbol(null)).toBe(false);
    expect(isSnpSymbol(undefined)).toBe(false);
  });
});

describe('resolveDefaultIndexInstrument', () => {
  beforeEach(() => {
    vi.mocked(listCollections).mockReset();
    vi.mocked(listInstruments).mockReset();
  });

  it('resolves IND_SP_500 from INDEX collection with mixed symbols', async () => {
    vi.mocked(listCollections).mockResolvedValueOnce(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockResolvedValueOnce({
      items: [
        { symbol: 'NDX' },
        { symbol: 'IND_SP_500' },
        { symbol: 'DJI' },
      ],
      total: 3,
      skip: 0,
      limit: 500,
    });

    const out = await resolveDefaultIndexInstrument();
    expect(out).toEqual({
      ok: true,
      data: {
        collection: 'INDEX',
        instrument_id: 'IND_SP_500',
        symbol: 'IND_SP_500',
      },
    });
    // Crucially, listCollections must be called WITHOUT an asset_class arg —
    // the server rejects uppercase enum values with a 400.
    expect(listCollections).toHaveBeenCalledWith();
    expect(listInstruments).toHaveBeenCalledWith('INDEX', expect.anything());
  });

  it('returns {ok:true, data:null} when no INDEX instrument matches', async () => {
    vi.mocked(listCollections).mockResolvedValueOnce(['INDEX']);
    vi.mocked(listInstruments).mockResolvedValueOnce({
      items: [{ symbol: 'SOMETHING_ELSE' }],
      total: 1,
      skip: 0,
      limit: 500,
    });

    const out = await resolveDefaultIndexInstrument();
    expect(out).toEqual({ ok: true, data: null });
  });

  it('surfaces a classified error when listCollections throws', async () => {
    const err = Object.assign(new Error('server explosion'), {
      kind: 'server',
      title: 'Server error',
    });
    vi.mocked(listCollections).mockRejectedValueOnce(err);
    const out = await resolveDefaultIndexInstrument();
    expect(out.ok).toBe(false);
    expect(out.error.kind).toBe('server');
    expect(out.error.title).toBe('Server error');
  });

  it('returns {ok:true, data:null} when INDEX collection is absent', async () => {
    vi.mocked(listCollections).mockResolvedValueOnce(['ETF', 'FUT_ES']);
    const out = await resolveDefaultIndexInstrument();
    expect(out).toEqual({ ok: true, data: null });
    expect(listInstruments).not.toHaveBeenCalled();
  });

  it('surfaces offline error from a failing listInstruments', async () => {
    vi.mocked(listCollections).mockResolvedValueOnce(['INDEX']);
    vi.mocked(listInstruments).mockRejectedValueOnce(
      Object.assign(new Error('offline'), { kind: 'offline', title: 'Offline' }),
    );
    const out = await resolveDefaultIndexInstrument();
    expect(out.ok).toBe(false);
    expect(out.error.kind).toBe('offline');
  });

  it('continues past a non-network listInstruments failure', async () => {
    vi.mocked(listCollections).mockResolvedValueOnce(['INDEX']);
    vi.mocked(listInstruments).mockRejectedValueOnce(
      Object.assign(new Error('oops'), { kind: 'client' }),
    );
    const out = await resolveDefaultIndexInstrument();
    expect(out).toEqual({ ok: true, data: null });
  });
});

describe('computeIndicator', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function makeFetchMock(response) {
    const fn = vi.fn().mockResolvedValue(response);
    globalThis.fetch = fn;
    return fn;
  }

  function jsonOk(body) {
    return {
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    };
  }

  function jsonError(status, body) {
    return {
      ok: false,
      status,
      statusText: 'Bad',
      json: () => Promise.resolve(body),
    };
  }

  it('forwards code/params/series in the JSON body', async () => {
    const fetchMock = makeFetchMock(jsonOk({ ok: true }));
    await computeIndicator({
      code: 'def compute(...)',
      params: { window: 20 },
      series: { close: { type: 'spot', collection: 'INDEX', instrument_id: 'IND_SP_500' } },
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0];
    const parsed = JSON.parse(init.body);
    expect(parsed.code).toBe('def compute(...)');
    expect(parsed.params).toEqual({ window: 20 });
    expect(parsed.series.close.collection).toBe('INDEX');
    // Optional fields absent → not on the wire body.
    expect(parsed).not.toHaveProperty('asset_type');
    expect(parsed).not.toHaveProperty('compatible_asset_types');
  });

  it('forwards asset_type and compatible_asset_types when provided', async () => {
    const fetchMock = makeFetchMock(jsonOk({ ok: true }));
    await computeIndicator({
      code: 'x',
      params: {},
      series: {},
      asset_type: 'option',
      compatible_asset_types: ['option'],
    });
    const [, init] = fetchMock.mock.calls[0];
    const parsed = JSON.parse(init.body);
    expect(parsed.asset_type).toBe('option');
    expect(parsed.compatible_asset_types).toEqual(['option']);
  });

  it('omits asset_type when it is an empty string or non-string', async () => {
    const fetchMock = makeFetchMock(jsonOk({ ok: true }));
    await computeIndicator({
      code: 'x',
      params: {},
      series: {},
      asset_type: '',
      compatible_asset_types: undefined,
    });
    const [, init] = fetchMock.mock.calls[0];
    const parsed = JSON.parse(init.body);
    expect(parsed).not.toHaveProperty('asset_type');
    expect(parsed).not.toHaveProperty('compatible_asset_types');
  });

  it('throws with err.body and err.status on non-2xx', async () => {
    const errorBody = {
      error_code: 'INDICATOR_INCOMPATIBLE_ASSET',
      indicator_id: 'atm-iv',
      asset_type: 'index',
      accepted_asset_types: ['option'],
      message: 'Indicator not compatible',
    };
    makeFetchMock(jsonError(422, errorBody));
    let caught;
    try {
      await computeIndicator({ code: 'x', params: {}, series: {} });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeTruthy();
    expect(caught.status).toBe(422);
    expect(caught.body).toEqual(errorBody);
    expect(caught.message).toBe('Indicator not compatible');
  });
});
