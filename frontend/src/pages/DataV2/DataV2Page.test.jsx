// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { renderWithClient } from '../../test/queryWrapper';

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
  // fat /objects/{id} fetch is observable rather than a network error.
  vi.mocked(getObjectDetailV2).mockResolvedValue({
    object: LIVE_OBJECTS[4], contracts: [], series: [],
  });
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
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    expect(await screen.findByText(/Filters/)).toBeTruthy();
    // Cheap /facets is what the panel is built from.
    expect(getObjectFacetsV2).toHaveBeenCalledWith(7, expect.anything());
    // The series-list query must not have run yet — this is the gate.
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    // Nor may the fat /objects/{id} payload be fetched: it is the 38 MB /
    // ~38 s request whose "Loading object…" gate froze this tab.
    expect(getObjectDetailV2).not.toHaveBeenCalled();
    // The prompt stands in for the result list until a filter exists.
    expect(screen.getByText(/press Apply to list/i)).toBeTruthy();
  });

  it('lists series after Apply', async () => {
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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

  it('shows the object total and the filtered total as different things', async () => {
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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

  it('greys out the Delta criterion on the options continuous builder', async () => {
    renderWithClient(<DataV2Page />);
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    // Switch to the Continuous (Options) tab.
    fireEvent.click(await screen.findByRole('tab', { name: /Continuous \(Options\)/i }));
    const deltaRadio = await screen.findByRole('radio', { name: /Delta/i });
    expect(deltaRadio.disabled).toBe(true);
    // The wrapping label carries the "greeks unavailable in v2" tooltip.
    const label = deltaRadio.closest('label');
    expect(label.getAttribute('title')).toBe('greeks unavailable in v2');
  });
});
