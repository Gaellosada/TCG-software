// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
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

  it('optionStreamAllowedStreams=[mid] hides the Series selector in the drill-down', async () => {
    // Issue #2 (D1): a PORTFOLIO option leg is the option PRICE only — the
    // picker pins the stream to mid and hides the (pointless) Series selector.
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        optionStreamAllowedStreams={['mid']}
      />,
    );
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());
    // Rest of the form present, but NO Series selector.
    expect(screen.getByLabelText('Root')).toBeTruthy();
    expect(screen.queryByLabelText('Series')).toBeNull();
  });

  it('without optionStreamAllowedStreams the Series selector is shown (default)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());
    expect(screen.getByLabelText('Series')).toBeTruthy();
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

  it('existing futures drill-down emits continuous spec via shared <ContinuousSpecPicker> (Sign 10 regression)', async () => {
    // Sign 10: the <ContinuousSpecPicker> extraction must preserve the
    // existing futures drill-down's behaviour pixel-perfect — same
    // controls, same defaults, same emit shape.  This regression test
    // walks the futures drill-down path end-to-end.
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();

    // Expand Futures group → click FUT_ES → enter drill-down.
    await waitFor(() => expect(screen.getByText('Futures')).toBeTruthy());
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_ES')).toBeTruthy());
    fireEvent.click(screen.getByText('FUT_ES'));

    // The continuous-spec picker is rendered (single source of truth
    // shared with the basket-composer future leg).
    await waitFor(() => expect(screen.getByTestId('continuous-spec-picker')).toBeTruthy());

    // Tune adjustment + cycle + rollOffset.
    fireEvent.change(screen.getByTestId('continuous-spec-picker-adjustment'), { target: { value: 'difference' } });
    await waitFor(() => {
      const sel = screen.getByTestId('continuous-spec-picker-cycle');
      expect(sel.querySelector('option[value="H"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-cycle'), { target: { value: 'H' } });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), { target: { value: '7' } });

    // Click "Select Continuous Series" → emit shape is iter-0 stable.
    fireEvent.click(screen.getByText('Select Continuous Series'));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toEqual({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'difference',
      cycle: 'H',
      rollOffset: 7,
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('continuous roll-offset accepts up to 365 days and clamps beyond', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} />);
    await flushAsync();
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_ES')).toBeTruthy());
    fireEvent.click(screen.getByText('FUT_ES'));
    await waitFor(() => expect(screen.getByTestId('continuous-spec-picker')).toBeTruthy());

    const input = screen.getByTestId('continuous-spec-picker-roll-offset');
    // 90 (~3 months) is now accepted (was capped at 30).
    fireEvent.change(input, { target: { value: '90' } });
    fireEvent.click(screen.getByText('Select Continuous Series'));
    expect(onSelect.mock.calls[0][0].rollOffset).toBe(90);
  });

  it('continuous roll-offset clamps an over-max value to 365', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} />);
    await flushAsync();
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_ES')).toBeTruthy());
    fireEvent.click(screen.getByText('FUT_ES'));
    await waitFor(() => expect(screen.getByTestId('continuous-spec-picker')).toBeTruthy());

    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), {
      target: { value: '999' },
    });
    fireEvent.click(screen.getByText('Select Continuous Series'));
    expect(onSelect.mock.calls[0][0].rollOffset).toBe(365);
  });

  it('futures drill-down emits strategy=end_of_month when chosen (Issue #3)', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();

    await waitFor(() => expect(screen.getByText('Futures')).toBeTruthy());
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_ES')).toBeTruthy());
    fireEvent.click(screen.getByText('FUT_ES'));
    await waitFor(() => expect(screen.getByTestId('continuous-spec-picker')).toBeTruthy());

    // Pick the END_OF_MONTH roll strategy via the new select.
    fireEvent.change(screen.getByTestId('continuous-spec-picker-strategy'), {
      target: { value: 'end_of_month' },
    });
    fireEvent.click(screen.getByText('Select Continuous Series'));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'end_of_month',
    });
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
    // (The OptionStreamForm "Stream" control was relabelled "Series" in PR #58.)
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_VIX' } });
    fireEvent.change(screen.getByLabelText('Series'), { target: { value: 'gamma' } });

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
  // Inline emit path (Use without saving) — polymorphic leg shape (iter-3).
  //
  // The composer now emits `{instrument: <discriminated>, weight}` per
  // leg (iter-3 wire shape).  For `asset_class="future"` the renderer
  // is a collection select + <ContinuousSpecPicker>; for "equity" it's
  // the iter-1/2 spot typeahead; for "option" it's <OptionStreamForm>.
  // ────────────────────────────────────────────────────────────────────────

  it('emits an inline-shape descriptor with polymorphic continuous leg when future leg is configured', async () => {
    // Mock futures collection — future legs reference the collection,
    // not a specific contract.
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Default asset class is 'future' — the leg-0 row renders the
    // future-class picker (collection select + ContinuousSpecPicker).
    const collectionSelect = await screen.findByTestId('basket-leg-0-collection-select');
    fireEvent.change(collectionSelect, { target: { value: 'FUT_ES' } });

    // Set weight to +1
    const weight = screen.getByTestId('basket-leg-0-weight-input');
    fireEvent.change(weight, { target: { value: '1' } });

    const useBtn = screen.getByTestId('basket-use-btn');
    await waitFor(() => expect(useBtn.disabled).toBe(false));
    fireEvent.click(useBtn);

    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('basket');
    expect(emitted.kind).toBe('inline');
    expect(emitted.asset_class).toBe('future');
    expect(emitted.legs).toHaveLength(1);
    expect(emitted.legs[0].weight).toBe(1);
    expect(emitted.legs[0].instrument).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'none',
      cycle: null,
      rollOffset: 0,
      strategy: 'front_month',
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('future leg renders a <ContinuousSpecPicker> per leg (single source of truth shared with futures drill-down)', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // The continuous spec picker is in-tree inside the future leg row.
    expect(screen.getByTestId('continuous-spec-picker')).toBeTruthy();
    expect(screen.getByTestId('continuous-spec-picker-adjustment')).toBeTruthy();
    expect(screen.getByTestId('continuous-spec-picker-cycle')).toBeTruthy();
    expect(screen.getByTestId('continuous-spec-picker-roll-offset')).toBeTruthy();

    // The row carries the discriminator markers.
    const row = screen.getByTestId('basket-leg-0');
    expect(row.dataset.assetClass).toBe('future');
    expect(row.dataset.instrumentType).toBe('continuous');
  });

  it('continuous-spec edits flow into the emitted leg', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-adjustment'), { target: { value: 'ratio' } });
    // Wait for the picker to load cycles for FUT_ES.
    await waitFor(() => expect(getAvailableCycles).toHaveBeenCalledWith('FUT_ES'));
    await waitFor(() => {
      const sel = screen.getByTestId('continuous-spec-picker-cycle');
      expect(sel.querySelector('option[value="M"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-cycle'), { target: { value: 'M' } });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), { target: { value: '5' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '2' } });

    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.legs[0].instrument).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'ratio',
      cycle: 'M',
      rollOffset: 5,
      strategy: 'front_month',
    });
  });

  // ── Issue #3 (review r1 MAJOR): basket future leg must round-trip the roll
  // strategy through ContinuousLegPicker (it previously hardcoded front_month).
  it('basket future leg emits strategy=end_of_month when chosen', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('continuous-spec-picker-strategy'), { target: { value: 'end_of_month' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });

    fireEvent.click(screen.getByTestId('basket-use-btn'));
    expect(onSelect.mock.calls[0][0].legs[0].instrument).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'end_of_month',
    });
  });

  it('basket future leg strategy=end_of_month SURVIVES a non-strategy field edit (no silent revert)', async () => {
    // The r1 MAJOR: ContinuousLegPicker hardcoded strategy in the value it fed
    // ContinuousSpecPicker, whose emit spreads `value` — so editing adjustment
    // (or any non-strategy field) reverted end_of_month back to front_month.
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    // 1) choose end_of_month, THEN 2) edit a DIFFERENT field.
    fireEvent.change(screen.getByTestId('continuous-spec-picker-strategy'), { target: { value: 'end_of_month' } });
    // The select must DISPLAY end_of_month after the choice (not revert).
    await waitFor(() =>
      expect(screen.getByTestId('continuous-spec-picker-strategy').value).toBe('end_of_month'),
    );
    fireEvent.change(screen.getByTestId('continuous-spec-picker-adjustment'), { target: { value: 'ratio' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });

    fireEvent.click(screen.getByTestId('basket-use-btn'));
    // strategy MUST still be end_of_month despite the adjustment edit.
    expect(onSelect.mock.calls[0][0].legs[0].instrument).toMatchObject({
      adjustment: 'ratio',
      strategy: 'end_of_month',
    });
  });

  it('equity asset class renders the spot typeahead and emits a spot leg', async () => {
    vi.mocked(listCollections).mockResolvedValue(['ETF']);
    vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPY' }] });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Switch to equity (no confirm — legs are still empty).
    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });

    // Spot typeahead is back in the row.
    const input = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'SP' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-SPY'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));

    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.asset_class).toBe('equity');
    expect(emitted.legs[0]).toEqual({
      instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
      weight: 1,
    });
  });

  it('option asset class renders the option-stream picker and emits an option_stream leg', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'option' } });
    // Wait for OptionStreamForm to mount with the default spec.
    await waitFor(() => expect(screen.getByTestId('option-stream-picker')).toBeTruthy());
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    const row = screen.getByTestId('basket-leg-0');
    expect(row.dataset.instrumentType).toBe('option_stream');
  });

  it('option leg emits {instrument:{type:"option_stream", ...}, weight}', async () => {
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'option' } });
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    await waitFor(() => expect(screen.getByTestId('basket-use-btn').disabled).toBe(false));
    fireEvent.click(screen.getByTestId('basket-use-btn'));

    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.asset_class).toBe('option');
    expect(emitted.legs[0].weight).toBe(1);
    const inst = emitted.legs[0].instrument;
    expect(inst.type).toBe('option_stream');
    expect(inst.collection).toBe('OPT_SP_500');
    expect(inst.option_type).toMatch(/^[CP]$/);
    expect(inst.maturity).toBeTruthy();
    expect(inst.maturity.kind).toBeTruthy();
    expect(inst.selection).toBeTruthy();
    expect(inst.selection.kind).toBeTruthy();
    expect(typeof inst.stream).toBe('string');
  });

  it('composer cannot emit a strict-mismatched leg — future asset_class always emits continuous instrument.type', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Future class — configure leg.
    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[onSelect.mock.calls.length - 1][0];
    expect(emitted.asset_class).toBe('future');
    expect(emitted.legs[0].instrument.type).toBe('continuous'); // strict match
  });

  it('composer cannot emit a strict-mismatched leg — equity asset_class always emits spot instrument.type', async () => {
    vi.mocked(listCollections).mockResolvedValue(['ETF']);
    vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPY' }] });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    const inp = screen.getByTestId('basket-leg-0-instrument-input');
    fireEvent.focus(inp);
    fireEvent.change(inp, { target: { value: 'SP' } });
    fireEvent.mouseDown(await screen.findByTestId('basket-leg-0-suggestion-SPY'));
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[onSelect.mock.calls.length - 1][0];
    expect(emitted.asset_class).toBe('equity');
    expect(emitted.legs[0].instrument.type).toBe('spot'); // strict match
  });

  it('rejects a leg with weight=0 — CTAs stay disabled', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });

    const weight = screen.getByTestId('basket-leg-0-weight-input');
    fireEvent.change(weight, { target: { value: '0' } });

    expect(screen.getByTestId('basket-use-btn').disabled).toBe(true);
    expect(screen.getByTestId('basket-save-btn').disabled).toBe(true);
  });

  it('accepts a negative leg weight (short) and emits it on use', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '-0.5' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const leg = onSelect.mock.calls[0][0].legs[0];
    expect(leg.weight).toBe(-0.5);
    expect(leg.instrument.type).toBe('continuous');
    expect(leg.instrument.collection).toBe('FUT_ES');
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
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Populate the one and only leg (future asset class — pick collection).
    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
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

    // The replacement row is genuinely empty (no collection selected).
    const newSelect = screen.getByTestId('basket-leg-0-collection-select');
    expect(newSelect.value).toBe('');
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
  // Saved baskets dropdown → emit saved-ref (polymorphic shape).
  // ────────────────────────────────────────────────────────────────────────

  it('selecting a saved equity basket emits a saved-reference descriptor on Use', async () => {
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        {
          id: 'BSK_ABC',
          name: 'My Basket',
          asset_class: 'equity',
          legs: [{
            instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
            weight: 1.0,
          }],
        },
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

  it('saved basket with continuous legs loads under the future asset class', async () => {
    vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        {
          id: 'BSK_CONT',
          name: 'Cont Basket',
          asset_class: 'future',
          legs: [{
            instrument: {
              type: 'continuous', collection: 'FUT_ES', adjustment: 'ratio',
              cycle: 'M', rollOffset: 3, strategy: 'front_month',
            },
            weight: 2.0,
          }],
        },
      ];
      return [];
    });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    await waitFor(() => {
      const select = screen.getByTestId('basket-saved-select');
      expect(select.querySelector('option[value="BSK_CONT"]')).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId('basket-saved-select'), { target: { value: 'BSK_CONT' } });

    // Asset class flipped to future per the envelope.
    await waitFor(() => expect(screen.getByTestId('basket-asset-class-select').value).toBe('future'));
    expect(screen.getByTestId('basket-leg-0-collection-select').value).toBe('FUT_ES');
    expect(screen.getByTestId('basket-leg-0-weight-input').value).toBe('2');
  });

  // ────────────────────────────────────────────────────────────────────────
  // Save-as flow (polymorphic shape).
  // ────────────────────────────────────────────────────────────────────────

  it('Save-as flow: opens inline input → calls createBasket with polymorphic legs → banner + saved-ref emit', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_NEW_FROM_BE', name: 'My Save' });
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Configure one leg (future asset class — pick collection).
    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '2' } });

    // Open save input.
    const saveBtn = screen.getByTestId('basket-save-btn');
    await waitFor(() => expect(saveBtn.disabled).toBe(false));
    fireEvent.click(saveBtn);
    await waitFor(() => expect(screen.getByTestId('basket-save-input')).toBeTruthy());

    // Type a name + confirm.
    fireEvent.change(screen.getByTestId('basket-save-name-input'), { target: { value: 'My Save' } });
    fireEvent.click(screen.getByTestId('basket-save-confirm'));

    // createBasket was called with the polymorphic payload shape.
    await waitFor(() => expect(createBasket).toHaveBeenCalled());
    const payload = vi.mocked(createBasket).mock.calls[0][0];
    expect(payload.name).toBe('My Save');
    expect(payload.category).toBe('RESEARCH');
    expect(payload.asset_class).toBe('future');
    expect(payload.legs).toHaveLength(1);
    expect(payload.legs[0].weight).toBe(2);
    expect(payload.legs[0].instrument).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
    });

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
    // BE may echo a different id; we want to inspect what the FE sends.
    vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_ECHO', name });
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Configure one leg.
    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
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
          asset_class: 'equity',
          legs: [{
            instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
            weight: 1.0,
          }],
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

    // Use → emits inline (polymorphic shape).
    fireEvent.click(screen.getByTestId('basket-use-btn'));
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.kind).toBe('inline');
    expect(emitted.asset_class).toBe('equity');
    expect(emitted.legs[0].weight).toBe(3);
    expect(emitted.legs[0].instrument).toEqual({
      type: 'spot', collection: 'ETF', instrument_id: 'SPY',
    });
  });

  it('Unsave drops the saved reference but keeps current legs', async () => {
    vi.mocked(listBaskets).mockImplementation(async (cat) => {
      if (cat === 'RESEARCH') return [
        {
          id: 'BSK_ABC',
          name: 'My Basket',
          asset_class: 'equity',
          legs: [{
            instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
            weight: 1.0,
          }],
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
    // Banner gone; legs preserved (still equity-typeahead).
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
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Populate the future leg.
    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });

    // Switch asset class → confirm banner.
    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    await waitFor(() => expect(screen.getByTestId('basket-asset-class-confirm')).toBeTruthy());

    fireEvent.click(screen.getByTestId('basket-asset-class-confirm-yes'));
    // After confirm, legs cleared to a fresh empty equity row.
    expect(screen.getByTestId('basket-asset-class-select').value).toBe('equity');
    // Equity row uses the spot typeahead.
    expect(screen.getByTestId('basket-leg-0-instrument-input').value).toBe('');
  });

  it('asset-class change confirmation can be cancelled — legs preserved', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });

    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'equity' } });
    await waitFor(() => expect(screen.getByTestId('basket-asset-class-confirm')).toBeTruthy());

    fireEvent.click(screen.getByTestId('basket-asset-class-confirm-cancel'));
    expect(screen.getByTestId('basket-asset-class-select').value).toBe('future');
    expect(screen.getByTestId('basket-leg-0-collection-select').value).toBe('FUT_ES');
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

  // ────────────────────────────────────────────────────────────────────────
  // Bug 1 regression — two option legs hold independent option_type.
  //
  // Pre-fix the `<OptionStreamForm>` rendered all option-type radios with
  // a hard-coded `name="option-type"`.  Two simultaneously-mounted forms
  // (the basket composer with two option legs) joined a single browser
  // radio group, so clicking "Put" on leg 1 visually deselected leg 0's
  // "Call".  Fix: use `useId()` to scope the radio name per form
  // instance — verified by both the DOM `.checked` state on the sibling
  // leg AND the emitted wire payload carrying distinct `option_type`s.
  // ────────────────────────────────────────────────────────────────────────

  it('two option legs hold independent option_type (call+put) — Bug 1 regression', async () => {
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    // Switch to option asset class — first leg becomes an option_stream
    // composer (its <OptionStreamForm> auto-adopts a default spec with
    // option_type=C once the option roots resolve).
    fireEvent.change(screen.getByTestId('basket-asset-class-select'), { target: { value: 'option' } });
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    // Add a second option leg so two <OptionStreamForm> instances
    // coexist — the precondition that surfaces Bug 1.
    fireEvent.click(screen.getByTestId('basket-add-leg'));
    await waitFor(() => expect(screen.getAllByTestId('option-stream-form').length).toBe(2));

    // Both legs start with option_type=C (the form's default after
    // root adoption).  Flip leg 1 to "Put".
    const forms = screen.getAllByTestId('option-stream-form');
    const leg0Form = forms[0];
    const leg1Form = forms[1];
    const leg1Put = within(leg1Form).getByLabelText('Put');
    fireEvent.click(leg1Put);

    // Sibling leg 0's "Call" radio must remain checked — this is the
    // DOM-level proof that the radio-group share is broken.
    const leg0Call = within(leg0Form).getByLabelText('Call');
    const leg1PutAfter = within(leg1Form).getByLabelText('Put');
    expect(leg0Call.checked).toBe(true);
    expect(leg1PutAfter.checked).toBe(true);

    // And the emitted wire payload carries the distinct option_types,
    // proving the state isolation extends end-to-end (not just visually).
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('basket-leg-1-weight-input'), { target: { value: '1' } });
    await waitFor(() => expect(screen.getByTestId('basket-use-btn').disabled).toBe(false));
    fireEvent.click(screen.getByTestId('basket-use-btn'));

    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.legs).toHaveLength(2);
    expect(emitted.legs[0].instrument.option_type).toBe('C');
    expect(emitted.legs[1].instrument.option_type).toBe('P');
  });

  // ────────────────────────────────────────────────────────────────────────
  // Per-leg `__id` defensive — internal-only key MUST NOT leak into the
  // emitted wire payload.  Pins the emit-side stripping contract.
  // ────────────────────────────────────────────────────────────────────────

  it('emit payload does not carry the internal __id leg key', async () => {
    vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} allowBaskets={true} />);
    await flushAsync();
    fireEvent.click(await screen.findByTestId('picker-baskets-toggle'));
    await waitFor(() => expect(screen.getByTestId('basket-composer')).toBeTruthy());

    fireEvent.change(await screen.findByTestId('basket-leg-0-collection-select'), { target: { value: 'FUT_ES' } });
    fireEvent.change(screen.getByTestId('basket-leg-0-weight-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('basket-use-btn'));

    const emitted = onSelect.mock.calls[0][0];
    for (const leg of emitted.legs) {
      expect('__id' in leg).toBe(false);
      expect(Object.keys(leg).sort()).toEqual(['instrument', 'weight']);
    }
  });
});
