// @vitest-environment jsdom
//
// Tests for BasketsSection — the Data-page "Baskets" list (browse + create +
// CRUD).  Mocks the persistence client + the shared picker modal so we can
// assert the section's wiring without the full composer.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { renderWithClient, makeTestClient } from '../../test/queryWrapper';

// --- mock the persistence client (list/update/archive) ---
const mockListBaskets = vi.fn();
const mockUpdateBasket = vi.fn(() => Promise.resolve({}));
const mockArchiveBasket = vi.fn(() => Promise.resolve(null));
vi.mock('../../api/persistence', () => ({
  listBaskets: (...a) => mockListBaskets(...a),
  updateBasket: (...a) => mockUpdateBasket(...a),
  archiveBasket: (...a) => mockArchiveBasket(...a),
  describePersistenceError: (err) => (err && err.message) || 'Request failed',
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
}));

// --- mock the picker modal: capture props, expose a button that emits a saved basket ---
let capturedModalProps = null;
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  default: (props) => {
    capturedModalProps = props;
    if (!props.isOpen) return null;
    return (
      <div data-testid="picker-modal">
        <button
          type="button"
          onClick={() =>
            props.onSelect({
              type: 'basket',
              kind: 'saved',
              basket_id: 'new-1',
              name: 'New Basket',
              asset_class: 'equity',
              legs: [],
            })
          }
        >
          emit-saved
        </button>
      </div>
    );
  },
}));

import BasketsSection from './BasketsSection';

const BASKET = {
  id: 'b1',
  type: 'basket',
  name: 'Tech Basket',
  category: 'RESEARCH',
  asset_class: 'equity',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  legs: [
    { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' }, weight: 0.6 },
    { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'QQQ' }, weight: 0.4 },
  ],
};

beforeEach(() => {
  capturedModalProps = null;
  // RESEARCH returns our basket; DEV/PROD empty.
  mockListBaskets.mockImplementation((cat) =>
    Promise.resolve(cat === 'RESEARCH' ? [BASKET] : []),
  );
  // Default happy-path mutations (individual tests override to reject).
  mockUpdateBasket.mockImplementation(() => Promise.resolve({}));
  mockArchiveBasket.mockImplementation(() => Promise.resolve(null));
});

afterEach(cleanup);

function renderSection(onSelect = vi.fn(), selected = null) {
  return renderWithClient(
    <BasketsSection selected={selected} onSelect={onSelect} />,
    makeTestClient(),
  );
}

describe('BasketsSection', () => {
  it('lists saved baskets under an expandable Baskets section', async () => {
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
  });

  it('selecting a basket emits a saved descriptor with legs', async () => {
    const onSelect = vi.fn();
    renderSection(onSelect);
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.click(screen.getByText('Tech Basket'));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'basket',
        basket: { kind: 'saved', basket_id: 'b1' },
        basket_id: 'b1',
        name: 'Tech Basket',
        asset_class: 'equity',
        legs: BASKET.legs,
      }),
    );
  });

  it('"+ New basket" opens the shared picker modal (allowBaskets)', async () => {
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('+ New basket')).toBeTruthy());
    fireEvent.click(screen.getByText('+ New basket'));
    expect(screen.getByTestId('picker-modal')).toBeTruthy();
    expect(capturedModalProps.allowBaskets).toBe(true);
  });

  it('emitting a saved basket from the modal selects it', async () => {
    const onSelect = vi.fn();
    renderSection(onSelect);
    fireEvent.click(screen.getByText('Baskets'));
    fireEvent.click(screen.getByText('+ New basket'));
    fireEvent.click(screen.getByText('emit-saved'));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'basket',
        basket: { kind: 'saved', basket_id: 'new-1' },
        basket_id: 'new-1',
      }),
    );
  });

  it('archive calls archiveBasket(id)', async () => {
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.click(screen.getByLabelText('Archive Tech Basket'));
    expect(mockArchiveBasket).toHaveBeenCalledWith('b1');
  });

  it('recategorize calls updateBasket with the new category', async () => {
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('Category for Tech Basket'), {
      target: { value: 'PROD' },
    });
    expect(mockUpdateBasket).toHaveBeenCalledWith(
      'b1',
      expect.objectContaining({ category: 'PROD', name: 'Tech Basket', legs: BASKET.legs }),
    );
  });

  it('surfaces a failed archive mutation as an inline error (no silent no-op)', async () => {
    mockArchiveBasket.mockImplementation(() =>
      Promise.reject(new Error('Server unreachable')),
    );
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.click(screen.getByLabelText('Archive Tech Basket'));
    // The rejection must surface (reusing describePersistenceError) — not vanish.
    await waitFor(() => {
      const alert = screen.getByRole('alert');
      expect(alert.textContent).toContain('Server unreachable');
    });
  });

  it('surfaces a failed recategorize mutation as an inline error', async () => {
    mockUpdateBasket.mockImplementation(() =>
      Promise.reject(new Error('Update rejected')),
    );
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('Category for Tech Basket'), {
      target: { value: 'PROD' },
    });
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toContain('Update rejected');
    });
  });

  it('clears a stale error on the next action', async () => {
    mockArchiveBasket.mockImplementation(() =>
      Promise.reject(new Error('Server unreachable')),
    );
    renderSection();
    fireEvent.click(screen.getByText('Baskets'));
    await waitFor(() => expect(screen.getByText('Tech Basket')).toBeTruthy());
    fireEvent.click(screen.getByLabelText('Archive Tech Basket'));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeTruthy());
    // A fresh action (create flow via handlePicked) must clear the stale error.
    fireEvent.click(screen.getByText('+ New basket'));
    fireEvent.click(screen.getByText('emit-saved'));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});
