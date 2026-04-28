// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useContractSeries } from './useContractSeries';

vi.mock('../../api/options', () => ({
  getOptionContract: vi.fn(),
}));

import { getOptionContract } from '../../api/options';

describe('useContractSeries', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns null and idle state when collection or contractId missing', async () => {
    const { result } = renderHook(() => useContractSeries(null, null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    expect(getOptionContract).not.toHaveBeenCalled();
  });

  it('fetches when both collection and contractId are provided', async () => {
    const payload = { contract: { contract_id: 'X|M' }, rows: [] };
    getOptionContract.mockResolvedValueOnce(payload);

    const { result } = renderHook(() =>
      useContractSeries('OPT_SP_500', 'X|M'),
    );

    await waitFor(() => expect(result.current.data).toEqual(payload));
    expect(getOptionContract).toHaveBeenCalledWith('OPT_SP_500', 'X|M', {
      computeMissing: false,
      dateFrom: null,
      dateTo: null,
    });
  });

  it('passes computeMissing/dateFrom/dateTo through to the API client', async () => {
    getOptionContract.mockResolvedValue({ contract: {}, rows: [] });

    renderHook(() =>
      useContractSeries('OPT_SP_500', 'X|M', {
        computeMissing: true,
        dateFrom: '2024-01-01',
        dateTo: '2024-12-31',
      }),
    );

    await waitFor(() => expect(getOptionContract).toHaveBeenCalled());
    expect(getOptionContract).toHaveBeenCalledWith('OPT_SP_500', 'X|M', {
      computeMissing: true,
      dateFrom: '2024-01-01',
      dateTo: '2024-12-31',
    });
  });

  it('re-fetches when computeMissing toggles', async () => {
    getOptionContract.mockResolvedValue({ contract: {}, rows: [] });

    const { rerender } = renderHook(
      ({ cm }) => useContractSeries('OPT_SP_500', 'X|M', { computeMissing: cm }),
      { initialProps: { cm: false } },
    );

    await waitFor(() => expect(getOptionContract).toHaveBeenCalledTimes(1));
    rerender({ cm: true });
    await waitFor(() => expect(getOptionContract).toHaveBeenCalledTimes(2));
  });

  it('surfaces fetch errors via the error field', async () => {
    getOptionContract.mockRejectedValueOnce(new Error('boom'));

    const { result } = renderHook(() =>
      useContractSeries('OPT_SP_500', 'X|M'),
    );

    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current.error.message).toBe('boom');
    expect(result.current.data).toBeNull();
  });
});
