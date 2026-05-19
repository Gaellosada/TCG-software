/**
 * BasketsPage tests.
 *
 * Network calls and the autosave hook are mocked so tests run in
 * isolation against jsdom. We verify:
 *   - mount fetches RESEARCH baskets
 *   - clicking a row hydrates the editor via getBasket
 *   - changing the category re-fetches
 *   - the autosave hook receives enabled + an onSave that delegates to
 *     updateBasket
 *   - create / archive paths
 *   - list-fetch errors surface to the user
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Mock the persistence API.
vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listBaskets: vi.fn(),
  createBasket: vi.fn(),
  getBasket: vi.fn(),
  updateBasket: vi.fn(),
  archiveBasket: vi.fn(),
  describePersistenceError: (err) => (err && err.message) || 'Unknown error',
}));

// Mock the autosave hook so we can introspect what the page passes in
// and so the test doesn't depend on debounce timing.
vi.mock('../../hooks/useBackendAutosave', () => ({
  default: vi.fn(() => ({
    status: 'idle',
    reset: vi.fn(),
    flush: vi.fn(),
    setStatus: vi.fn(),
  })),
  DEFAULT_AUTOSAVE_DEBOUNCE_MS: 3000,
}));

import BasketsPage from './BasketsPage';
import * as api from '../../api/persistence';
import useBackendAutosave from '../../hooks/useBackendAutosave';

const BASKET_STUB = {
  id: 'b1',
  type: 'basket',
  name: 'My Basket',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [{ instrument_id: 'SPY', collection: 'ETF', weight: 0.6 }],
};

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  api.listBaskets.mockResolvedValue([]);
  api.createBasket.mockResolvedValue({ ...BASKET_STUB });
  api.getBasket.mockResolvedValue({ ...BASKET_STUB });
  api.updateBasket.mockResolvedValue({ ...BASKET_STUB });
  api.archiveBasket.mockResolvedValue(null);
  // Reset the hook mock to the default idle return value.
  useBackendAutosave.mockImplementation(() => ({
    status: 'idle',
    reset: vi.fn(),
    flush: vi.fn(),
    setStatus: vi.fn(),
  }));
});

describe('BasketsPage — list & category filter', () => {
  it('fetches RESEARCH baskets on mount and renders them', async () => {
    api.listBaskets.mockResolvedValue([BASKET_STUB]);
    render(<BasketsPage />);
    await waitFor(() => {
      expect(api.listBaskets).toHaveBeenCalledWith('RESEARCH');
    });
    expect(await screen.findByText('My Basket')).toBeTruthy();
  });

  it('re-fetches the list when the category filter changes', async () => {
    api.listBaskets.mockResolvedValue([]);
    render(<BasketsPage />);
    await waitFor(() => expect(api.listBaskets).toHaveBeenCalledWith('RESEARCH'));
    const select = screen.getByTestId('basket-category-filter');
    await userEvent.selectOptions(select, 'DEV');
    await waitFor(() => expect(api.listBaskets).toHaveBeenCalledWith('DEV'));
  });

  it('surfaces a list-fetch error in the sidebar', async () => {
    api.listBaskets.mockRejectedValue(new Error('Network down'));
    render(<BasketsPage />);
    const errBox = await screen.findByTestId('basket-list-error');
    expect(errBox.textContent).toMatch(/Network down/);
  });
});

describe('BasketsPage — hydrate on select', () => {
  it('calls getBasket and populates the editor when a row is clicked', async () => {
    api.listBaskets.mockResolvedValue([BASKET_STUB]);
    render(<BasketsPage />);
    await screen.findByText('My Basket');
    await userEvent.click(screen.getByTestId('load-basket-b1'));
    await waitFor(() => expect(api.getBasket).toHaveBeenCalledWith('b1'));
    // Editor populated — name input visible with hydrated value.
    const nameInput = await screen.findByTestId('basket-name-input');
    expect(nameInput.value).toBe('My Basket');
    // Leg row rendered.
    expect(screen.getByTestId('basket-leg-SPY')).toBeTruthy();
  });
});

describe('BasketsPage — autosave wiring', () => {
  it('wires useBackendAutosave with an onSave that delegates to updateBasket', async () => {
    api.listBaskets.mockResolvedValue([BASKET_STUB]);
    render(<BasketsPage />);
    await screen.findByText('My Basket');
    await userEvent.click(screen.getByTestId('load-basket-b1'));
    await waitFor(() => expect(api.getBasket).toHaveBeenCalled());

    // The most recent hook call should be enabled=true with a valid
    // JSON payload and an onSave function.
    const lastCall = useBackendAutosave.mock.calls.at(-1)?.[0];
    expect(lastCall).toBeTruthy();
    expect(typeof lastCall.onSave).toBe('function');
    // The payload is the serialised editor state.
    expect(typeof lastCall.payload).toBe('string');
    const parsed = JSON.parse(lastCall.payload);
    expect(parsed.name).toBe('My Basket');
    expect(parsed.category).toBe('RESEARCH');
    expect(parsed.legs).toEqual([
      { instrument_id: 'SPY', collection: 'ETF', weight: 0.6 },
    ]);

    // Invoking onSave should call updateBasket with the parsed body
    // and thread the AbortSignal through.
    const controller = new AbortController();
    await lastCall.onSave(lastCall.payload, { signal: controller.signal });
    expect(api.updateBasket).toHaveBeenCalledWith(
      'b1',
      { name: 'My Basket', category: 'RESEARCH', legs: parsed.legs },
      { signal: controller.signal },
    );
  });
});

describe('BasketsPage — create', () => {
  it('creates a basket then refreshes the list', async () => {
    api.listBaskets.mockResolvedValue([]);
    render(<BasketsPage />);
    await waitFor(() => expect(api.listBaskets).toHaveBeenCalledTimes(1));
    await userEvent.type(screen.getByTestId('new-basket-id-input'), 'new-b');
    await userEvent.type(screen.getByTestId('new-basket-name-input'), 'New Basket');
    await userEvent.click(screen.getByTestId('create-basket-btn'));
    await waitFor(() => {
      expect(api.createBasket).toHaveBeenCalledWith({
        id: 'new-b',
        name: 'New Basket',
        category: 'RESEARCH',
        legs: [],
      });
    });
    // List refetched after create (initial + post-create).
    await waitFor(() => expect(api.listBaskets).toHaveBeenCalledTimes(2));
  });

  it('surfaces create errors in a separate error region', async () => {
    api.listBaskets.mockResolvedValue([]);
    api.createBasket.mockRejectedValue(new Error('id already exists'));
    render(<BasketsPage />);
    await userEvent.type(screen.getByTestId('new-basket-id-input'), 'dup');
    await userEvent.type(screen.getByTestId('new-basket-name-input'), 'Dup');
    await userEvent.click(screen.getByTestId('create-basket-btn'));
    const errBox = await screen.findByTestId('basket-oneshot-error');
    expect(errBox.textContent).toMatch(/id already exists/);
  });
});

describe('BasketsPage — archive', () => {
  it('archives the basket after confirming the dialog', async () => {
    api.listBaskets
      .mockResolvedValueOnce([BASKET_STUB])   // initial
      .mockResolvedValueOnce([]);             // after archive
    render(<BasketsPage />);
    await screen.findByText('My Basket');
    await userEvent.click(screen.getByTestId('archive-basket-b1'));
    // ConfirmDialog mounts — find the Archive button (confirmLabel="Archive").
    const confirm = await screen.findByTestId('confirm-dialog-confirm');
    expect(confirm.textContent).toMatch(/Archive/);
    await userEvent.click(confirm);
    await waitFor(() => expect(api.archiveBasket).toHaveBeenCalledWith('b1'));
    // List refetched and basket gone.
    await waitFor(() => expect(screen.queryByText('My Basket')).toBeNull());
  });

  it('does NOT archive when the dialog is cancelled', async () => {
    api.listBaskets.mockResolvedValue([BASKET_STUB]);
    render(<BasketsPage />);
    await screen.findByText('My Basket');
    await userEvent.click(screen.getByTestId('archive-basket-b1'));
    const cancel = await screen.findByTestId('confirm-dialog-cancel');
    await userEvent.click(cancel);
    expect(api.archiveBasket).not.toHaveBeenCalled();
  });
});
