// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

afterEach(cleanup);

// Chart pulls in Plotly (canvas) — stub it so jsdom doesn't choke, and so we
// can assert the bar trace handed to it (same pattern as
// EventAttributionView.test.jsx / RegimeSensitivityView.test.jsx).
vi.mock('../../components/Chart', () => ({
  default: ({ traces, downloadFilename }) => (
    <div
      data-testid="ladder-analysis-chart"
      data-fn={downloadFilename}
      data-x={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].x : [])}
      data-y={JSON.stringify(Array.isArray(traces) && traces[0] ? traces[0].y : [])}
    />
  ),
}));

import LadderAnalysisView from './LadderAnalysisView';

function rung(date, hhmm, usd, status = 'ok', skipReason) {
  return {
    date,
    status,
    skip_reason: skipReason,
    entry_ts: `${date}T${hhmm}:00Z`,
    contracts: 1,
    pnl: status === 'ok' ? { total_pnl_usd: usd } : null,
    weighted_pnl_usd: status === 'ok' ? usd : 0,
  };
}

const LADDERED_DAYS = [
  {
    date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 300 },
    entries: [rung('2025-02-03', '10:00', 100), rung('2025-02-03', '10:30', 200)],
  },
  {
    date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -60 },
    entries: [
      rung('2025-02-04', '10:00', -50),
      rung('2025-02-04', '10:30', 0, 'skipped', 'max_concurrent'),
    ],
  },
];

const SINGLE_ENTRY_DAYS = [
  { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 5 } },
  { date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -5 } },
];

describe('LadderAnalysisView', () => {
  it('renders a hint (not a chart) for a non-laddered run', () => {
    render(<LadderAnalysisView days={SINGLE_ENTRY_DAYS} />);
    expect(screen.getByTestId('ladder-analysis-view')).toBeTruthy();
    expect(screen.getByTestId('ladder-analysis-hint').textContent).toMatch(/laddered entry/i);
    expect(screen.queryByTestId('ladder-analysis-chart')).toBeNull();
  });

  it('renders a hint for a run with only one rung per day', () => {
    const oneRungDays = [
      { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 5 }, entries: [rung('2025-02-03', '10:00', 5)] },
    ];
    render(<LadderAnalysisView days={oneRungDays} />);
    expect(screen.getByTestId('ladder-analysis-hint').textContent).toMatch(/single rung|nothing to compare/i);
  });

  it('renders per-rung buckets + N for a multi-day laddered run', () => {
    render(<LadderAnalysisView days={LADDERED_DAYS} />);
    expect(screen.getByTestId('ladder-analysis-view')).toBeTruthy();

    const chart = screen.getByTestId('ladder-analysis-chart');
    const x = JSON.parse(chart.getAttribute('data-x'));
    const y = JSON.parse(chart.getAttribute('data-y'));
    expect(x).toEqual(['10:00Z', '10:30Z']);
    // Rung 0 mean = (100 + -50)/2 = 25; rung 1 mean = (200 traded only)/1 = 200.
    expect(y[0]).toBeCloseTo(25, 6);
    expect(y[1]).toBeCloseTo(200, 6);

    const row0 = screen.getByTestId('ladder-analysis-row-0');
    expect(row0.getAttribute('data-n')).toBe('2');
    const row1 = screen.getByTestId('ladder-analysis-row-1');
    expect(row1.getAttribute('data-n')).toBe('1'); // one rung skipped (max_concurrent)
    expect(row1.textContent).toContain('1'); // skipped count column shows 1
  });
});
