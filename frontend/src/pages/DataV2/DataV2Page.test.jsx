// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, useSearchParams, useNavigate } from 'react-router-dom';
import { renderWithClient } from '../../test/queryWrapper';

/**
 * Exposes the query string (where the applied filter lives) and a real
 * history-back button — ``navigate(-1)`` is the same history ``go`` the browser
 * button calls, and MemoryRouter has no address bar of its own.
 */
function Spy() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  return (
    <>
      <div data-testid="qs">{params.toString()}</div>
      <button type="button" onClick={() => navigate(-1)}>go back in history</button>
    </>
  );
}

/**
 * The page needs a Router: ``ObjectDetail`` keeps the applied filter and the
 * page offset in the query string (``useSearchParams``), which throws outside
 * one. ``src/test/setup.js`` supplies a QueryClient to every render but no
 * router, and in the real app the route is ``/data-v2`` (``App.jsx``).
 *
 * Each test gets its own fresh history entry list, so no filter written by one
 * test can leak into the next.
 */
function renderPage(entry = '/data-v2') {
  return renderWithClient(
    <MemoryRouter initialEntries={[entry]}>
      <DataV2Page />
      <Spy />
    </MemoryRouter>,
  );
}

/** The query string as a plain object, so it can be compared exhaustively. */
function qs() {
  return Object.fromEntries(new URLSearchParams(screen.getByTestId('qs').textContent));
}

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Mock the v2 API client (declared before component import for hoisting).
// ---------------------------------------------------------------------------
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

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke.
//
// Only ``downloadFilename`` reaches Chart; the ``label`` ObjectDetail computes
// is rendered by SeriesChartV2 as its own <h2>, so ``chartHeading()`` below
// reads it from the real DOM rather than from a stub. That matters: with the
// heading unobserved, deleting ``serieTitle``'s object-level branch — or
// replacing the whole helper with `serie ${id}` — left all 46 tests green.
vi.mock('../../components/Chart', () => ({
  default: ({ downloadFilename }) => <div data-testid="chart" data-fn={downloadFilename} />,
}));

import DataV2Page from './DataV2Page';
import {
  listObjectsV2,
  getObjectDetailV2,
  getObjectFacetsV2,
  getObjectSeriesV2,
  getSeriesV2,
  getContinuousOptionsV2,
} from '../../api/dataV2';

const LIVE_OBJECTS = [
  { object_id: 1, kind: 'rate', symbol: 'RATE_US_CMT_1M', name: 'CMT 1M', cycle: null, underlying_object_id: null },
  { object_id: 3, kind: 'rate', symbol: 'RATE_US_SOFR_ON', name: 'SOFR ON', cycle: null, underlying_object_id: null },
  { object_id: 5, kind: 'index', symbol: 'IND_SP_500', name: 'S&P 500', cycle: null, underlying_object_id: null },
  { object_id: 6, kind: 'future', symbol: 'FUT_SP_500', name: 'E-mini S&P', cycle: 'M', underlying_object_id: 5 },
  { object_id: 7, kind: 'option', symbol: 'OPT_SP_500_EW3', name: 'SPX Weekly W3', cycle: 'W3', underlying_object_id: 6 },
];

// One filtered page containing the series the tests chart.
const PAGE_WITH_SERIE = {
  items: [{
    serie_id: 1433194, contract_id: 77, type: 'bbba', freq: '1m',
    source: 'DATABENTO:GLBX.MDP3:bbo-1m',
    contract_code: 'EW2H6 P6260.20260313',
    expiration: '2026-03-13', strike: 6260, option_type: 'put',
  }],
  total: 195, skip: 0, limit: 50,
};

// The same object after a filter change that excludes serie 1433194.
const PAGE_WITHOUT_SERIE = {
  items: [{
    serie_id: 1433195, contract_id: 78, type: 'bbba', freq: '1m',
    source: 'DATABENTO:GLBX.MDP3:bbo-1m',
    contract_code: 'EW2H6 C6300.20260313',
    expiration: '2026-03-13', strike: 6300, option_type: 'call',
  }],
  total: 12, skip: 0, limit: 50,
};

const OPTION_FACETS = {
  object_id: 7, kind: 'option',
  expirations: [{ expiration: '2026-03-13', contracts: 500 }],
  strike_min: 15, strike_max: 10600,
  option_types: ['call', 'put'],
  serie_types: [{ type: 'bbba', freq: '1m' }, { type: 'bar', freq: 'daily' }],
  totals: { contracts: 96106, series: 200672 },
};

// An index object has no contracts, so /facets offers no expiration, strike or
// option-type dimension and every contract field on its series is null.
const INDEX_FACETS = {
  object_id: 5, kind: 'index',
  expirations: [], strike_min: null, strike_max: null, option_types: [],
  serie_types: [{ type: 'bar', freq: 'daily' }],
  totals: { contracts: 0, series: 1 },
};
const INDEX_PAGE = {
  items: [{
    serie_id: 5, contract_id: null, type: 'bar', freq: 'daily', source: 'stooq',
    contract_code: null, expiration: null, strike: null, option_type: null,
  }],
  total: 1, skip: 0, limit: 50,
};

/**
 * The charted series' heading, as SeriesChartV2 renders it from ObjectDetail's
 * ``label``. The object header is the first level-2 heading and the chart adds
 * the second, so demanding exactly two also makes "no chart rendered" a failure
 * instead of silently returning the object symbol.
 */
function chartHeading() {
  const headings = screen.getAllByRole('heading', { level: 2 });
  expect(headings.length).toBe(2);
  return headings[1].textContent;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listObjectsV2).mockResolvedValue(LIVE_OBJECTS);
  // Still mocked (and still exported) so an accidental re-introduction of the
  // /objects/{id} fetch is observable rather than a network error. The shape
  // matches the slimmed endpoint: metadata only, no contracts/series.
  vi.mocked(getObjectDetailV2).mockResolvedValue({ object: LIVE_OBJECTS[4] });
  vi.mocked(getObjectFacetsV2).mockImplementation(
    async (objectId) => (objectId === 5 ? INDEX_FACETS : OPTION_FACETS),
  );
  vi.mocked(getObjectSeriesV2).mockImplementation(
    async (objectId) => (objectId === 5 ? INDEX_PAGE : PAGE_WITH_SERIE),
  );
  // Enough points for SeriesChartV2 to reach its <Chart> branch (it renders a
  // "No data" status for an empty payload, which would hide the chart and make
  // the "chart survives a filter change" assertions vacuous).
  vi.mocked(getSeriesV2).mockResolvedValue({
    serie_id: 1433194, type: 'bbba', grain: 'intraday',
    fields: ['best_bid'],
    points: {
      ts: ['2026-03-02T14:31:00Z', '2026-03-02T14:32:00Z'],
      best_bid: [12.5, 12.75],
    },
  });
  vi.mocked(getContinuousOptionsV2).mockResolvedValue({
    points: { ts: [], value: [] }, roll_dates: [], contracts: [],
  });
});

// jest-dom is NOT configured in this project — assert native DOM properties.
describe('DataV2Page', () => {
  it('groups objects by kind and lists their symbols', async () => {
    renderPage();
    expect(await screen.findByText('RATE_US_CMT_1M')).toBeDefined();
    expect(screen.getByText('IND_SP_500')).toBeDefined();
    expect(screen.getByText('FUT_SP_500')).toBeDefined();
    expect(screen.getByText('OPT_SP_500_EW3')).toBeDefined();
    // Kind group headers present.
    expect(screen.getByText('Rates')).toBeDefined();
    expect(screen.getByText('Futures')).toBeDefined();
    expect(screen.getByText('Options')).toBeDefined();
  });

  it('shows the filter panel and fetches nothing until Apply', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    expect(await screen.findByText(/Filters/)).toBeTruthy();
    // Cheap /facets is what the panel is built from.
    expect(getObjectFacetsV2).toHaveBeenCalledWith(7, expect.anything());
    // The series-list query must not have run yet — this is the gate.
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    // Nor may /objects/{id} be fetched. That request was the 38 MB / ~36 s
    // payload whose "Loading object…" gate froze this tab; it now returns
    // metadata only, but the tab still must not mount it — the browser list
    // already supplies every field the header renders, and re-adding the query
    // would put a loading gate back in front of the whole tab. This assertion
    // is what goes red if `useObjectDetailV2` is ever wired back in.
    expect(getObjectDetailV2).not.toHaveBeenCalled();
    // The prompt stands in for the result list until a filter exists.
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('lists series after Apply', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    await waitFor(() => expect(getObjectSeriesV2).toHaveBeenCalled());
    // Paging travels with the filters, on the keys the client allowlists.
    const [objectId, args] = vi.mocked(getObjectSeriesV2).mock.calls[0];
    expect(objectId).toBe(7);
    expect(args.skip).toBe(0);
    expect(args.limit).toBe(50);
    expect(args.serieType).toBe('any');
    // The filtered total comes from the page, not from the object.
    expect(screen.getByText(/^195 series/)).toBeTruthy();
  });

  it('pages forward without discarding the filter', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.change(await screen.findByLabelText('Series type'), {
      target: { value: 'bbba' },
    });
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    await screen.findByText('EW2H6 P6260.20260313');

    fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    await waitFor(() => {
      const last = vi.mocked(getObjectSeriesV2).mock.calls.at(-1)[1];
      expect(last.skip).toBe(50);
      // …and the filter is still the one the user chose.
      expect(last.serieType).toBe('bbba');
    });
  });

  it('keeps the chart when a filter change excludes the plotted series', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    fireEvent.click(await screen.findByText('EW2H6 P6260.20260313'));

    // Baseline: the chart is really mounted for THIS serie, un-flagged.
    const chart = await screen.findByTestId('chart');
    expect(chart.getAttribute('data-fn')).toBe('OPT_SP_500_EW3-1433194');
    expect(chartHeading()).toBe('OPT_SP_500_EW3 · EW2H6 P6260.20260313');
    expect(screen.queryByText(/outside the current filter/i)).toBeNull();

    // Narrow the filter so the plotted serie is no longer in the page.
    vi.mocked(getObjectSeriesV2).mockResolvedValue(PAGE_WITHOUT_SERIE);
    fireEvent.change(screen.getByLabelText('Series type'), {
      target: { value: 'bbba' },
    });

    // The new page arrived…
    expect(await screen.findByText('EW2H6 C6300.20260313')).toBeTruthy();
    expect(screen.queryByText('EW2H6 P6260.20260313')).toBeNull();
    // …and the chart survived it, flagged rather than erased.
    expect(await screen.findByText(/outside the current filter/i)).toBeTruthy();
    expect(screen.getByTestId('chart').getAttribute('data-fn'))
      .toBe('OPT_SP_500_EW3-1433194');
    // …WITH ITS IDENTITY: the row left the page, but the heading must not
    // degrade to "· serie 1433194". A chart whose name decays into a database
    // id is barely better than one that vanished, and a CSV downloaded in that
    // state is named by id too.
    expect(chartHeading()).toBe('OPT_SP_500_EW3 · EW2H6 P6260.20260313');
  });

  it('names an object-level serie after its object, in the row and the heading', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('IND_SP_500'));
    // An index object has no contract dimensions to filter on.
    expect(await screen.findByLabelText('Series type')).toBeTruthy();
    expect(screen.queryByLabelText('Expiration')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    // The ROW: every contract field on this serie is null, so without the
    // object symbol it renders the bare database id "serie 5". The title
    // attribute pins the label AND the meta line in one assertion.
    const row = await screen.findByTitle('IND_SP_500 — bar · daily');
    expect(row.tagName).toBe('BUTTON');
    expect(screen.queryByText('serie 5')).toBeNull();

    // The HEADING: "IND_SP_500", not "IND_SP_500 · serie 5" and not the
    // doubled "IND_SP_500 · IND_SP_500".
    fireEvent.click(row);
    await waitFor(() => expect(screen.getByTestId('chart')).toBeTruthy());
    expect(chartHeading()).toBe('IND_SP_500');
    expect(screen.getByTestId('chart').getAttribute('data-fn')).toBe('IND_SP_500-5');
  });

  it('Reset un-applies, rather than leaving a stale list beside a blank panel', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.change(await screen.findByLabelText('Series type'), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    // Assert the list is really there first, so its absence below cannot pass
    // just because nothing ever rendered.
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('195 series (1-1)');
    const callsBefore = vi.mocked(getObjectSeriesV2).mock.calls.length;
    expect(callsBefore).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: /reset/i }));

    // Back to the pre-Apply prompt…
    expect(await screen.findByText(/press Apply to list/i)).toBeTruthy();
    // …with the page produced by the cleared filter gone, not left on screen
    // silently stale.
    expect(screen.queryByText('EW2H6 P6260.20260313')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    // …and the panel's own fields cleared.
    expect(screen.getByLabelText('Series type').value).toBe('any');
    // …and no request issued: Reset re-gates, it does not apply an empty filter
    // (which would be the unbounded fetch this page exists to prevent).
    expect(vi.mocked(getObjectSeriesV2).mock.calls.length).toBe(callsBefore);

    // Reset re-arms rather than breaking: Apply works again.
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
  });

  // ---------------------------------------------------------------------
  // The filter is in the URL, and the URL outlives a component that is keyed
  // on the object. These two tests pull in OPPOSITE directions and both are
  // required: the filter must follow a shared link across the recipient's
  // first object pick, and must NOT follow ordinary browsing.
  // ---------------------------------------------------------------------

  it('does not carry an applied filter over to another object', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.change(await screen.findByLabelText('Series type'), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(qs()).toEqual({ serie_type: 'bbba' });
    const callsBefore = vi.mocked(getObjectSeriesV2).mock.calls.length;

    // Switch to a different object.
    fireEvent.click(screen.getByText('FUT_SP_500'));

    /*
     * The panel is rebuilt from the NEW object's facets, so a filter left in
     * the URL is one the panel cannot always display: a <select> whose value is
     * not among its options falls back to the first one ("Any"), so the panel
     * would report "no type filter" while ``serie_type=bbba`` was still being
     * applied to the request. An empty list next to a panel claiming to filter
     * nothing, with only the address bar telling the truth.
     */
    expect(await screen.findByText(/press Apply to list/i)).toBeTruthy();
    expect(qs()).toEqual({});
    // The panel is rebuilt from the new object's facets, which arrive after the
    // prompt renders (the prompt does not wait on /facets).
    expect((await screen.findByLabelText('Series type')).value).toBe('any');
    // …and no request was issued for the new object — the gate applies to it
    // exactly as it did to the first one.
    const newCalls = vi.mocked(getObjectSeriesV2).mock.calls.slice(callsBefore);
    expect(newCalls.map(([objectId]) => objectId)).toEqual([]);
  });

  it('does not re-apply the previous object\'s filter after a Back press onto the new object', async () => {
    /*
     * The regression the object-switch clearing exists to prevent, reached with
     * one keypress: apply a filter on A, switch to B (the URL is cleared with a
     * history PUSH), then press Back. The router restores A's query string, but
     * ``selected`` is still B. Without binding the applied filter to the object
     * it was produced for, B would re-derive and fetch A's filter and the panel
     * would misreport it (a <select> value B lacks falls back to "Any"). The fix
     * stamps each write with its object, so a filter is never read against a
     * different object than the one that produced it.
     */
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.change(await screen.findByLabelText('Series type'), {
      target: { value: 'bbba' },
    });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(qs()).toEqual({ serie_type: 'bbba' });

    // Switch to a different object — the filter is cleared for it (PUSH).
    fireEvent.click(screen.getByText('FUT_SP_500'));
    expect(await screen.findByText(/press Apply to list/i)).toBeTruthy();
    expect(qs()).toEqual({});
    const callsBefore = vi.mocked(getObjectSeriesV2).mock.calls.length;

    // Press Back: the router restores ?serie_type=bbba, but that filter belongs
    // to OPT (7), not FUT (6).
    fireEvent.click(screen.getByRole('button', { name: /go back in history/i }));

    // The panel and the request reflect FUT with NO stale filter…
    await waitFor(() => expect(screen.getByLabelText('Series type').value).toBe('any'));
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
    // …and no request was ever issued for FUT (6) — least of all bbba.
    const after = vi.mocked(getObjectSeriesV2).mock.calls.slice(callsBefore);
    expect(after.map(([objectId]) => objectId)).toEqual([]);
  });

  it("keeps a shared link's filter across the recipient's first object pick", async () => {
    // The object is deliberately NOT in the URL, so the recipient of a link
    // has to pick it from the browser list. That pick must not be mistaken for
    // "the user moved to another object" — it is the link being opened, and
    // clearing there would make every shared link arrive empty.
    renderPage('/data-v2?serie_type=bbba');
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));

    // No Apply click.
    expect(await screen.findByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(qs()).toEqual({ serie_type: 'bbba' });
    expect(screen.getByLabelText('Series type').value).toBe('bbba');
    const [objectId, args] = vi.mocked(getObjectSeriesV2).mock.calls[0];
    expect(objectId).toBe(7);
    expect(args.serieType).toBe('bbba');
  });

  it('shows the object total and the filtered total as different things', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    await screen.findByText('EW2H6 P6260.20260313');

    // Both counts are in one rendered body and they disagree (200 672 vs 195).
    // The panel's must read as the population being filtered, not as an answer
    // to "how many matched?" — locale-formatted, so built via toLocaleString
    // rather than hardcoded (this suite runs under fr-FR).
    const objectTotal = (200672).toLocaleString();
    const panelHeader = screen.getByText(/^Filters/);
    expect(panelHeader.textContent).toBe(`Filters · of ${objectTotal} series`);
    expect(panelHeader.textContent).not.toBe(`Filters · ${objectTotal} series`);
    expect(screen.getByRole('status').textContent).toBe('195 series (1-1)');
  });

  it('does not flag the chart when the new filter still contains it', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('button', { name: /apply/i }));
    fireEvent.click(await screen.findByText('EW2H6 P6260.20260313'));
    await screen.findByTestId('chart');

    // A filter change whose page still holds the plotted serie: same total
    // changes, so the list demonstrably re-rendered off a NEW response.
    vi.mocked(getObjectSeriesV2).mockResolvedValue({ ...PAGE_WITH_SERIE, total: 42 });
    fireEvent.change(screen.getByLabelText('Series type'), {
      target: { value: 'bbba' },
    });

    expect(await screen.findByText(/^42 series/)).toBeTruthy();
    expect(screen.queryByText(/outside the current filter/i)).toBeNull();
    expect(screen.getByTestId('chart').getAttribute('data-fn'))
      .toBe('OPT_SP_500_EW3-1433194');
  });

  // v2's fact_greeks is populated (11,580,236 rows, live-probed 2026-07-27), so
  // the old "greeks unavailable in v2" lockout is stale: Delta is selectable.
  // If the backend does not serve it, it answers 400 and the error block below
  // renders the message — graceful degradation, not a frontend guess.
  it('lets the user select the Delta criterion on the options continuous builder', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    // Switch to the Continuous (Options) tab.
    fireEvent.click(await screen.findByRole('tab', { name: /Continuous \(Options\)/i }));
    const deltaRadio = await screen.findByRole('radio', { name: /Delta/i });
    expect(deltaRadio.disabled).toBe(false);
    // No hover-only disabled-reason left on the label.
    expect(deltaRadio.closest('label').getAttribute('title')).toBeNull();

    fireEvent.click(deltaRadio);
    expect(deltaRadio.checked).toBe(true);
  });

  it('states the Roll lock as VISIBLE text, not a hover-only title', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    fireEvent.click(await screen.findByRole('tab', { name: /Continuous \(Options\)/i }));
    // Visible in the DOM (readable on touch + by screen readers), and wired to
    // the disabled control via aria-describedby.
    const note = await screen.findByText('v2 options roll at expiry');
    expect(note).toBeTruthy();
    const roll = screen.getByRole('combobox', { name: /Roll/i });
    expect(roll.getAttribute('aria-describedby')).toBe(note.id);
  });
});
