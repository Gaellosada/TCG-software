// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Capture the props the component hands the shared Chart. `vi.hoisted` keeps
// the ref reachable from the hoisted `vi.mock` factory (a bare top-level `let`
// referenced inside vi.mock would trip vitest's hoist guard).
// ---------------------------------------------------------------------------
const { chartProps } = vi.hoisted(() => ({ chartProps: { current: null } }));

// Chart pulls in Plotly (canvas) — stub it, and record the props so we can
// assert on the roll markers the component built rather than booting Plotly.
vi.mock('../../components/Chart', () => ({
  default: (props) => {
    chartProps.current = props;
    return <div data-testid="chart" />;
  },
}));

// Mock the single data hook the component consumes so we can feed exact
// options-continuous payloads without a react-query round trip.
vi.mock('../../hooks/marketQueries', () => ({
  useContinuousOptionsV2: vi.fn(),
}));

import ContinuousOptionsChartV2 from './ContinuousOptionsChartV2';
import { useContinuousOptionsV2 } from '../../hooks/marketQueries';

beforeEach(() => {
  vi.clearAllMocks();
  chartProps.current = null;
});

function mockPayload(data) {
  vi.mocked(useContinuousOptionsV2).mockReturnValue({ data, loading: false, error: null });
}

// ---------------------------------------------------------------------------
// Multi-roll payload. Two rolls, three expiration segments, AND an
// intra-segment strike drift on the FIRST segment (moneyness reselection picks
// a new strike on bar 1 while still in the same expiration). This is the case
// that separates the correct per-BAR labeling (points.contract[i-1]/[i]) from a
// naive per-ROLL-index lookup into the de-duped `contracts` list.
//
//   idx  ts          contract              value   note
//   0    20240101    SPX240103P4500        10.5    segment 1 (exp 2024-01-03)
//   1    20240102    SPX240103P4550        11.0    segment 1, strike drifted
//   2    20240103    SPX240105P4600        12.25   ROLL #0 -> segment 2
//   3    20240104    SPX240105P4600        13.0    segment 2
//   4    20240105    SPX240108P4650        14.5    ROLL #1 -> segment 3
//
// roll_dates = [20240103, 20240105]
// `contracts` (de-duped, first-of-segment) = [P4500, P4600, P4650]
//
// Roll #0 is at bar i=2. Correct labels use the bars adjacent to the roll:
//   sell = points.contract[1] = SPX240103P4550   (the drifted strike)
//   buy  = points.contract[2] = SPX240105P4600
// A naive per-roll-index approach (contracts[k-... ] / contracts[k]) would emit
//   sell = contracts[0] = SPX240103P4500   <-- WRONG (the pre-drift strike)
// so we anti-regression-assert the sell is P4550 and specifically NOT P4500.
// ---------------------------------------------------------------------------
const MULTI_ROLL = {
  points: {
    ts: [20240101, 20240102, 20240103, 20240104, 20240105],
    value: [10.5, 11.0, 12.25, 13.0, 14.5],
    contract: [
      'SPX240103P4500',
      'SPX240103P4550',
      'SPX240105P4600',
      'SPX240105P4600',
      'SPX240108P4650',
    ],
  },
  roll_dates: [20240103, 20240105],
  // Present in the real payload but intentionally divergent from the per-bar
  // codes at the roll boundary — a per-roll-index labeler would read this.
  contracts: ['SPX240103P4500', 'SPX240105P4600', 'SPX240108P4650'],
};

describe('ContinuousOptionsChartV2 — roll-marker contract-label alignment', () => {
  it('labels each roll sell/buy with the exact contract active on the adjacent bars', () => {
    mockPayload(MULTI_ROLL);
    render(<ContinuousOptionsChartV2 objectId={7} symbol="OPT_SP_500_EW3" />);

    expect(screen.getByTestId('chart')).toBeDefined();
    const markers = chartProps.current?.markers;
    // 2 rolls x (sell + buy), all values finite -> 4 markers.
    expect(Array.isArray(markers)).toBe(true);
    expect(markers.length).toBe(4);

    const sells = markers.filter((m) => m.kind === 'sell');
    const buys = markers.filter((m) => m.kind === 'buy');
    expect(sells.length).toBe(2);
    expect(buys.length).toBe(2);

    // customdata = [contractCode, settlementValue].
    // Roll #0 (x=2024-01-03): sell bar i-1=1, buy bar i=2.
    expect(sells[0].x).toBe('2024-01-03');
    expect(sells[0].customdata[0]).toBe('SPX240103P4550'); // drifted strike, per-bar
    expect(sells[0].customdata[1]).toBe(11.0);
    expect(buys[0].x).toBe('2024-01-03');
    expect(buys[0].customdata[0]).toBe('SPX240105P4600');
    expect(buys[0].customdata[1]).toBe(12.25);

    // Roll #1 (x=2024-01-05): sell bar i-1=3, buy bar i=4.
    expect(sells[1].x).toBe('2024-01-05');
    expect(sells[1].customdata[0]).toBe('SPX240105P4600');
    expect(sells[1].customdata[1]).toBe(13.0);
    expect(buys[1].x).toBe('2024-01-05');
    expect(buys[1].customdata[0]).toBe('SPX240108P4650');
    expect(buys[1].customdata[1]).toBe(14.5);

    // ANTI-REGRESSION: a per-roll-index lookup into the de-duped `contracts`
    // list would label roll #0's sell as the pre-drift strike. Assert against
    // the exact wrong value so a regression to that logic fails loudly.
    expect(sells[0].customdata[0]).not.toBe('SPX240103P4500');
  });

  it('exposes distinct sell/buy hover templates that read customdata', () => {
    mockPayload(MULTI_ROLL);
    render(<ContinuousOptionsChartV2 objectId={7} symbol="OPT_SP_500_EW3" />);
    const tpl = chartProps.current?.markerHovertemplates;
    expect(tpl.sell).toContain('customdata[0]');
    expect(tpl.buy).toContain('customdata[0]');
    expect(tpl.sell).toContain('Sell');
    expect(tpl.buy).toContain('Buy');
  });

  it('thin data: points present but zero roll_dates -> chart with no markers, no crash', () => {
    mockPayload({
      points: { ts: [20240101, 20240102], value: [10, 11], contract: ['A', 'B'] },
      roll_dates: [],
      contracts: ['A'],
    });
    render(<ContinuousOptionsChartV2 objectId={7} symbol="OPT_SP_500_EW3" />);
    expect(screen.getByTestId('chart')).toBeDefined();
    expect(chartProps.current.markers).toEqual([]);
  });

  it('thin data: empty points -> no chart rendered, no crash', () => {
    mockPayload({ points: { ts: [], value: [], contract: [] }, roll_dates: [], contracts: [] });
    render(<ContinuousOptionsChartV2 objectId={7} symbol="OPT_SP_500_EW3" />);
    expect(screen.queryByTestId('chart')).toBeNull();
    // The pre-target snap notice stands in for the chart (target defaults blank).
    expect(chartProps.current).toBeNull();
  });

  it('drops a roll whose date is not the first bar (i<=0 guard) without crashing', () => {
    // A roll_date landing on bar 0 (i===0) has no sell bar (i-1) — the i<=0
    // guard skips it entirely. Only the genuine roll at 20240103 survives.
    mockPayload({
      points: {
        ts: [20240101, 20240102, 20240103],
        value: [10, 11, 12],
        contract: ['SPX_A', 'SPX_A', 'SPX_B'],
      },
      roll_dates: [20240101, 20240103],
      contracts: ['SPX_A', 'SPX_B'],
    });
    render(<ContinuousOptionsChartV2 objectId={7} symbol="OPT_SP_500_EW3" />);
    const markers = chartProps.current.markers;
    expect(markers.length).toBe(2); // sell+buy for the single valid roll
    expect(markers.every((m) => m.x === '2024-01-03')).toBe(true);
    expect(markers.find((m) => m.kind === 'sell').customdata[0]).toBe('SPX_A');
    expect(markers.find((m) => m.kind === 'buy').customdata[0]).toBe('SPX_B');
  });
});
