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
    expect(res).toEqual({ id: 'l1', start: '2011-06-15', end: '2026-07-21' });
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
    expect(res).toEqual({ id: 'l1', start: '2005-12-01', end: '2026-06-12' });
    expect(getOptionCoverage).toHaveBeenCalledWith('OPT_SP_500', 'v1');
    expect(qc.keys[0]).toEqual(queryKeys.market.optionCoverage('OPT_SP_500', 'v1'));
  });
});
