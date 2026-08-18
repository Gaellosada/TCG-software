// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';

const captured = { traces: null };

// SeriesChartV2 imports `Chart` from '../../components/Chart' (the barrel), the
// same path DataV2Page.test.jsx stubs. Capture the traces it receives.
vi.mock('../../components/Chart', () => ({
  default: ({ traces }) => {
    captured.traces = traces;
    return <div data-testid="chart" />;
  },
}));

vi.mock('../../hooks/marketQueries', () => ({
  useSeriesV2: vi.fn(),
}));

import SeriesChartV2 from './SeriesChartV2';
import { useSeriesV2 } from '../../hooks/marketQueries';

afterEach(cleanup);

function mockSeries(data) {
  useSeriesV2.mockReturnValue({ data, loading: false, error: null });
}

describe('SeriesChartV2 grain dispatch', () => {
  beforeEach(() => { vi.clearAllMocks(); captured.traces = null; });

  it('formats daily int dates as YYYY-MM-DD', () => {
    mockSeries({
      serie_id: 1, type: 'value', grain: 'daily',
      fields: ['value'],
      points: { ts: [20260601, 20260602], value: [1.5, 1.6] },
    });
    render(<SeriesChartV2 serieId={1} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual(['2026-06-01', '2026-06-02']);
  });

  it('passes intraday ISO timestamps through unchanged', () => {
    mockSeries({
      serie_id: 2, type: 'value', grain: 'intraday',
      fields: ['value'],
      points: {
        ts: ['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z'],
        value: [1.5, 1.6],
      },
    });
    render(<SeriesChartV2 serieId={2} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual([
      '2026-06-01T14:31:00Z',
      '2026-06-01T14:32:00Z',
    ]);
  });

  it('keeps distinct intraday minutes distinct', () => {
    mockSeries({
      serie_id: 3, type: 'bbba', grain: 'intraday',
      fields: ['best_bid_value', 'best_bid_volume', 'best_ask_value', 'best_ask_volume'],
      points: {
        ts: ['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z'],
        best_bid_value: [610.5, 608.5],
        best_bid_volume: [15, 15],
        best_ask_value: [612, 610],
        best_ask_volume: [15, 1],
      },
    });
    render(<SeriesChartV2 serieId={3} serieType="bbba" label="x" />);
    expect(new Set(captured.traces[0].x).size).toBe(2);
  });

  // ── Added beyond the brief: the three tests above all pass against the
  // pre-change implementation (`ts.map(formatDateInt)` unconditionally), because
  // formatDateInt is the identity on any string that is not 8 chars long — and
  // every ISO 8601 timestamp the backend can emit is at least 20. They lock the
  // contract in, but on their own they cannot show the dispatch is doing any
  // work. The cases below fail under mutations the three above survive.

  it('preserves sub-second precision in intraday timestamps', () => {
    // _ts_to_iso emits microseconds when the ts carries them, so ISO strings are
    // NOT fixed width. Any handling that slices at a fixed offset (or round-trips
    // through Date) silently rewrites the data; this pins pass-through instead.
    mockSeries({
      serie_id: 4, type: 'value', grain: 'intraday',
      fields: ['value'],
      points: {
        ts: ['2026-06-01T14:31:00.123456Z', '2026-06-01T14:31:00.654321Z'],
        value: [1.5, 1.6],
      },
    });
    render(<SeriesChartV2 serieId={4} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual([
      '2026-06-01T14:31:00.123456Z',
      '2026-06-01T14:31:00.654321Z',
    ]);
  });

  it('treats a payload with no grain field as daily', () => {
    // Defensive default: a cached/older payload without `grain` must keep the v1
    // behaviour (YYYYMMDD ints formatted), never be read as intraday — passing
    // ints through would put the series on an epoch-ms axis in 1970.
    mockSeries({
      serie_id: 5, type: 'value',
      fields: ['value'],
      points: { ts: [20260601, 20260602], value: [1.5, 1.6] },
    });
    render(<SeriesChartV2 serieId={5} serieType="value" label="x" />);
    expect(captured.traces[0].x).toEqual(['2026-06-01', '2026-06-02']);
  });

  it('applies the same x to every trace of an intraday bar series', () => {
    // `bar` builds two traces (OHLC + volume); both must carry the ISO x, so the
    // volume bars line up with the candles on the datetime axis.
    mockSeries({
      serie_id: 6, type: 'bar', grain: 'intraday',
      fields: ['open', 'high', 'low', 'close', 'volume'],
      points: {
        ts: ['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z'],
        open: [10, 11], high: [12, 13], low: [9, 10], close: [11, 12],
        volume: [100, 200],
      },
    });
    render(<SeriesChartV2 serieId={6} serieType="bar" label="x" />);
    expect(captured.traces.length).toBe(2);
    expect(captured.traces[1].name).toBe('Volume');
    for (const trace of captured.traces) {
      expect(trace.x).toEqual(['2026-06-01T14:31:00Z', '2026-06-01T14:32:00Z']);
    }
  });

  it('formats a daily bar series x for every trace', () => {
    mockSeries({
      serie_id: 7, type: 'bar', grain: 'daily',
      fields: ['open', 'high', 'low', 'close', 'volume'],
      points: {
        ts: [20260601, 20260602],
        open: [10, 11], high: [12, 13], low: [9, 10], close: [11, 12],
        volume: [100, 200],
      },
    });
    render(<SeriesChartV2 serieId={7} serieType="bar" label="x" />);
    expect(captured.traces.length).toBe(2);
    for (const trace of captured.traces) {
      expect(trace.x).toEqual(['2026-06-01', '2026-06-02']);
    }
  });
});
