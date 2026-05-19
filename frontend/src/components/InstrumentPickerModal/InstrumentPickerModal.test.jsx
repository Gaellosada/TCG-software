// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import InstrumentPickerModal from './InstrumentPickerModal';

afterEach(cleanup);

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
  getAvailableCycles: vi.fn(),
}));

vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(),
}));

vi.mock('../../api/persistence', () => ({
  createBasket: vi.fn(),
  listBaskets: vi.fn(),
}));

import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import { createBasket, listBaskets } from '../../api/persistence';

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
  { collection: 'OPT_VIX', root_label: 'VIX', name: 'VIX', has_greeks: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_SP_500']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
  vi.mocked(getAvailableCycles).mockResolvedValue(['M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
  vi.mocked(listBaskets).mockResolvedValue([]);
  vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_NEW', name: 'Test' });
});

async function flushAsync() {
  // Wait for async-loaded state to settle
  await waitFor(() => {
    // groups have rendered if at least one Indexes/Futures/Options label is visible
    expect(
      screen.queryAllByText(/Indexes|Assets|Futures|Options/).length,
    ).toBeGreaterThan(0);
  });
}

describe('<InstrumentPickerModal>', () => {
  it('returns null when isOpen is false', () => {
    const { container } = render(
      <InstrumentPickerModal isOpen={false} onClose={vi.fn()} onSelect={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders all 4 categories by default (with Options)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    // Wait for fut collections + option roots
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
      expect(screen.getByText('Options')).toBeTruthy();
    });
    expect(screen.getByText('Indexes')).toBeTruthy();
    expect(screen.getByText('Assets')).toBeTruthy();
  });

  it('hiddenCategories=["options"] hides the Options tab', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['options']}
      />,
    );
    await flushAsync();
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
    });
    expect(screen.queryByText('Options')).toBeNull();
    expect(screen.queryByTestId('picker-options-toggle')).toBeNull();
  });

  it('hiddenCategories=["options"] does not call getOptionRoots', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['options']}
      />,
    );
    await flushAsync();
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
    });
    expect(getOptionRoots).not.toHaveBeenCalled();
  });

  it('hiddenCategories=["futures","options"] hides both', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['futures', 'options']}
      />,
    );
    await flushAsync();
    expect(screen.queryByText('Options')).toBeNull();
    expect(screen.queryByText('Futures')).toBeNull();
  });

  it('clicking Options enters drill-down and shows the OptionStreamForm', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => {
      expect(screen.getByTestId('option-stream-form')).toBeTruthy();
    });
    expect(screen.getByLabelText('Root')).toBeTruthy();
    expect(screen.getByTestId('option-stream-confirm')).toBeTruthy();
  });

  it('confirming emits an option_stream-shaped object via onSelect and closes', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    const confirm = screen.getByTestId('option-stream-confirm');
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('option_stream');
    expect(emitted.collection).toBe('OPT_SP_500');
    expect(emitted.option_type).toMatch(/^[CP]$/);
    expect(emitted.maturity).toBeTruthy();
    expect(emitted.maturity.kind).toBeTruthy();
    expect(emitted.selection).toBeTruthy();
    expect(emitted.selection.kind).toBeTruthy();
    expect(typeof emitted.stream).toBe('string');
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('back button returns from options drill-down to category list', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    // Header should now show 'Options' as title
    expect(screen.getByRole('heading', { name: 'Options' })).toBeTruthy();
    // Back button → leaves drill-down
    const backBtn = screen.getByText('←');
    fireEvent.click(backBtn);
    await waitFor(() => expect(screen.queryByTestId('option-stream-form')).toBeNull());
  });

  it('still emits a spot selection from the existing flow (regression)', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();
    // Open Indexes group (or Assets)
    const indexBtn = screen.getByText('Indexes');
    fireEvent.click(indexBtn);
    await waitFor(() => expect(screen.getByText('SPX')).toBeTruthy());
    fireEvent.click(screen.getByText('SPX'));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toMatchObject({
      type: 'spot',
      instrument_id: 'SPX',
    });
  });

  it('disables Confirm when validation fails (greek stream + no-greeks root)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    // Pick OPT_VIX (no greeks) and stream=gamma
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_VIX' } });
    fireEvent.change(screen.getByLabelText('Stream'), { target: { value: 'gamma' } });

    const confirm = screen.getByTestId('option-stream-confirm');
    expect(confirm.disabled).toBe(true);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Basket category visibility (Q5 — default-deny).
  // ────────────────────────────────────────────────────────────────────────

  it('does NOT render the Baskets category by default (default-deny)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Futures')).toBeTruthy());
    expect(screen.queryByText('Baskets')).toBeNull();
    expect(screen.queryByTestId('picker-baskets-toggle')).toBeNull();
  });

  it('does NOT call listBaskets when allowBaskets is unset', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Futures')).toBeTruthy());
    expect(listBaskets).not.toHaveBeenCalled();
  });

  it('renders the Baskets category when allowBaskets={true}', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Baskets')).toBeTruthy());
    expect(screen.getByTestId('picker-baskets-toggle')).toBeTruthy();
  });

  it('calls listBaskets for RESEARCH+DEV+PROD when allowBaskets={true}', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    await waitFor(() => expect(listBaskets).toHaveBeenCalled());
    const categories = vi.mocked(listBaskets).mock.calls.map((c) => c[0]).sort();
    expect(categories).toEqual(['DEV', 'PROD', 'RESEARCH']);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Composer entry + basic layout.
  // ────────────────────────────────────────────────────────────────────────

  it('opens the inline composer when the Baskets tile is clicked', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByTestId('picker-baskets-toggle')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    // Header switches; one empty leg is visible by default.
    expect(screen.getByRole('heading', { name: 'Basket Composer' })).toBeTruthy();
    expect(screen.getByTestId('basket-leg-0')).toBeTruthy();
    expect(screen.getByTestId('basket-asset-class-select')).toBeTruthy();
    expect(screen.getByTestId('basket-saved-select')).toBeTruthy();
  });

  it('both CTAs are disabled when no leg is fully populated (0-leg disable)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    expect(screen.getByTestId('basket-use-btn').disabled).toBe(true);
    expect(screen.getByTestId('basket-save-btn').disabled).toBe(true);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Inline emit path (Use without saving).
  // ────────────────────────────────────────────────────────────────────────

  it('emits an inline-shape descriptor when a leg is configured and "Use" is clicked', async () => {
    // Mock futures collection so the future asset-class has candidates.
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Default asset class is 'future' — wait for futures instruments to load.
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    // Type into the leg-0 instrument input → pick from suggestions.
    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    const suggestion = await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26');
    fireEvent.mouseDown(suggestion);

    // Set weight to +1
    const weight = screen.getByTestId('basket-leg-0-weight-input');
    fireEvent.change(weight, { target: { value: '1' } });

    const useBtn = screen.getByTestId('basket-use-btn');
    await waitFor(() => expect(useBtn.disabled).toBe(false));
    fireEvent.click(useBtn);

    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted).toEqual({
      type: 'basket',
      kind: 'inline',
      asset_class: 'future',
      legs: [{ instrument_id: 'ES_MAR26', weight: 1 }],
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('rejects a leg with weight=0 — CTAs stay disabled', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));

    const weight = screen.getByTestId('basket-leg-0-weight-input');
    fireEvent.change(weight, { target: { value: '0' } });

    expect(screen.getByTestId('basket-use-btn').disabled).toBe(true);
    expect(screen.getByTestId('basket-save-btn').disabled).toBe(true);
  });

  it('accepts a negative leg weight (short) and emits it on use', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '-0.5' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    expect(onSelect.mock.calls[0][0].legs[0]).toEqual({ instrument_id: 'ES_MAR26', weight: -0.5 });
  });

  it('removing the last leg falls back to a single empty row', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    fireEvent.click(screen.getByTestId('basket-leg-0-remove'));
    expect(screen.getByTestId('basket-leg-0')).toBeTruthy();
  });

  it('removing the last populated leg returns to one empty row + both CTAs disabled (no zero-leg DOM state)', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'ES_MAR26' }] });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    // Populate the one and only leg.
    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '2' } });

    // Both CTAs should be enabled now that the leg is fully configured.
    await waitFor(() => expect(screen.getByTestId('basket-use-btn').disabled).toBe(false));
    expect(screen.getByTestId('basket-save-btn').disabled).toBe(false);

    // Remove the only leg.
    fireEvent.click(screen.getByTestId('basket-leg-0-remove'));

    // Exactly one empty leg row is present — composer never goes 0-leg.
    expect(screen.getByTestId('basket-leg-0')).toBeTruthy();
    expect(screen.queryByTestId('basket-leg-1')).toBeNull();

    // And both CTAs are disabled again (no configured legs).
    expect(screen.getByTestId('basket-use-btn').disabled).toBe(true);
    expect(screen.getByTestId('basket-save-btn').disabled).toBe(true);

    // The replacement row is genuinely empty (no instrument selected).
    const newInput = screen.getByTestId('basket-leg-0-instrument-input');
    expect(newInput.value).toBe('');
  });

  it('Add leg appends an empty row', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    fireEvent.click(screen.getByTestId('basket-add-leg'));
    expect(screen.getByTestId('basket-leg-0')).toBeTruthy();
    expect(screen.getByTestId('basket-leg-1')).toBeTruthy();
  });

  // ────────────────────────────────────────────────────────────────────────
  // Saved baskets dropdown → emit saved-ref.
  // ────────────────────────────────────────────────────────────────────────

  it('selecting a saved basket emits a saved-reference descriptor on Use', async () => {
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        { id: 'BSK_ABC', name: 'My Basket', legs: [{ instrument_id: 'SPY', weight: 1.0, collection: 'ETF' }] },
      ];
      return [];
    });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Wait for the option to appear in the dropdown.
    await waitFor(() => {
      const select = screen.getByTestId('basket-saved-select');
      expect(select.querySelector('option[value="BSK_ABC"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('basket-saved-select'), { target: { value: 'BSK_ABC' } });

    // Banner confirms it's saved-clean.
    await waitFor(() => expect(screen.getByTestId('basket-saved-banner')).toBeTruthy());
    expect(screen.getByTestId('basket-saved-banner').textContent).toMatch(/Saved as/);

    // Use button label morphs.
    expect(screen.getByTestId('basket-use-btn').textContent).toMatch(/Use saved/);

    fireEvent.click(screen.getByTestId('basket-use-btn'));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toEqual({
      type: 'basket',
      kind: 'saved',
      basket_id: 'BSK_ABC',
    });
  });

  // ────────────────────────────────────────────────────────────────────────
  // Save-as flow.
  // ────────────────────────────────────────────────────────────────────────

  it('Save-as flow: opens inline input → calls createBasket → banner + saved-ref emit', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_NEW_FROM_BE', name: 'My Save' });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    // Configure one leg.
    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '2' } });

    // Open save input.
    const saveBtn = screen.getByTestId('basket-save-btn');
    await waitFor(() => expect(saveBtn.disabled).toBe(false));
    fireEvent.click(saveBtn);
    await waitFor(() => expect(screen.getByTestId('basket-save-input')).toBeTruthy());

    // Type a name + confirm.
    fireEvent.change(screen.getByTestId('basket-save-name-input'), { target: { value: 'My Save' } });
    fireEvent.click(screen.getByTestId('basket-save-confirm'));

    // createBasket was called with the right payload shape.
    await waitFor(() => expect(createBasket).toHaveBeenCalled());
    const payload = vi.mocked(createBasket).mock.calls[0][0];
    expect(payload.name).toBe('My Save');
    expect(payload.category).toBe('RESEARCH');
    expect(payload.legs).toEqual([
      { instrument_id: 'ES_MAR26', collection: 'FUT_ES', weight: 2 },
    ]);

    // Banner appears.
    await waitFor(() => expect(screen.getByTestId('basket-saved-banner')).toBeTruthy());
    expect(screen.getByTestId('basket-saved-banner').textContent).toMatch(/Saved as/);
    expect(screen.getByTestId('basket-save-btn').textContent).toMatch(/Saved/);

    // Now use → emits saved-ref.
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    expect(onSelect.mock.calls[0][0]).toEqual({
      type: 'basket',
      kind: 'saved',
      basket_id: 'BSK_NEW_FROM_BE',
    });
  });

  it.each([
    ['My Save'],
    ['  spaces  galore  '],
    ['weird/+=$#@!chars'],
    ['日本語unicode-name'],
    ['a'],
    ['UPPERCASE'],
    ['mix3d_NUMb3rs'],
  ])('client-minted basket id conforms to BSK_<SLUG>_<timestamp> shape: %s', async (name) => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'ES_MAR26' }] });
    // BE may echo a different id; we want to inspect what the FE sends.
    vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_ECHO', name });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    // Configure one leg.
    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });

    fireEvent.click(screen.getByTestId('basket-save-btn'));
    await waitFor(() => expect(screen.getByTestId('basket-save-input')).toBeTruthy());
    fireEvent.change(screen.getByTestId('basket-save-name-input'), { target: { value: name } });
    fireEvent.click(screen.getByTestId('basket-save-confirm'));

    await waitFor(() => expect(createBasket).toHaveBeenCalled());
    const payload = vi.mocked(createBasket).mock.calls[0][0];

    // Backend _ID_PATTERN: /^[A-Za-z0-9_\-:.]+$/ — only these characters,
    // total length 1..128. Also: prefix MUST be "BSK_" and there MUST be
    // a trailing "_<digits>" timestamp segment (Date.now() ms epoch).
    expect(typeof payload.id).toBe('string');
    expect(payload.id.length).toBeGreaterThanOrEqual(1);
    expect(payload.id.length).toBeLessThanOrEqual(128);
    expect(payload.id).toMatch(/^[A-Za-z0-9_\-:.]+$/);
    expect(payload.id).toMatch(/^BSK_/);
    expect(payload.id).toMatch(/_\d+$/);
    // The fully-anchored client-shape regex (single check, documents intent).
    expect(payload.id).toMatch(/^BSK_[A-Z0-9_]+_\d+$/);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Save → mutate-after-save → re-save state machine.
  // ────────────────────────────────────────────────────────────────────────

  it('mutating a saved basket flips the banner to "Modified" and reverts emit shape to inline', async () => {
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        {
          id: 'BSK_ABC',
          name: 'My Basket',
          legs: [{ instrument_id: 'SPY', weight: 1.0, collection: 'ETF' }],
        },
      ];
      return [];
    });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    await waitFor(() => {
      const select = screen.getByTestId('basket-saved-select');
      expect(select.querySelector('option[value="BSK_ABC"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('basket-saved-select'), { target: { value: 'BSK_ABC' } });

    // Saved-clean state.
    await waitFor(() => expect(screen.getByTestId('basket-saved-banner').textContent).toMatch(/Saved as/));
    expect(screen.getByTestId('basket-use-btn').textContent).toMatch(/Use saved/);

    // Mutate leg-0 weight → dirty.
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '3' } });
    await waitFor(() => expect(screen.getByTestId('basket-saved-banner').textContent).toMatch(/Modified/));
    expect(screen.getByTestId('basket-use-btn').textContent).toMatch(/Use without saving/);

    // Use → emits inline.
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.kind).toBe('inline');
    expect(emitted.legs[0].weight).toBe(3);
  });

  it('Unsave drops the saved reference but keeps current legs', async () => {
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        {
          id: 'BSK_ABC',
          name: 'My Basket',
          legs: [{ instrument_id: 'SPY', weight: 1.0, collection: 'ETF' }],
        },
      ];
      return [];
    });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    await waitFor(() => {
      const select = screen.getByTestId('basket-saved-select');
      expect(select.querySelector('option[value="BSK_ABC"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('basket-saved-select'), { target: { value: 'BSK_ABC' } });
    await waitFor(() => expect(screen.getByTestId('basket-saved-banner')).toBeTruthy());

    fireEvent.click(screen.getByTestId('basket-unsave-btn'));
    // Banner gone; legs preserved.
    expect(screen.queryByTestId('basket-saved-banner')).toBeNull();
    expect(screen.getByTestId('basket-leg-0-weight-input').value).toBe('1');

    // Use → emits inline (no saved ref).
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.kind).toBe('inline');
  });

  // ────────────────────────────────────────────────────────────────────────
  // Asset-class change with non-empty legs → confirm dialog clears legs.
  // ────────────────────────────────────────────────────────────────────────

  it('asset-class change with non-empty legs prompts confirm; confirm clears legs', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));

    // Switch asset class → confirm banner.
    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    await waitFor(() => expect(screen.getByTestId('basket-asset-class-confirm')).toBeTruthy());

    fireEvent.click(screen.getByTestId('basket-asset-class-confirm-yes'));
    // After confirm, legs cleared to a fresh empty row, asset class changed.
    expect(screen.getByTestId('basket-asset-class-select').value).toBe('equity');
    expect(screen.getByTestId('basket-leg-0-instrument-input').value).toBe('');
  });

  it('asset-class change confirmation can be cancelled — legs preserved', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockImplementation(async (coll) => {
      if (coll === 'FUT_ES') return { items: [{ symbol: 'ES_MAR26' }] };
      return { items: [{ symbol: 'SPX' }] };
    });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    await waitFor(() => expect(listInstruments).toHaveBeenCalledWith('FUT_ES', expect.anything()));

    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'ES' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-ES_MAR26'));

    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    await waitFor(() => expect(screen.getByTestId('basket-asset-class-confirm')).toBeTruthy());

    fireEvent.click(screen.getByTestId('basket-asset-class-confirm-cancel'));
    expect(screen.getByTestId('basket-asset-class-select').value).toBe('future');
    expect(screen.getByTestId('basket-leg-0-instrument-input').value).toBe('ES_MAR26');
  });

  // ────────────────────────────────────────────────────────────────────────
  // Back button leaves the composer without emitting.
  // ────────────────────────────────────────────────────────────────────────

  it('back button leaves the composer and returns to the category list', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());
    const backBtn = screen.getByText('←');
    fireEvent.click(backBtn);
    await waitFor(() => expect(screen.queryByTestId('basket-composer')).toBeNull());
  });
});
