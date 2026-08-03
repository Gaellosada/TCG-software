import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchOptionLegRange } from './optionLegRange';
import { getOptionCoverage } from '../../api/options';
import { queryKeys } from '../../queryKeys';

vi.mock('../../api/options', () => ({ getOptionCoverage: vi.fn() }));

// A queryClient stub that just runs the queryFn and records the key it was
// called with — enough to assert the range resolution is data-source-scoped.
function makeQC() {
  const keys = [];
  return {
    keys,
    fetchQuery: async ({ queryKey, queryFn }) => {
      keys.push(queryKey);
      return queryFn();
    },
  };
}

const LEG = { id: 'l1', type: 'option_stream', collection: 'OPT_SP_500' };

describe('fetchOptionLegRange data-source awareness', () => {
  beforeEach(() => getOptionCoverage.mockReset());

  it('resolves v2 coverage (later floor) when dataSource is v2', async () => {
    getOptionCoverage.mockResolvedValue({ start: '2011-06-15', end: '2026-07-21' });
    const qc = makeQC();
    const res = await fetchOptionLegRange(qc, LEG, 'v2');
    // No cadence fields in the payload → recommendedStart defaults to start,
    // segments to []. start/end unchanged.
    expect(res).toEqual({
      id: 'l1', start: '2011-06-15', end: '2026-07-21',
      recommendedStart: '2011-06-15', segments: [],
    });
    // The v2 warehouse is queried, and the cache key is v2-scoped so it can
    // never collide with the v1 coverage entry (different floor).
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v2');
    expect(qc.keys[0]).toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v2'));
    expect(qc.keys[0]).not.toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v1'));
  });

  it('defaults to v1 when no dataSource is given (byte-identical to before)', async () => {
    getOptionCoverage.mockResolvedValue({ start: '2005-12-01', end: '2026-06-12' });
    const qc = makeQC();
    const res = await fetchOptionLegRange(qc, LEG);
    expect(res).toEqual({
      id: 'l1', start: '2005-12-01', end: '2026-06-12',
      recommendedStart: '2005-12-01', segments: [],
    });
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v1');
    expect(qc.keys[0]).toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v1'));
  });
});

// A leg on a specific expiration cycle (e.g. EW3 / 'W3 Friday') has data that
// starts LATER than the collection as a whole. Its coverage query must be
// scoped to that cycle so the resolved range reflects the cycle's real data
// extent (not the collection's earlier floor).
const CYCLE_LEG = { id: 'l1', type: 'option_stream', collection: 'OPT_SP_500', cycle: 'W3 Friday' };

describe('fetchOptionLegRange cycle scoping', () => {
  beforeEach(() => getOptionCoverage.mockReset());

  it('forwards the leg cycle to the coverage query + cache key (v2)', async () => {
    // Cycle-scoped coverage starts at the cycle's true data start (2016), not
    // the collection floor (~2011).
    getOptionCoverage.mockResolvedValue({ start: '2016-02-22', end: '2026-07-21' });
    const qc = makeQC();
    const res = await fetchOptionLegRange(qc, CYCLE_LEG, 'v2');
    expect(res).toEqual({
      id: 'l1', start: '2016-02-22', end: '2026-07-21',
      recommendedStart: '2016-02-22', segments: [],
    });
    // The coverage call carries the cycle as the 3rd argument.
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v2', 'W3 Friday');
    // The cache key is cycle-scoped so two cycles never collide.
    expect(qc.keys[0]).toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v2', 'W3 Friday'));
    expect(qc.keys[0]).not.toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v2'));
    expect(qc.keys[0]).not.toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v2', 'M'));
  });

  it('forwards the cycle for v1 as well', async () => {
    getOptionCoverage.mockResolvedValue({ start: '2011-06-15', end: '2026-06-12' });
    const qc = makeQC();
    await fetchOptionLegRange(qc, CYCLE_LEG);
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v1', 'W3 Friday');
    expect(qc.keys[0]).toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v1', 'W3 Friday'));
  });

  it('a leg with no cycle makes a byte-identical (cycle-free) coverage call', async () => {
    getOptionCoverage.mockResolvedValue({ start: '2011-06-15', end: '2026-07-21' });
    const qc = makeQC();
    await fetchOptionLegRange(qc, LEG, 'v2');
    // Exactly two args — NO cycle appended → byte-identical to the pre-cycle call.
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v2');
    expect(getOptionCoverage.mock.calls[0]).toHaveLength(2);
  });

  it('treats an empty-string cycle as no cycle (byte-identical call)', async () => {
    getOptionCoverage.mockResolvedValue({ start: '2011-06-15', end: '2026-07-21' });
    const qc = makeQC();
    await fetchOptionLegRange(qc, { ...LEG, cycle: '' }, 'v2');
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v2');
    expect(getOptionCoverage.mock.calls[0]).toHaveLength(2);
  });

  it('passes through recommended_start + segments when the payload carries them', async () => {
    // The cadence-aware backend response (W3-v2 shape): a quarterly-only era
    // then a monthly era, with a recommendation in the monthly segment.
    getOptionCoverage.mockResolvedValue({
      start: '2010-06-07',
      end: '2026-07-27',
      recommended_start: '2016-05-01',
      segments: [
        { start: '2010-06-07', end: '2016-04-30', cadence: 'quarterly' },
        { start: '2016-05-01', end: '2026-07-27', cadence: 'monthly' },
      ],
    });
    const qc = makeQC();
    const res = await fetchOptionLegRange(qc, CYCLE_LEG, 'v2');
    expect(res.start).toBe('2010-06-07'); // raw floor preserved
    expect(res.end).toBe('2026-07-27');
    expect(res.recommendedStart).toBe('2016-05-01'); // monthly-era default
    expect(res.segments).toHaveLength(2);
    expect(res.segments[0].cadence).toBe('quarterly');
  });
});
