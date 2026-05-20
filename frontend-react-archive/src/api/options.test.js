import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock ``./client`` so every test controls what ``fetchApi`` returns (or
// throws) without a real HTTP server. ``fetchClassified`` in ``options.js``
// calls ``fetchApi`` internally; mocking the module intercepts all calls.
vi.mock('./client', () => ({
  fetchApi: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(errorType, message, details = null) {
      super(message);
      this.name = 'ApiError';
      this.errorType = errorType;
      this.details = details;
    }
  },
}));

import { fetchApi, ApiError } from './client';
import {
  getOptionRoots,
  getOptionChain,
  getOptionContract,
  selectOption,
  getChainSnapshot,
} from './options';

// ── Helpers ──────────────────────────────────────────────────────────────────

function mockSuccess(payload) {
  vi.mocked(fetchApi).mockResolvedValueOnce(payload);
}

function mockApiError(errorType, message, details = null) {
  const err = new ApiError(errorType, message, details);
  vi.mocked(fetchApi).mockRejectedValueOnce(err);
}

// ── Fixtures ─────────────────────────────────────────────────────────────────

const ROOT_LIST = { roots: [{ collection: 'OPT_SP_500', name: 'S&P 500 Options' }] };
const CHAIN_RESP = { root: 'OPT_SP_500', date: '2024-03-15', rows: [] };
const CONTRACT_RESP = { contract: { contract_id: 'SPY|M' }, rows: [] };
const SELECT_RESP = { contract: { contract_id: 'X' }, matched_value: 0.30, error_code: null };
const SNAPSHOT_RESP = { root: 'OPT_SP_500', date: '2024-03-15', series: [] };

// ── Suite ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.mocked(fetchApi).mockReset();
});

// ---------------------------------------------------------------------------
// 1. getOptionRoots
// ---------------------------------------------------------------------------

describe('getOptionRoots', () => {
  it('fetches GET /api/options/roots and returns parsed JSON', async () => {
    mockSuccess(ROOT_LIST);
    const result = await getOptionRoots();

    expect(result).toEqual(ROOT_LIST);
    expect(fetchApi).toHaveBeenCalledOnce();
    expect(fetchApi).toHaveBeenCalledWith('/options/roots');
  });
});

// ---------------------------------------------------------------------------
// 2. getOptionChain
// ---------------------------------------------------------------------------

describe('getOptionChain', () => {
  it('builds URL with required params (no strike bounds)', async () => {
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('root=OPT_SP_500');
    expect(url).toContain('date=2024-03-15');
    expect(url).toContain('type=both');
    expect(url).toContain('expiration_min=2024-03-15');
    expect(url).toContain('expiration_max=2024-06-15');
    // Optional params absent → must NOT appear in URL.
    expect(url).not.toContain('strike_min');
    expect(url).not.toContain('strike_max');
    expect(url).not.toContain('compute_missing');
  });

  it('includes compute_missing=true when requested', async () => {
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'C',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
      computeMissing: true,
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('compute_missing=true');
  });

  it('includes strike bounds when provided', async () => {
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
      strikeMin: 450,
      strikeMax: 550,
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('strike_min=450');
    expect(url).toContain('strike_max=550');
  });

  it('includes expiration_cycle when provided', async () => {
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
      expirationCycle: 'M',
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('expiration_cycle=M');
  });

  it('omits expiration_cycle when null/undefined', async () => {
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
      expirationCycle: null,
    });
    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).not.toContain('expiration_cycle');

    vi.mocked(fetchApi).mockClear();
    mockSuccess(CHAIN_RESP);
    await getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
      // expirationCycle not supplied at all
    });
    const url2 = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url2).not.toContain('expiration_cycle');
  });
});

// ---------------------------------------------------------------------------
// 3. getOptionContract
// ---------------------------------------------------------------------------

describe('getOptionContract', () => {
  it('URL-encodes contractId containing a pipe character', async () => {
    mockSuccess(CONTRACT_RESP);
    await getOptionContract('OPT_SP_500', 'SPY|M');

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    // Pipe must be percent-encoded; plain '|' must NOT appear.
    expect(url).toContain('/options/contract/OPT_SP_500/SPY%7CM');
    expect(url).not.toContain('SPY|M');
  });

  it('omits optional params when not supplied', async () => {
    mockSuccess(CONTRACT_RESP);
    await getOptionContract('OPT_SP_500', 'SPY|M');

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).not.toContain('compute_missing');
    expect(url).not.toContain('date_from');
    expect(url).not.toContain('date_to');
  });

  it('appends optional params when supplied', async () => {
    mockSuccess(CONTRACT_RESP);
    await getOptionContract('OPT_SP_500', 'SPY|M', {
      computeMissing: true,
      dateFrom: '2024-01-01',
      dateTo: '2024-03-31',
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('compute_missing=true');
    expect(url).toContain('date_from=2024-01-01');
    expect(url).toContain('date_to=2024-03-31');
  });
});

// ---------------------------------------------------------------------------
// 4. selectOption
// ---------------------------------------------------------------------------

describe('selectOption', () => {
  it('serialises the query as URL-encoded JSON in the q param', async () => {
    mockSuccess(SELECT_RESP);

    const query = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      type: 'C',
      criterion: { kind: 'by_delta', target_delta: 0.3, tolerance: 0.05, strict: false },
      maturity: { kind: 'next_third_friday', offset_months: 1 },
    };

    await selectOption(query);

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('/options/select?q=');

    // Decode the q param and verify it round-trips back to the original object.
    const rawQ = url.replace('/options/select?q=', '');
    const parsed = JSON.parse(decodeURIComponent(rawQ));
    expect(parsed).toEqual(query);
  });

  it('returns the parsed JSON response unchanged', async () => {
    mockSuccess(SELECT_RESP);
    const result = await selectOption({ root: 'X', date: '2024-01-01', type: 'C', criterion: {}, maturity: {} });
    expect(result).toEqual(SELECT_RESP);
  });
});

// ---------------------------------------------------------------------------
// 5. getChainSnapshot
// ---------------------------------------------------------------------------

describe('getChainSnapshot', () => {
  it('repeats expirations as separate query params (not comma-joined)', async () => {
    mockSuccess(SNAPSHOT_RESP);
    await getChainSnapshot('OPT_SP_500', {
      date: '2024-03-15',
      type: 'C',
      expirations: ['2024-04-19', '2024-05-17'],
      field: 'iv',
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('expirations=2024-04-19');
    expect(url).toContain('expirations=2024-05-17');
    // Must NOT be comma-joined.
    expect(url).not.toContain('2024-04-19,2024-05-17');
    // Both must appear as separate occurrences.
    const occurrences = (url.match(/expirations=/g) || []).length;
    expect(occurrences).toBe(2);
  });

  it('includes root, date, type, field in URL', async () => {
    mockSuccess(SNAPSHOT_RESP);
    await getChainSnapshot('OPT_SP_500', {
      date: '2024-03-15',
      type: 'C',
      expirations: ['2024-04-19'],
      field: 'delta',
    });

    const url = vi.mocked(fetchApi).mock.calls[0][0];
    expect(url).toContain('root=OPT_SP_500');
    expect(url).toContain('date=2024-03-15');
    expect(url).toContain('type=C');
    expect(url).toContain('field=delta');
  });
});

// ---------------------------------------------------------------------------
// Error path — fetchClassified propagates ApiError as FetchError
// ---------------------------------------------------------------------------

describe('error propagation', () => {
  it('propagates a server ApiError as FetchError to caller', async () => {
    mockApiError('server_error', 'Internal server error');

    await expect(getOptionRoots()).rejects.toThrow('Internal server error');
  });

  it('wraps a network_error ApiError as a FetchError', async () => {
    mockApiError('network_error', 'Backend unreachable');

    await expect(getOptionChain('OPT_SP_500', {
      date: '2024-03-15',
      type: 'both',
      expirationMin: '2024-03-15',
      expirationMax: '2024-06-15',
    })).rejects.toMatchObject({ name: 'FetchError' });
  });

  it('propagates errors from getOptionContract', async () => {
    mockApiError('not_found', 'Contract not found');
    await expect(getOptionContract('OPT_SP_500', 'MISSING')).rejects.toThrow();
  });

  it('propagates errors from selectOption', async () => {
    mockApiError('validation', 'Invalid query');
    await expect(selectOption({})).rejects.toThrow();
  });

  it('propagates errors from getChainSnapshot', async () => {
    mockApiError('server_error', 'boom');
    await expect(getChainSnapshot('OPT_SP_500', {
      date: '2024-03-15',
      type: 'C',
      expirations: ['2024-04-19'],
      field: 'iv',
    })).rejects.toThrow();
  });
});
