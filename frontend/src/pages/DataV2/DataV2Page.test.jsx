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
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke.
vi.mock('../../components/Chart', () => ({
  default: ({ downloadFilename }) => <div data-testid="chart" data-fn={downloadFilename} />,
}));

import DataV2Page from './DataV2Page';
import {
  listObjectsV2,
  getObjectDetailV2,
  getContinuousOptionsV2,
} from '../../api/dataV2';

const LIVE_OBJECTS = [
  { object_id: 1, kind: 'rate', symbol: 'RATE_US_CMT_1M', name: 'CMT 1M', cycle: null, underlying_object_id: null },
  { object_id: 3, kind: 'rate', symbol: 'RATE_US_SOFR_ON', name: 'SOFR ON', cycle: null, underlying_object_id: null },
  { object_id: 5, kind: 'index', symbol: 'IND_SP_500', name: 'S&P 500', cycle: null, underlying_object_id: null },
  { object_id: 6, kind: 'future', symbol: 'FUT_SP_500', name: 'E-mini S&P', cycle: 'M', underlying_object_id: 5 },
  { object_id: 7, kind: 'option', symbol: 'OPT_SP_500_EW3', name: 'SPX Weekly W3', cycle: 'W3', underlying_object_id: 6 },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listObjectsV2).mockResolvedValue(LIVE_OBJECTS);
  vi.mocked(getObjectDetailV2).mockResolvedValue({
    object: LIVE_OBJECTS[4],
    contracts: [
      { contract_id: 101, contract_code: 'EW3 4500P', expiration: '2024-03-15', strike: 4500, option_type: 'put', multiplier: 50 },
    ],
    series: [
      { serie_id: 201, contract_id: 101, type: 'bar', freq: 'daily', source: 'settle' },
    ],
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

  it('drills into an object and shows its series list', async () => {
    renderWithClient(<DataV2Page />);
    fireEvent.click(await screen.findByText('OPT_SP_500_EW3'));
    // Series item (contract_code) renders from the detail response.
    expect(await screen.findByText('EW3 4500P')).toBeDefined();
    expect(getObjectDetailV2).toHaveBeenCalledWith(7, expect.anything());
  });

  // v2's fact_greeks is populated (11,580,236 rows, live-probed 2026-07-27), so
  // the old "greeks unavailable in v2" lockout is stale: Delta is selectable.
  // If the backend does not serve it, it answers 400 and the error block below
  // renders the message — graceful degradation, not a frontend guess.
  it('lets the user select the Delta criterion on the options continuous builder', async () => {
    renderWithClient(<DataV2Page />);
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
    renderWithClient(<DataV2Page />);
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
