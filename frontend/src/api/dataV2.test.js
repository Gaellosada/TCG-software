// Unit tests for the Database-v2 api client (``src/api/dataV2.js``).
//
// Only the two endpoints added for the filtered/paginated series drill-down are
// covered here: ``/objects/{id}/facets`` and ``/objects/{id}/series``.
//
// Testing note — why exact assertions rather than only ``toContain``:
// a ``url.toContain('strike_min=6000')`` check passes even when the PATH is
// wrong (``/objects/12`` instead of ``/objects/12/series``) or when an EXTRA
// param the caller never asked for was injected. Omission is load-bearing for
// this endpoint (an unset filter must fall through to the backend's own
// default, not arrive as an empty string), so each mapping test also asserts
// the fully-parsed param map and the exact path.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getObjectFacetsV2, getObjectSeriesV2 } from './dataV2';
import * as client from './client';

/** Split a request URL from the client into { path, params } for exact asserts. */
function splitUrl(url) {
  const [path, qs = ''] = url.split('?');
  return { path, params: Object.fromEntries(new URLSearchParams(qs)) };
}

function spyFetch(resolved = { items: [], total: 0, skip: 0, limit: 50 }) {
  return vi.spyOn(client, 'fetchApi').mockResolvedValue(resolved);
}

describe('getObjectSeriesV2', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('maps camelCase filters onto snake_case query params', async () => {
    const spy = spyFetch();
    await getObjectSeriesV2(12, {
      expirationMin: '2026-03-01',
      expirationMax: '2026-03-31',
      strikeMin: 6000,
      strikeMax: 7000,
      optionType: 'put',
      serieType: 'bbba',
      freq: '1m',
      skip: 50,
      limit: 100,
    });
    const url = spy.mock.calls[0][0];
    expect(url).toContain('expiration_min=2026-03-01');
    expect(url).toContain('expiration_max=2026-03-31');
    expect(url).toContain('strike_min=6000');
    expect(url).toContain('strike_max=7000');
    expect(url).toContain('option_type=put');
    expect(url).toContain('serie_type=bbba');
    expect(url).toContain('freq=1m');
    expect(url).toContain('skip=50');
    expect(url).toContain('limit=100');

    // Discriminating form of the above: the parsed map must be EXACTLY this —
    // catches a renamed param, a value attached to the wrong name, a dropped
    // param and an injected extra one, none of which the ``toContain`` list
    // above can distinguish.
    const { path, params } = splitUrl(url);
    expect(path).toBe('/data-v2/objects/12/series');
    expect(params).toEqual({
      expiration_min: '2026-03-01',
      expiration_max: '2026-03-31',
      strike_min: '6000',
      strike_max: '7000',
      option_type: 'put',
      serie_type: 'bbba',
      freq: '1m',
      skip: '50',
      limit: '100',
    });
  });

  it('omits unset filters entirely', async () => {
    const spy = spyFetch();
    await getObjectSeriesV2(12, {});
    const url = spy.mock.calls[0][0];
    expect(url).not.toContain('strike_min');
    expect(url).not.toContain('expiration_min');

    // Stronger: with no filters the request must carry NO query string at all,
    // so every backend default (option_type=both / serie_type=any / freq=any /
    // skip=0 / limit=50) applies server-side. An injected ``option_type=both``
    // or ``limit=50`` would slip past the two ``not.toContain`` checks above.
    expect(url).toBe('/data-v2/objects/12/series');
    expect(splitUrl(url).params).toEqual({});
  });

  it('omits unset filters when called with no filter object at all', async () => {
    const spy = spyFetch();
    await getObjectSeriesV2(12);
    expect(spy.mock.calls[0][0]).toBe('/data-v2/objects/12/series');
  });

  it('sends a zero strike bound (0 is a value, not "unset")', async () => {
    // A naive ``if (strikeMin)`` truthiness guard would silently drop 0.
    const spy = spyFetch();
    await getObjectSeriesV2(12, { strikeMin: 0, strikeMax: 0 });
    expect(splitUrl(spy.mock.calls[0][0]).params).toEqual({
      strike_min: '0',
      strike_max: '0',
    });
  });

  it('drops empty-string and null filters instead of sending them blank', async () => {
    // The filter panel clears an input to '' — that must mean "unset", never
    // ``strike_min=&option_type=``, which the backend would reject as a 400.
    const spy = spyFetch();
    await getObjectSeriesV2(12, {
      expirationMin: '',
      expirationMax: null,
      strikeMin: '',
      strikeMax: null,
      optionType: '',
      serieType: null,
      freq: '',
      skip: 0,
      limit: undefined,
    });
    expect(spy.mock.calls[0][0]).toBe('/data-v2/objects/12/series');
  });

  it('threads the AbortSignal as a fetch option and never as a query param', async () => {
    // ``useObjectSeriesV2`` calls this as ``{ ...filters, signal }``, so a
    // signal leaking into URLSearchParams would produce ``signal=[object
    // AbortSignal]`` on every request.
    const spy = spyFetch();
    const controller = new AbortController();
    await getObjectSeriesV2(12, { optionType: 'call', signal: controller.signal });
    const [url, options] = spy.mock.calls[0];
    expect(url).toBe('/data-v2/objects/12/series?option_type=call');
    expect(options).toEqual({ signal: controller.signal });
  });

  it('URL-encodes the object id', async () => {
    const spy = spyFetch();
    await getObjectSeriesV2('a/b', { freq: 'daily' });
    expect(spy.mock.calls[0][0]).toBe('/data-v2/objects/a%2Fb/series?freq=daily');
  });

  it('returns the { items, total, skip, limit } page unchanged', async () => {
    const page = {
      items: [{ serie_id: 7, type: 'bbba', freq: '1m', strike: 6000, option_type: 'put' }],
      total: 431,
      skip: 50,
      limit: 50,
    };
    spyFetch(page);
    await expect(getObjectSeriesV2(12, { skip: 50 })).resolves.toEqual(page);
  });

  // ── Guard 1: unknown filter keys are loud, not silently dropped ──────────

  it('rejects a snake_case filter key instead of silently dropping it', async () => {
    // Measured behaviour before the guard:
    //   { option_type: 'call', optionType: 'put' } -> ?option_type=put
    // i.e. the wire-format key vanished and the caller got a page filtered the
    // other way with nothing reporting it.
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { option_type: 'call', optionType: 'put' }),
    ).rejects.toThrow(TypeError);
    // The whole point: it throws BEFORE issuing anything.
    expect(spy).not.toHaveBeenCalled();
  });

  it('names every offending key in the error message', async () => {
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { serie_type: 'bbba', strikeMim: 6000, freq: '1m' }),
    ).rejects.toThrow(/'serie_type'.*'strikeMim'|'strikeMim'.*'serie_type'/);
    // A typo'd key must be named too, not just the snake_case one.
    await expect(
      getObjectSeriesV2(12, { strikeMim: 6000 }),
    ).rejects.toThrow(/strikeMim/);
    expect(spy).not.toHaveBeenCalled();
  });

  it('rejects query options mis-slotted into the filters argument', async () => {
    // The 3-arg hook shape makes this a plausible slip:
    //   useObjectSeriesV2(12, { limit: 50, enabled: false })
    // Before the guard this passed the `filters != null` gate and FETCHED while
    // the author believed the hook was disabled.
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { limit: 50, enabled: false }),
    ).rejects.toThrow(/'enabled'/);
    expect(spy).not.toHaveBeenCalled();
  });

  it('accepts every documented filter key, and signal, as known', async () => {
    // Guards the guard: an over-zealous allowlist that rejected a legitimate
    // key would break the whole feature, so assert the full set passes.
    const spy = spyFetch();
    await getObjectSeriesV2(12, {
      expirationMin: '2026-03-01',
      expirationMax: '2026-03-31',
      strikeMin: 6000,
      strikeMax: 7000,
      optionType: 'put',
      serieType: 'bbba',
      freq: '1m',
      skip: 50,
      limit: 100,
      signal: new AbortController().signal,
    });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  // ── Guard 2: a non-finite strike bound never reaches the wire ────────────

  it('rejects a NaN strike bound instead of sending strike_min=NaN', async () => {
    // FastAPI's `float | None = Query(None)` ACCEPTS the string "NaN" with HTTP
    // 200 as `nan` (unlike "abc"/""), which then matches nothing. So a strike
    // input doing Number('1e') would make the panel report "no series" for an
    // object with hundreds, with no error surfaced anywhere.
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { strikeMin: Number('1e') }),
    ).rejects.toThrow(/strikeMin must be a finite number/);
    await expect(
      getObjectSeriesV2(12, { strikeMax: NaN }),
    ).rejects.toThrow(/strikeMax must be a finite number/);
    expect(spy).not.toHaveBeenCalled();
  });

  it('reports the offending value as NaN, not as "null"', async () => {
    // JSON.stringify(NaN) === 'null', so the obvious message renders the most
    // misleading text available — "received null" for a value that is NaN.
    await expect(
      getObjectSeriesV2(12, { strikeMin: NaN }),
    ).rejects.toThrow(/received NaN/);
    await expect(
      getObjectSeriesV2(12, { strikeMax: Infinity }),
    ).rejects.toThrow(/received Infinity/);
    await expect(
      getObjectSeriesV2(12, { strikeMin: 'abc' }),
    ).rejects.toThrow(/received 'abc'/);
  });

  it('rejects a non-numeric string strike bound', async () => {
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { strikeMin: 'abc' }),
    ).rejects.toThrow(/strikeMin must be a finite number/);
    expect(spy).not.toHaveBeenCalled();
  });

  it('rejects Infinity as a strike bound', async () => {
    const spy = spyFetch();
    await expect(
      getObjectSeriesV2(12, { strikeMax: Infinity }),
    ).rejects.toThrow(/strikeMax must be a finite number/);
    expect(spy).not.toHaveBeenCalled();
  });

  it('still sends well-formed numeric and numeric-string strike bounds', async () => {
    // The finite check must not narrow what already worked.
    const spy = spyFetch();
    await getObjectSeriesV2(12, { strikeMin: '6000', strikeMax: 7000.5 });
    expect(splitUrl(spy.mock.calls[0][0]).params).toEqual({
      strike_min: '6000',
      strike_max: '7000.5',
    });
  });

  it('surfaces an out-of-range limit rejection (HTTP 400) as a FetchError', async () => {
    // The backend caps limit at 500 and this app remaps RequestValidationError
    // to 400 (tcg/core/app.py), so the client must not swallow it.
    vi.spyOn(client, 'fetchApi').mockRejectedValue(
      new client.ApiError('validation', 'limit must be <= 500', { status: 400 }),
    );
    await expect(getObjectSeriesV2(12, { limit: 5000 })).rejects.toMatchObject({
      name: 'FetchError',
    });
  });
});

describe('getObjectFacetsV2', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('GETs the facets path with no query params', async () => {
    const spy = spyFetch({ object_id: 12, kind: 'option', totals: { contracts: 0, series: 0 } });
    await getObjectFacetsV2(12);
    expect(spy.mock.calls[0][0]).toBe('/data-v2/objects/12/facets');
  });

  it('URL-encodes the object id and threads the AbortSignal', async () => {
    const spy = spyFetch({ object_id: 'a/b' });
    const controller = new AbortController();
    await getObjectFacetsV2('a/b', { signal: controller.signal });
    const [url, options] = spy.mock.calls[0];
    expect(url).toBe('/data-v2/objects/a%2Fb/facets');
    expect(options).toEqual({ signal: controller.signal });
  });

  it('returns the facets payload unchanged', async () => {
    const facets = {
      object_id: 12,
      kind: 'option',
      expirations: [{ expiration: '2026-03-20', contracts: 118 }],
      strike_min: 1000,
      strike_max: 9000,
      option_types: ['call', 'put'],
      serie_types: [{ type: 'bbba', freq: '1m', series: 236 }],
      totals: { contracts: 118, series: 236 },
    };
    spyFetch(facets);
    await expect(getObjectFacetsV2(12)).resolves.toEqual(facets);
  });

  it('propagates a 404 for an unknown object as a FetchError', async () => {
    vi.spyOn(client, 'fetchApi').mockRejectedValue(
      new client.ApiError('not_found', 'Object 999 not found', { status: 404 }),
    );
    await expect(getObjectFacetsV2(999)).rejects.toMatchObject({ name: 'FetchError' });
  });
});
