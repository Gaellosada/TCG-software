// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the hook module (declared before the component import for hoisting) so
// the panel needs no QueryClient. This mirrors how DataV2Page.test.jsx stubs
// its dependencies.
vi.mock('../../hooks/marketQueries', () => ({
  useObjectFacetsV2: vi.fn(),
}));

// Mocked at the transport boundary only, so ``api/dataV2`` itself stays REAL.
// That is the point of the "accepted by the real client" test below: the filter
// object the panel emits is fed through the actual allowlist guard
// (``assertKnownSeriesFilters``) and the actual param encoder, not a copy of
// their rules that could drift.
vi.mock('../../api/client', () => ({
  fetchApi: vi.fn(async () => ({ items: [], total: 0, skip: 0, limit: 50 })),
}));

import SeriesFilterPanel, { parseStrikeBound } from './SeriesFilterPanel';
import { useObjectFacetsV2 } from '../../hooks/marketQueries';
import { getObjectSeriesV2 } from '../../api/dataV2';
import { fetchApi } from '../../api/client';

afterEach(cleanup);

const OPTION_FACETS = {
  object_id: 12,
  kind: 'option',
  expirations: [
    { expiration: '2026-03-13', contracts: 500 },
    { expiration: '2026-02-13', contracts: 480 },
  ],
  strike_min: 15,
  strike_max: 10600,
  option_types: ['call', 'put'],
  serie_types: [
    { type: 'bar', freq: '1m', series: 96106 },
    { type: 'bbba', freq: '1m', series: 96106 },
    { type: 'greeks', freq: 'daily', series: 4230 },
  ],
  totals: { contracts: 96106, series: 200672 },
};

const INDEX_FACETS = {
  object_id: 5,
  kind: 'index',
  expirations: [],
  strike_min: null,
  strike_max: null,
  option_types: [],
  serie_types: [{ type: 'bar', freq: 'daily', series: 1 }],
  totals: { contracts: 0, series: 1 },
};

function mockFacets(data) {
  useObjectFacetsV2.mockReturnValue({ data, loading: false, error: null });
}

describe('SeriesFilterPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchApi.mockResolvedValue({ items: [], total: 0, skip: 0, limit: 50 });
  });

  it('offers expiration, strike, option type and series controls for an option', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    // ``getByLabelText`` already throws when a control is missing; asserting the
    // resolved element's tag/type additionally pins that each label points at a
    // real control of the right kind (a stray <div> would satisfy mere presence).
    expect(screen.getByLabelText(/expiration/i).tagName).toBe('SELECT');
    expect(screen.getByLabelText(/strike min/i).type).toBe('number');
    expect(screen.getByLabelText(/strike max/i).type).toBe('number');
    expect(screen.getByLabelText(/option type/i).tagName).toBe('SELECT');
    expect(screen.getByLabelText(/series type/i).tagName).toBe('SELECT');
    expect(screen.getByLabelText(/frequency/i).tagName).toBe('SELECT');
  });

  it('hides contract dimensions for an object without contracts', () => {
    mockFacets(INDEX_FACETS);
    render(<SeriesFilterPanel objectId={5} onApply={() => {}} />);
    expect(screen.queryByLabelText(/expiration/i)).toBeNull();
    expect(screen.queryByLabelText(/strike min/i)).toBeNull();
    expect(screen.queryByLabelText(/strike max/i)).toBeNull();
    expect(screen.queryByLabelText(/option type/i)).toBeNull();
    // The series-type and frequency controls always exist — with no contracts
    // they are the only dimensions this object has. Asserted so the absences
    // above cannot pass merely because the panel rendered nothing at all.
    expect(screen.getByLabelText(/series type/i).tagName).toBe('SELECT');
    expect(screen.getByLabelText(/frequency/i).tagName).toBe('SELECT');
    expect(screen.getByRole('button', { name: /apply/i })).toBeTruthy();
  });

  it('populates the expiration options from facets', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    expect(screen.getByRole('option', { name: /2026-03-13/ }).value).toBe('2026-03-13');
    expect(screen.getByRole('option', { name: /2026-02-13/ }).value).toBe('2026-02-13');
    // Contract counts come from facets and are shown on the option label.
    expect(screen.getByRole('option', { name: /2026-03-13/ }).textContent).toMatch(/500/);
  });

  it('offers only the series types and frequencies this object actually has', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    const serieType = screen.getByLabelText(/series type/i);
    expect([...serieType.options].map((o) => o.value))
      .toEqual(['any', 'bar', 'bbba', 'greeks']);
    // ``value`` is absent from OPTION_FACETS.serie_types, so it must not be offered.
    expect([...serieType.options].map((o) => o.value)).not.toContain('value');
    const freq = screen.getByLabelText(/frequency/i);
    expect([...freq.options].map((o) => o.value)).toEqual(['any', '1m', 'daily']);
  });

  // ---------------------------------------------------------------------
  // The gate. These four tests are the reason the component exists: the
  // endpoint this page replaced shipped a 38 MB unbounded series list.
  // ---------------------------------------------------------------------

  it('does not call onApply before the first Apply click', () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    // Every control, not just one: a gate that leaks on any single dimension
    // re-opens the unbounded request.
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    fireEvent.change(screen.getByLabelText(/option type/i), { target: { value: 'call' } });
    fireEvent.change(screen.getByLabelText(/expiration/i), { target: { value: '2026-02-13' } });
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/strike max/i), { target: { value: '7000' } });
    expect(onApply).not.toHaveBeenCalled();
  });

  it('applies on the first click, then auto-applies on every later change', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);

    fireEvent.change(screen.getByLabelText(/expiration/i), {
      target: { value: '2026-03-13' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(onApply.mock.calls[0][0]).toMatchObject({
      expirationMin: '2026-03-13',
      expirationMax: '2026-03-13',
    });

    fireEvent.change(screen.getByLabelText(/option type/i), {
      target: { value: 'put' },
    });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
    expect(onApply.mock.calls[1][0]).toMatchObject({ optionType: 'put' });

    // A third change must also emit — "auto-apply" is a standing mode, not a
    // one-shot that fires once after the click.
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: 'daily' } });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(3));
    expect(onApply.mock.calls[2][0]).toMatchObject({ freq: 'daily', optionType: 'put' });
  });

  it('reset clears the fields and re-gates until Apply', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);

    // Move every field OFF its default first, otherwise "reset clears the
    // fields" is asserted against values that were never dirty and the
    // assertion cannot fail.
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    fireEvent.change(screen.getByLabelText(/option type/i), { target: { value: 'call' } });
    fireEvent.change(screen.getByLabelText(/expiration/i), { target: { value: '2026-02-13' } });
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/strike max/i), { target: { value: '7000' } });

    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));

    const before = onApply.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: /reset/i }));

    expect(screen.getByLabelText(/series type/i).value).toBe('any');
    expect(screen.getByLabelText(/frequency/i).value).toBe('any');
    expect(screen.getByLabelText(/option type/i).value).toBe('both');
    expect(screen.getByLabelText(/expiration/i).value).toBe('');
    expect(screen.getByLabelText(/strike min/i).value).toBe('');
    expect(screen.getByLabelText(/strike max/i).value).toBe('');

    // Reset itself must not emit onApply — it re-gates, it does not apply
    // "no filter" (that would be the unbounded request).
    expect(onApply.mock.calls.length).toBe(before);

    // …and a change after Reset must stay gated until Apply is pressed again.
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bar' } });
    expect(onApply.mock.calls.length).toBe(before);

    // Apply re-opens the gate.
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(before + 1));
    expect(onApply.mock.calls[before][0]).toMatchObject({
      serieType: 'bar',
      freq: 'any',
      optionType: 'both',
    });
    expect(onApply.mock.calls[before][0].expirationMin).toBeUndefined();
    expect(onApply.mock.calls[before][0].strikeMin).toBeUndefined();
  });

  it('tells the parent to un-apply on Reset, and only on Reset', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    const onReset = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} onReset={onReset} />);

    // Neither a field change nor Apply is a reset.
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(onReset).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
    expect(onReset).not.toHaveBeenCalled();

    // Reset notifies. Without this the parent keeps showing the page produced
    // by the filters just cleared, with no refetch and nothing saying so.
    fireEvent.click(screen.getByRole('button', { name: /reset/i }));
    expect(onReset).toHaveBeenCalledTimes(1);
    // …and still does not emit onApply.
    expect(onApply).toHaveBeenCalledTimes(2);
  });

  it('survives a parent that passes no onReset', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /reset/i }));
    // The optional callback must not throw for the Continuous-tab-only callers.
    expect(screen.getByLabelText(/series type/i).value).toBe('any');
  });

  it('labels the object total as the population being filtered, not the result count', () => {
    mockFacets(OPTION_FACETS);
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    // This figure is the object's UNFILTERED total and it renders directly
    // beside the result list's filtered "N series (from-to)". Measured on the
    // real page: 200 672 next to 1, both reading as "how many matched".
    // Locale-formatted (this suite runs under fr-FR), hence toLocaleString.
    const total = (200672).toLocaleString();
    const header = screen.getByText(/^Filters/);
    expect(header.textContent).toBe(`Filters · of ${total} series`);
    expect(header.textContent).not.toBe(`Filters · ${total} series`);
  });

  it('does not swallow the next change after a redundant Apply click', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);

    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));

    // A user who clicks Apply again out of habit (a no-op re-application) must
    // not disarm auto-apply for the change that follows.
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));

    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'greeks' } });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(3));
    expect(onApply.mock.calls[2][0]).toMatchObject({ serieType: 'greeks' });
  });

  it('re-gates and clears the fields when the object changes', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    const { rerender } = render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));

    // Switching object must NOT auto-apply the previous object's filters.
    const before = onApply.mock.calls.length;
    rerender(<SeriesFilterPanel objectId={99} onApply={onApply} />);
    expect(onApply.mock.calls.length).toBe(before);
    expect(screen.getByLabelText(/strike min/i).value).toBe('');
    expect(screen.getByLabelText(/series type/i).value).toBe('any');

    // …and the gate is re-armed for the new object.
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bar' } });
    expect(onApply.mock.calls.length).toBe(before);
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(before + 1));
    expect(onApply.mock.calls[before][0].strikeMin).toBeUndefined();
    expect(onApply.mock.calls[before][0]).toMatchObject({ serieType: 'bar' });
  });

  // ---------------------------------------------------------------------
  // ``initialFilters`` — a filter state restored from somewhere the user
  // already applied it (in practice the URL). It arrives pre-filled AND
  // pre-applied: the recipient of a shared link must not have to press Apply.
  // ---------------------------------------------------------------------

  it('pre-fills from initialFilters and starts already applied', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(
      <SeriesFilterPanel
        objectId={12}
        initialFilters={{
          expirationMin: '2026-03-13',
          expirationMax: '2026-03-13',
          optionType: 'put',
          serieType: 'bbba',
          freq: '1m',
        }}
        onApply={onApply}
      />,
    );
    expect(screen.getByLabelText(/expiration/i).value).toBe('2026-03-13');
    expect(screen.getByLabelText(/series type/i).value).toBe('bbba');
    expect(screen.getByLabelText(/frequency/i).value).toBe('1m');
    expect(screen.getByLabelText(/option type/i).value).toBe('put');

    // Already applied: a change auto-applies with no Apply click first.
    fireEvent.change(screen.getByLabelText(/option type/i), {
      target: { value: 'call' },
    });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(onApply.mock.calls[0][0]).toMatchObject({ optionType: 'call' });
    // …and the emitted set carries the seeded dimensions too, not just the
    // one that moved: seeding the FIELDS but not the emitted ``filters`` would
    // silently widen the query the parent re-issues.
    expect(onApply.mock.calls[0][0]).toMatchObject({
      expirationMin: '2026-03-13',
      expirationMax: '2026-03-13',
      serieType: 'bbba',
      freq: '1m',
    });
  });

  it('seeds numeric strike bounds back into the inputs', () => {
    mockFacets(OPTION_FACETS);
    render(
      <SeriesFilterPanel
        objectId={12}
        initialFilters={{ strikeMin: 6000, strikeMax: 7000.5, optionType: 'both', serieType: 'any', freq: 'any' }}
        onApply={() => {}}
      />,
    );
    // A number input's ``value`` is a string; a raw number would render as ''.
    expect(screen.getByLabelText(/strike min/i).value).toBe('6000');
    expect(screen.getByLabelText(/strike max/i).value).toBe('7000.5');
  });

  it('does not echo initialFilters back to the parent on mount', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(
      <SeriesFilterPanel
        objectId={12}
        initialFilters={{ serieType: 'bbba', optionType: 'both', freq: 'any' }}
        onApply={onApply}
      />,
    );
    // The parent HANDED US these filters; it already has them applied. Echoing
    // them back reads as a fresh application, and the parent's "a new filter
    // starts from page 1" rule then discards the page a shared link asked for
    // (``?serie_type=bbba&skip=50`` silently lands on page 1). Wait a tick so a
    // late effect-driven emit cannot hide behind a synchronous assertion.
    await waitFor(() => expect(screen.getByLabelText(/series type/i).value).toBe('bbba'));
    expect(onApply).not.toHaveBeenCalled();

    // …and the panel is nonetheless applied: the very next change emits.
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
  });

  it('re-arms Reset even when it started from initialFilters', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    const onReset = vi.fn();
    render(
      <SeriesFilterPanel
        objectId={12}
        initialFilters={{ serieType: 'bbba', optionType: 'both', freq: 'any' }}
        onApply={onApply}
        onReset={onReset}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /reset/i }));
    expect(onReset).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/series type/i).value).toBe('any');
    // Re-gated: a restored state is still un-appliable back to nothing.
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    expect(onApply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
  });

  // ---------------------------------------------------------------------
  // Shape of the emitted filter object.
  // ---------------------------------------------------------------------

  it('omits strike bounds that were left empty rather than sending 0', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    const filters = onApply.mock.calls[0][0];
    expect(filters.strikeMin).toBeUndefined();
    expect(filters.strikeMax).toBeUndefined();
    expect(filters.expirationMin).toBeUndefined();
    expect(filters.expirationMax).toBeUndefined();
    // Sentinels for the enum dimensions are sent explicitly and match the
    // backend defaults.
    expect(filters).toMatchObject({ optionType: 'both', serieType: 'any', freq: 'any' });
  });

  it('emits finite numbers for strike bounds, not strings', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/strike max/i), { target: { value: '7000.5' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    const filters = onApply.mock.calls[0][0];
    expect(filters.strikeMin).toBe(6000);
    expect(filters.strikeMax).toBe(7000.5);
    expect(Number.isFinite(filters.strikeMin)).toBe(true);
    expect(Number.isFinite(filters.strikeMax)).toBe(true);
  });

  it('emits a filter object the real series client accepts and encodes', async () => {
    mockFacets(OPTION_FACETS);
    const onApply = vi.fn();
    render(<SeriesFilterPanel objectId={12} onApply={onApply} />);
    fireEvent.change(screen.getByLabelText(/expiration/i), { target: { value: '2026-03-13' } });
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/strike max/i), { target: { value: '7000' } });
    fireEvent.change(screen.getByLabelText(/option type/i), { target: { value: 'put' } });
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.change(screen.getByLabelText(/frequency/i), { target: { value: '1m' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));

    // The REAL client: an unrecognised key or a non-finite strike throws
    // TypeError here, so this both proves allowlist compliance and pins the
    // camelCase → wire mapping.
    await expect(getObjectSeriesV2(12, onApply.mock.calls[0][0])).resolves.toBeTruthy();
    const path = fetchApi.mock.calls[0][0];
    const query = new URLSearchParams(path.slice(path.indexOf('?') + 1));
    expect(Object.fromEntries(query)).toEqual({
      expiration_min: '2026-03-13',
      expiration_max: '2026-03-13',
      strike_min: '6000',
      strike_max: '7000',
      option_type: 'put',
      serie_type: 'bbba',
      freq: '1m',
    });
  });

  // ---------------------------------------------------------------------
  // Strike parsing helper. A number input sanitises garbage to '' in both
  // jsdom and Blink, so the non-finite branch is not reachable through the
  // DOM; it is defence-in-depth and is tested here directly.
  // ---------------------------------------------------------------------

  it('parseStrikeBound omits blanks and refuses non-finite input', () => {
    expect(parseStrikeBound('')).toBeUndefined();
    expect(parseStrikeBound(null)).toBeUndefined();
    expect(parseStrikeBound(undefined)).toBeUndefined();
    expect(parseStrikeBound('6000')).toBe(6000);
    expect(parseStrikeBound('0')).toBe(0);
    expect(parseStrikeBound('-12.5')).toBe(-12.5);
    // Never NaN / Infinity — the client rejects those with a TypeError, and
    // surfacing that as a user-facing error would be a UI bug.
    expect(parseStrikeBound('1e')).toBeUndefined();
    expect(parseStrikeBound('abc')).toBeUndefined();
    expect(parseStrikeBound('-')).toBeUndefined();
    expect(parseStrikeBound('1e999')).toBeUndefined();
  });

  // ---------------------------------------------------------------------
  // Facets loading / failure.
  // ---------------------------------------------------------------------

  it('shows a loading placeholder and no controls while facets load', () => {
    useObjectFacetsV2.mockReturnValue({ data: null, loading: true, error: null });
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    expect(screen.getByText(/loading filters/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull();
  });

  it('reports a facets failure instead of rendering controls', () => {
    useObjectFacetsV2.mockReturnValue({
      data: null, loading: false, error: new Error('facets exploded'),
    });
    render(<SeriesFilterPanel objectId={12} onApply={() => {}} />);
    expect(screen.getByText(/facets exploded/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull();
  });
});
