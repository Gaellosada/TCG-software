// @vitest-environment jsdom
//
// Proof that InstrumentPickerModal's basket save is WIRED to invalidation:
// saving a basket in the composer (createBasket) invalidates the saved-baskets
// queries so they refetch — without reopening the modal. Also confirms no
// unrelated persistence list (signals/portfolios) is refetched.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(() => Promise.resolve(['INDEX', 'ETF', 'FUT_SP_500'])),
  listInstruments: vi.fn(() => Promise.resolve({ items: [{ symbol: 'SPY' }] })),
  getAvailableCycles: vi.fn(() => Promise.resolve(['M'])),
}));
vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(() => Promise.resolve({ roots: [] })),
}));

const mockListBaskets = vi.fn(() => Promise.resolve([]));
const mockCreateBasket = vi.fn(() => Promise.resolve({ id: 'BSK_NEW', name: 'My Basket' }));
const mockListSignals = vi.fn(() => Promise.resolve([]));
const mockListPortfolios = vi.fn(() => Promise.resolve([]));
vi.mock('../../api/persistence', () => ({
  createBasket: (...a) => mockCreateBasket(...a),
  listBaskets: (...a) => mockListBaskets(...a),
  listSignals: (...a) => mockListSignals(...a),
  listPortfolios: (...a) => mockListPortfolios(...a),
}));

import InstrumentPickerModal from './InstrumentPickerModal';

beforeEach(() => {
  mockListBaskets.mockReset().mockResolvedValue([]);
  mockCreateBasket.mockReset().mockResolvedValue({ id: 'BSK_NEW', name: 'My Basket' });
  mockListSignals.mockReset().mockResolvedValue([]);
  mockListPortfolios.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

describe('InstrumentPickerModal — basket save invalidation (C3)', () => {
  it('refetches the saved-baskets queries after a basket is saved, touching no other resource', async () => {
    render(<InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} allowBaskets />);

    // Enter the basket composer.
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // The three category lists loaded once each on open.
    await waitFor(() => expect(mockListBaskets).toHaveBeenCalled());
    const callsBefore = mockListBaskets.mock.calls.length; // 3 (RESEARCH/DEV/PROD)

    // Configure one equity leg.
    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    const inp = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(inp);
    fireEvent.change(inp, { target: { value: 'SP' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-SPY'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });

    // Save as basket… → enter name → confirm.
    fireEvent.click(screen.getByTestId('basket-save-btn'));
    fireEvent.change(await screen.findByTestId('basket-save-name-input'), { target: { value: 'My Basket' } });
    fireEvent.click(screen.getByTestId('basket-save-confirm'));

    // createBasket fired, then invalidation → the 3 basket queries refetch.
    await waitFor(() => expect(mockCreateBasket).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockListBaskets.mock.calls.length).toBe(callsBefore + 3));

    // Isolation: a basket save must not refetch signals or portfolios.
    expect(mockListSignals).not.toHaveBeenCalled();
    expect(mockListPortfolios).not.toHaveBeenCalled();
  });
});
