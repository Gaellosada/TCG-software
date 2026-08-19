// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke, and so we
// can assert the scatter trace handed to it (same pattern as
// WeekdayAttributionView.test.jsx).
vi.mock('../../components/Chart', () => ({
  default: ({ traces, downloadFilename }) => (
    <div
      data-testid="regime-chart"
      data-fn={downloadFilename}
      data-x={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].x : [])}
      data-y={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].y : [])}
    />
  ),
}));

import RegimeSensitivityView from './RegimeSensitivityView';

function decisionDay(date, usd, state, signals = {}) {
  return {
    date,
    status: 'ok',
    pnl: { option_pnl_pts: usd / 10, hedge_pnl_pts: 0, total_pnl_pts: usd / 10, total_pnl_usd: usd },
    regime: {
      state,
      side: state === 'hvol_on' ? 'long' : state === 'hvol_off' ? 'short' : 'flat',
      asof: 20250131,
      gate: null,
      signals: { h20: null, h30: null, h100: null, vvix: null, ...signals },
    },
  };
}

const REGIME_DAYS = [
  decisionDay('2025-02-03', 100, 'hvol_on', { vvix: 90, h20: 0.2, h100: 0.15 }),
  decisionDay('2025-02-04', -50, 'hvol_off', { vvix: 110, h20: 0.12, h100: 0.18 }),
  decisionDay('2025-02-05', 30, 'fallback', { vvix: null, h20: null, h100: null }),
];

const NO_REGIME_DAYS = [
  { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 100, total_pnl_pts: 10 } },
  { date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -50, total_pnl_pts: -5 } },
];

describe('RegimeSensitivityView — regime data present', () => {
  it('mounts and shows a bucket row per state with N', () => {
    render(<RegimeSensitivityView days={REGIME_DAYS} />);
    expect(screen.getByTestId('regime-sensitivity-view')).toBeTruthy();
    expect(screen.getByTestId('regime-row-hvol_on').dataset.n).toBe('1');
    expect(screen.getByTestId('regime-row-hvol_off').dataset.n).toBe('1');
    expect(screen.getByTestId('regime-row-fallback').dataset.n).toBe('1');
    expect(screen.getByTestId('regime-row-extremely_low').dataset.n).toBe('0');
  });

  it('renders the default VVIX scatter with one point per day carrying a vvix signal', () => {
    render(<RegimeSensitivityView days={REGIME_DAYS} />);
    const chart = screen.getByTestId('regime-chart');
    // 2025-02-05 has a null vvix -> excluded (not plotted as 0).
    expect(JSON.parse(chart.dataset.x)).toEqual([90, 110]);
    expect(JSON.parse(chart.dataset.y)).toEqual([100, -50]);
  });

  it('switches the scatter to the RV term-structure signal via the selector', () => {
    render(<RegimeSensitivityView days={REGIME_DAYS} />);
    fireEvent.change(screen.getByTestId('regime-signal-select'), { target: { value: 'rvSpread' } });
    const chart = screen.getByTestId('regime-chart');
    expect(JSON.parse(chart.dataset.x)).toEqual([0.2 - 0.15, 0.12 - 0.18]);
  });

  it('surfaces the small-sample robustness caveat', () => {
    render(<RegimeSensitivityView days={REGIME_DAYS} />);
    expect(screen.getByTestId('regime-sensitivity-caveat').textContent).toMatch(/sample/i);
  });
});

describe('RegimeSensitivityView — regime data absent', () => {
  it('shows a concise enable-regime hint instead of an empty/broken chart', () => {
    render(<RegimeSensitivityView days={NO_REGIME_DAYS} />);
    expect(screen.getByTestId('regime-sensitivity-view')).toBeTruthy();
    expect(screen.getByTestId('regime-sensitivity-hint')).toBeTruthy();
    expect(screen.queryByTestId('regime-chart')).toBeNull();
    expect(screen.queryByTestId('regime-sensitivity-table')).toBeNull();
  });

  it('shows the hint for an empty days array too', () => {
    render(<RegimeSensitivityView days={[]} />);
    expect(screen.getByTestId('regime-sensitivity-hint')).toBeTruthy();
  });

  it('renders gracefully with an absent (undefined) days prop', () => {
    render(<RegimeSensitivityView />);
    expect(screen.getByTestId('regime-sensitivity-view')).toBeTruthy();
    expect(screen.getByTestId('regime-sensitivity-hint')).toBeTruthy();
  });
});
