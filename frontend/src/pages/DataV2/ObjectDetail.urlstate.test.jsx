// @vitest-environment jsdom
/**
 * The filter state lives in the URL. What that has to buy, and is asserted here:
 *   - a link reproduces a view (restore fetches with no Apply click),
 *   - a reload loses nothing (including WHICH page),
 *   - the back button works (so the writes are pushes, not replaces),
 *   - and none of it re-opens the gate: an empty query string fetches nothing.
 *
 * Assertions compare the WHOLE query string, not a substring of it. A
 * ``toContain('serie_type=bbba')`` passes just as happily when every other
 * parameter is missing, wrong, or left over from the previous state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, useSearchParams, useNavigate } from 'react-router-dom';

// ``listObjectsV2`` is not used by ObjectDetail but IS imported by
// hooks/marketQueries, and a mock factory replaces the whole module — omitting
// it makes the hook module fail to import.
vi.mock('../../api/dataV2', () => ({
  listObjectsV2: vi.fn(),
  getObjectDetailV2: vi.fn(),
  getObjectFacetsV2: vi.fn(),
  getObjectSeriesV2: vi.fn(),
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));

// Plotly needs a canvas; jsdom has none.
vi.mock('../../components/Chart', () => ({
  default: () => <div data-testid="chart" />,
}));

import ObjectDetail from './ObjectDetail';
import {
  getObjectDetailV2,
  getObjectFacetsV2,
  getObjectSeriesV2,
} from '../../api/dataV2';

afterEach(cleanup);

const OBJECT = {
  object_id: 12,
  kind: 'option',
  symbol: 'OPT_SP_500_EW2',
  name: 'EW2 Weekly',
  cycle: 'weekly',
  underlying_object_id: 6,
};

const ROW = {
  serie_id: 1433194, contract_id: 77, type: 'bbba', freq: '1m',
  source: 'DATABENTO', contract_code: 'EW2H6 P6260.20260313',
  expiration: '2026-03-13', strike: 6260, option_type: 'put',
};

/**
 * Reads the query string out of the router and offers a real history-back
 * button (MemoryRouter has no address bar, and ``navigate(-1)`` is the same
 * history ``go`` the browser button calls).
 */
function Probe() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  return (
    <>
      <div data-testid="qs">{params.toString()}</div>
      <button type="button" onClick={() => navigate(-1)}>go back in history</button>
    </>
  );
}

function renderAt(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ObjectDetail object={OBJECT} />
      <Probe />
    </MemoryRouter>,
  );
}

/** The query string as a plain object, so it can be compared exhaustively. */
function qs() {
  return Object.fromEntries(new URLSearchParams(screen.getByTestId('qs').textContent));
}

/** The filters of the most recent series request. */
function lastSent() {
  const calls = vi.mocked(getObjectSeriesV2).mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls.at(-1)[1];
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getObjectDetailV2).mockResolvedValue({ object: OBJECT });
  vi.mocked(getObjectFacetsV2).mockResolvedValue({
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
      { type: 'bbba', freq: '1m', series: 96106 },
      { type: 'bar', freq: 'daily', series: 4230 },
    ],
    totals: { contracts: 96106, series: 200672 },
  });
  // Echo the requested offset back, as the real endpoint does — otherwise the
  // rendered "(51-51)" range would come from a hardcoded 0 and could not
  // disagree with the URL.
  vi.mocked(getObjectSeriesV2).mockImplementation(async (_objectId, sent = {}) => ({
    items: [ROW],
    total: 195,
    skip: sent.skip || 0,
    limit: sent.limit || 50,
  }));
});

describe('ObjectDetail filter state in the URL', () => {
  it('writes every applied dimension into the query string, and only those', async () => {
    renderAt('/data-v2');
    fireEvent.change(await screen.findByLabelText(/expiration/i), {
      target: { value: '2026-03-13' },
    });
    fireEvent.change(screen.getByLabelText(/strike min/i), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText(/option type/i), { target: { value: 'put' } });
    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bbba' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    await waitFor(() => expect(qs()).toEqual({
      expiration_min: '2026-03-13',
      expiration_max: '2026-03-13',
      strike_min: '6000',
      option_type: 'put',
      serie_type: 'bbba',
    }));
    // The untouched dimensions are absent rather than written as their
    // defaults: a shared link should read as what the user chose.
    expect(qs().freq).toBeUndefined();
    expect(qs().strike_max).toBeUndefined();
    // …and the request that the URL produced carries the same filter.
    await waitFor(() => expect(lastSent()).toMatchObject({
      expirationMin: '2026-03-13',
      expirationMax: '2026-03-13',
      strikeMin: 6000,
      optionType: 'put',
      serieType: 'bbba',
      freq: 'any',
      skip: 0,
      limit: 50,
    }));
  });

  it('restores an applied filter from the query string and fetches at once', async () => {
    renderAt('/data-v2?serie_type=bbba&freq=1m&option_type=put');

    // No Apply click anywhere in this test: the URL already expresses an
    // applied filter, and a shared link must list results without one.
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    await waitFor(() => expect(getObjectSeriesV2).toHaveBeenCalled());
    expect(lastSent()).toMatchObject({
      serieType: 'bbba', freq: '1m', optionType: 'put', skip: 0, limit: 50,
    });
    // Nothing invented: the dimensions the URL did not name are unset.
    expect(lastSent().expirationMin).toBeUndefined();
    expect(lastSent().strikeMin).toBeUndefined();

    // The panel agrees with the URL — a pre-filled panel is how the recipient
    // can tell what they are looking at, and how they narrow it further.
    expect(screen.getByLabelText(/series type/i).value).toBe('bbba');
    expect(screen.getByLabelText(/frequency/i).value).toBe('1m');
    expect(screen.getByLabelText(/option type/i).value).toBe('put');
  });

  it('accepts the spec\'s single-expiration URL as a one-day window', async () => {
    renderAt('/data-v2?expiration=2026-03-13&serie_type=bbba');
    await waitFor(() => expect(lastSent()).toMatchObject({
      expirationMin: '2026-03-13',
      expirationMax: '2026-03-13',
      serieType: 'bbba',
    }));
    // The series request does not wait for /facets, so the panel may still be
    // loading when the request has already gone out.
    expect((await screen.findByLabelText(/expiration/i)).value).toBe('2026-03-13');
  });

  // ---------------------------------------------------------------------
  // The gate, in its URL form. The endpoint this page replaced shipped a
  // 38 MB unbounded series list; "restore from the URL" must not become
  // "fetch whatever, whenever".
  // ---------------------------------------------------------------------

  it('fetches nothing when the URL carries no filter', async () => {
    renderAt('/data-v2');
    // Wait until the panel is up, so "no request" is a statement about a
    // rendered page rather than about a tree that has not settled yet.
    expect(await screen.findByLabelText(/series type/i)).toBeTruthy();
    expect(getObjectFacetsV2).toHaveBeenCalled();      // the cheap one did run
    expect(getObjectSeriesV2).not.toHaveBeenCalled();  // the gated one did not
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('treats a bare skip as no filter, not as a filter', async () => {
    renderAt('/data-v2?skip=50');
    expect(await screen.findByLabelText(/series type/i)).toBeTruthy();
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('degrades a non-finite strike bound in the URL instead of throwing', async () => {
    // ``getObjectSeriesV2`` throws a TypeError on a non-finite bound (the
    // backend answers a NaN bound with HTTP 200 and no rows), and the URL is
    // the one place a caller can hand it one — the number inputs sanitise.
    renderAt('/data-v2?strike_min=abc&serie_type=bbba');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent().strikeMin).toBeUndefined();
    expect(lastSent()).toMatchObject({ serieType: 'bbba' });
    // The panel shows no bound either, rather than the literal 'abc'.
    expect(screen.getByLabelText(/strike min/i).value).toBe('');
  });

  it('drops an enum value the backend would reject, rather than passing it on', async () => {
    /*
     * The backend allowlists all three enums (``_SERIE_TYPE_VALUES`` &c. in
     * ``tcg/core/api/data_v2.py``), so junk here comes back as a validation
     * error banner instead of a page. Worse, the panel cannot show it: a
     * <select> whose value is absent from its options falls back to the first
     * option, so it would read "Any" while the request carried "<script>".
     */
    renderAt('/data-v2?serie_type=%3Cscript%3E&freq=nope&option_type=zzz&expiration=2026-03-13');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent()).toMatchObject({
      expirationMin: '2026-03-13',   // the one usable dimension survives
      serieType: 'any',
      freq: 'any',
      optionType: 'both',
    });
    // …and the panel agrees with the request, which is the whole point.
    expect((await screen.findByLabelText(/series type/i)).value).toBe('any');
    expect(screen.getByLabelText(/frequency/i).value).toBe('any');
    expect(screen.getByLabelText(/option type/i).value).toBe('both');
  });

  it('does not fetch for a URL whose only filters are illegal enum values', async () => {
    renderAt('/data-v2?serie_type=%3Cscript%3E&freq=nope&option_type=zzz');
    expect(await screen.findByLabelText(/series type/i)).toBeTruthy();
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('does not fetch for a URL whose only filter is unusable', async () => {
    renderAt('/data-v2?strike_min=abc');
    expect(await screen.findByLabelText(/series type/i)).toBeTruthy();
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('records an Apply that narrows nothing, which an empty URL cannot express', async () => {
    renderAt('/data-v2');
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent()).toMatchObject({ serieType: 'any', optionType: 'both', freq: 'any' });
    // The state is in the URL, so it survives a reload: an empty query string
    // would read back as "not applied yet" and show the prompt again.
    expect(qs()).toEqual({ applied: '1' });
  });

  // ---------------------------------------------------------------------
  // Pagination.
  // ---------------------------------------------------------------------

  it('records the page in the URL so paging survives a reload', async () => {
    renderAt('/data-v2?serie_type=bbba');
    // Next is disabled until a page with a total has arrived, so clicking
    // before then is a silent no-op that would make this test vacuous.
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', skip: '50' }));
    // The filter travelled with the page, on both sides of the boundary.
    await waitFor(() => expect(lastSent()).toMatchObject({ serieType: 'bbba', skip: 50 }));
  });

  it('keeps an unnarrowed filter alive across a page change', async () => {
    renderAt('/data-v2?applied=1');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    // Dropping the marker here would leave ``?skip=50``, which reads back as
    // "no filter" — the user would land on the pre-Apply prompt from page 2.
    await waitFor(() => expect(qs()).toEqual({ applied: '1', skip: '50' }));
    await waitFor(() => expect(lastSent()).toMatchObject({ serieType: 'any', skip: 50 }));
    expect(screen.queryByText(/press Apply to list/i)).toBeNull();
  });

  it('restores the page a link points at rather than resetting to the first', async () => {
    renderAt('/data-v2?serie_type=bbba&skip=50');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent()).toMatchObject({ serieType: 'bbba', skip: 50 });
    // The offset stays put: a restored filter must not be echoed back to the
    // parent as a NEW application, whose "start from page 1" rule would move
    // the user to a page they did not ask for.
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('195 series (51-51)'));
    expect(qs()).toEqual({ serie_type: 'bbba', skip: '50' });
    expect(vi.mocked(getObjectSeriesV2).mock.calls.length).toBe(1);
  });

  it('falls back to the first page for an unusable offset in the URL', async () => {
    // A hand-edited or truncated URL. NaN is dropped by the client (so the
    // request would silently be page 1 while the pager rendered "NaN-NaN of
    // 195"), and a negative offset is an HTTP 422 from the backend.
    renderAt('/data-v2?serie_type=bbba&skip=abc');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent().skip).toBe(0);
    expect(screen.getByRole('status').textContent).toBe('195 series (1-1)');

    cleanup();
    renderAt('/data-v2?serie_type=bbba&skip=-5');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(lastSent().skip).toBe(0);
    expect(screen.getByRole('status').textContent).toBe('195 series (1-1)');
  });

  it('sends a new filter back to the first page', async () => {
    renderAt('/data-v2?serie_type=bbba&skip=50');
    await waitFor(() => expect(lastSent().skip).toBe(50));
    fireEvent.change(await screen.findByLabelText(/frequency/i), {
      target: { value: 'daily' },
    });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', freq: 'daily' }));
    await waitFor(() => expect(lastSent()).toMatchObject({
      serieType: 'bbba', freq: 'daily', skip: 0,
    }));
  });

  // ---------------------------------------------------------------------
  // Back button and Reset.
  // ---------------------------------------------------------------------

  it('returns the previous filter on the back button, in the URL AND in the panel', async () => {
    renderAt('/data-v2');
    fireEvent.change(await screen.findByLabelText(/series type/i), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));

    fireEvent.change(screen.getByLabelText(/series type/i), { target: { value: 'bar' } });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bar' }));
    await waitFor(() => expect(lastSent()).toMatchObject({ serieType: 'bar' }));

    // Each Apply/change is a history entry, so the browser back button can
    // return to it. (A ``replace`` write would leave the query string alone
    // here and take the user off the page instead.)
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));
    // …and the panel follows the URL back. Otherwise the controls claim 'bar'
    // while the list below them shows the 'bbba' page — the mismatch that made
    // Reset look like a no-op, in a new disguise.
    await waitFor(() => expect(screen.getByLabelText(/series type/i).value).toBe('bbba'));
    await waitFor(() => expect(lastSent()).toMatchObject({ serieType: 'bbba' }));
  });

  it('spends ONE history entry on a typed strike bound, not one per keystroke', async () => {
    /*
     * Each keystroke in a number input is a filter change, so it writes the
     * URL. Pushed, that costs one history entry per character: typing 6260
     * needed four back presses to undo, and a user who wanted to leave
     * /data-v2 entirely had to press back past every character they had typed.
     * "The back button works" was true and still useless.
     *
     * So consecutive writes that differ only in the VALUE of a numeric bound
     * replace the current entry instead of pushing a new one. The first one
     * still pushes, so the state before the bound existed stays reachable.
     */
    renderAt('/data-v2');
    fireEvent.change(await screen.findByLabelText(/series type/i), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));

    const strike = screen.getByLabelText(/strike min/i);
    for (const value of ['6', '62', '626', '6260']) {
      fireEvent.change(strike, { target: { value } });
    }
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '6260' }));

    // ONE back press undoes the whole typing episode…
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));

    // …and the next one leaves the filter behind entirely, rather than landing
    // on a half-typed bound. Three entries total: empty, the type filter, the
    // strike episode.
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({}));
  });

  it('still pushes when a bound is added or cleared, so it can be undone', async () => {
    renderAt('/data-v2?serie_type=bbba');
    const strike = await screen.findByLabelText(/strike min/i);
    fireEvent.change(strike, { target: { value: '6000' } });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '6000' }));
    // Clearing the field is not "the same filter with another number", so it is
    // its own entry — otherwise the bound could never be restored by going back.
    fireEvent.change(strike, { target: { value: '' } });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '6000' }));
  });

  it('never replaces a state the user navigated back to', async () => {
    /*
     * The replace rule compares against the last URL WE wrote. After a back
     * press the current URL is not ours any more, and replacing it would
     * overwrite the state the user just returned to — one back press would then
     * skip two states. So an external change forces the next write to push.
     */
    renderAt('/data-v2?serie_type=bbba');
    fireEvent.change(await screen.findByLabelText(/strike min/i), {
      target: { value: '6000' },
    });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '6000' }));

    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));

    // Same shape as the write before the back press — but a different history
    // entry, which must survive.
    fireEvent.change(await screen.findByLabelText(/strike min/i), {
      target: { value: '7000' },
    });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '7000' }));
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba' }));
  });

  it('does not remount the panel for its own writes', async () => {
    renderAt('/data-v2?serie_type=bbba');
    const strike = await screen.findByLabelText(/strike min/i);
    fireEvent.change(strike, { target: { value: '6000' } });
    await waitFor(() => expect(qs()).toEqual({ serie_type: 'bbba', strike_min: '6000' }));
    // Same DOM node: a remount would replace it, discarding focus and caret
    // position between two keystrokes of a strike bound.
    expect(screen.getByLabelText(/strike min/i)).toBe(strike);
    expect(screen.getByLabelText(/strike min/i).value).toBe('6000');
  });

  it('clears the query string on Reset, so a reload cannot resurrect the filter', async () => {
    renderAt('/data-v2?serie_type=bbba&skip=50');
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    const before = vi.mocked(getObjectSeriesV2).mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: /reset/i }));

    // The URL is the source of truth, so an unchanged query string would mean
    // the very next reload brings back the filter just cleared.
    await waitFor(() => expect(qs()).toEqual({}));
    expect(await screen.findByText(/press Apply to list/i)).toBeTruthy();
    expect(screen.queryByText('EW2H6 P6260.20260313')).toBeNull();
    // Reset re-gates: it does not apply the empty filter.
    expect(vi.mocked(getObjectSeriesV2).mock.calls.length).toBe(before);
    expect(screen.getByLabelText(/series type/i).value).toBe('any');
  });
});
