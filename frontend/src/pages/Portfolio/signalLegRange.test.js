import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(),
  getContinuousSeries: vi.fn(),
}));

import { fetchSignalLegRange } from './signalLegRange';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';

beforeEach(() => {
  getInstrumentPrices.mockReset();
  getContinuousSeries.mockReset();
});

describe('fetchSignalLegRange', () => {
  it('returns {start:null, end:null} when no inputs are configured', async () => {
    const r = await fetchSignalLegRange({ id: 7, signalSpec: { inputs: [] } });
    expect(r).toEqual({ id: 7, start: null, end: null });
  });

  it('skips inputs without an instrument and returns null when none remain', async () => {
    const r = await fetchSignalLegRange({
      id: 5,
      signalSpec: { inputs: [{ id: 'X' }, { id: 'Y' }] },
    });
    expect(r).toEqual({ id: 5, start: null, end: null });
  });

  it('computes the overlap (latest start, earliest end) across inputs', async () => {
    // Dates as YYYYMMDD ints (formatDateInt input). The helper calls
    // formatDateInt on each endpoint; we return the values it will
    // compare lexicographically as YYYY-MM-DD.
    getInstrumentPrices.mockImplementation(async (_c, sym) => {
      if (sym === 'A') return { dates: [20200101, 20210101] };
      if (sym === 'B') return { dates: [20200601, 20200701] };
      return null;
    });
    const r = await fetchSignalLegRange({
      id: 3,
      signalSpec: {
        inputs: [
          { id: 'X', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'A' } },
          { id: 'Y', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'B' } },
        ],
      },
    });
    // Overlap = max(2020-01-01, 2020-06-01) → 2020-06-01,
    //           min(2021-01-01, 2020-07-01) → 2020-07-01
    expect(r).toEqual({ id: 3, start: '2020-06-01', end: '2020-07-01' });
  });

  it('returns {start:null, end:null} when inputs do not overlap (start > end)', async () => {
    getInstrumentPrices.mockImplementation(async (_c, sym) => {
      if (sym === 'A') return { dates: [20200101, 20200601] };
      if (sym === 'B') return { dates: [20210101, 20220101] };
      return null;
    });
    const r = await fetchSignalLegRange({
      id: 9,
      signalSpec: {
        inputs: [
          { id: 'X', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'A' } },
          { id: 'Y', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'B' } },
        ],
      },
    });
    expect(r).toEqual({ id: 9, start: null, end: null });
  });

  it('uses getContinuousSeries for continuous inputs', async () => {
    getContinuousSeries.mockResolvedValueOnce({ dates: [20220101, 20220601] });
    const r = await fetchSignalLegRange({
      id: 11,
      signalSpec: {
        inputs: [{
          id: 'Z',
          instrument: {
            type: 'continuous', collection: 'FUT_ES',
            strategy: 'front_month', adjustment: 'none',
            cycle: 'all', rollOffset: 2,
          },
        }],
      },
    });
    expect(getContinuousSeries).toHaveBeenCalledWith('FUT_ES', expect.objectContaining({
      strategy: 'front_month',
      adjustment: 'none',
      cycle: 'all',
      rollOffset: 2,
    }));
    expect(r).toEqual({ id: 11, start: '2022-01-01', end: '2022-06-01' });
  });
});
