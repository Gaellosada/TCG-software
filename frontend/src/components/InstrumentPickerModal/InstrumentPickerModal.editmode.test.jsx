// @vitest-environment jsdom
//
// W3a — edit-mode / pre-fill / readOnly contract on the shared
// InstrumentPickerModal.  The three consuming surfaces (Signals, Indicators,
// Portfolio) all round-trip a saved config through THIS modal, so the
// pre-fill + readOnly behaviour verified here is the frozen contract they
// build against.
//
// The critical footgun (Sign 7): the modal is `isOpen`-gated but stays
// MOUNTED at its unconditional call sites, so `useState` initializers run
// only once.  Pre-fill MUST be an `isOpen`-keyed EFFECT — the reopen test
// below fails hard if pre-fill is a `useState` initializer.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import InstrumentPickerModal from './InstrumentPickerModal';
import { buildDefaultOptionStream } from '../OptionStreamForm';

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
import { listBaskets } from '../../api/persistence';

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
  { collection: 'OPT_VIX', root_label: 'VIX', name: 'VIX', has_greeks: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_ES']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
  vi.mocked(getAvailableCycles).mockResolvedValue(['H', 'M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
  vi.mocked(listBaskets).mockResolvedValue([]);
});

// A fully-valid saved future config (a prior `onSelect` continuous emit).
const FUTURE_CONFIG = {
  type: 'continuous',
  collection: 'FUT_ES',
  adjustment: 'difference',
  cycle: 'H',
  rollOffset: 7,
  strategy: 'end_of_month',
};

// A fully-valid saved option config: a real default (guarantees validity)
// with option_type flipped to 'P' so the pre-fill is DISTINCT from a fresh
// default (which is 'C').
function optionConfig() {
  return { ...buildDefaultOptionStream({ availableRoots: MOCK_ROOTS }), option_type: 'P' };
}

describe('<InstrumentPickerModal> edit mode + readOnly', () => {
  // ── 1. Edit mode pre-fills a FUTURE (continuous) config ──
  it('pre-fills a continuous config: opens straight into the futures drill-down with every field seeded', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        initialConfig={FUTURE_CONFIG}
      />,
    );

    // The modal opens directly on the terminal config step — no category list.
    const picker = await screen.findByTestId('continuous-spec-picker');
    expect(picker).toBeTruthy();
    // Header shows the pre-selected collection.
    expect(screen.getByRole('heading', { name: 'FUT_ES' })).toBeTruthy();

    // Synchronously-seeded fields.
    expect(screen.getByTestId('continuous-spec-picker-strategy').value).toBe('end_of_month');
    expect(screen.getByTestId('continuous-spec-picker-adjustment').value).toBe('difference');
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').value).toBe('7');

    // Cycle resolves once getAvailableCycles(FUT_ES) loads the option list.
    await waitFor(() =>
      expect(getAvailableCycles).toHaveBeenCalledWith('FUT_ES'),
    );
    await waitFor(() =>
      expect(screen.getByTestId('continuous-spec-picker-cycle').value).toBe('H'),
    );
  });

  // ── 2. Edit mode pre-fills an OPTION config ──
  it('pre-fills an option_stream config: opens straight into the options drill-down with fields seeded, and round-trips on Confirm', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={onClose}
        onSelect={onSelect}
        initialConfig={optionConfig()}
      />,
    );

    const form = await screen.findByTestId('option-stream-form');
    expect(form).toBeTruthy();
    // Header shows the Options drill-down.
    expect(screen.getByRole('heading', { name: 'Options' })).toBeTruthy();
    // Root pre-selected + the DISTINCT-from-default option_type='P' seeded.
    expect(screen.getByLabelText('Root').value).toBe('OPT_SP_500');
    expect(screen.getByLabelText('Put').checked).toBe(true);
    expect(screen.getByLabelText('Call').checked).toBe(false);

    // Confirm emits the SAME shape the caller passed in (symmetric round-trip).
    const confirm = screen.getByTestId('option-stream-confirm');
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('option_stream');
    expect(emitted.collection).toBe('OPT_SP_500');
    expect(emitted.option_type).toBe('P');
    expect(onClose).toHaveBeenCalledOnce();
  });

  // ── 3. Reset-on-reopen footgun ──
  // Open with configA → close → REOPEN with configB on the SAME instance
  // (rerender, never unmounted — mirrors the real call sites). Must show B,
  // not A. This FAILS if pre-fill is a `useState` initializer.
  it('reopening with a different config shows the new config, not the stale first one (Sign 7 footgun)', async () => {
    const configA = { ...FUTURE_CONFIG, adjustment: 'ratio', rollOffset: 3, strategy: 'front_month' };
    const configB = { ...FUTURE_CONFIG, adjustment: 'difference', rollOffset: 9, strategy: 'end_of_month' };

    const { rerender } = render(
      <InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} initialConfig={configA} />,
    );
    await screen.findByTestId('continuous-spec-picker');
    expect(screen.getByTestId('continuous-spec-picker-adjustment').value).toBe('ratio');
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').value).toBe('3');
    expect(screen.getByTestId('continuous-spec-picker-strategy').value).toBe('front_month');

    // Close (component STAYS mounted — returns null but hooks/state persist).
    rerender(
      <InstrumentPickerModal isOpen={false} onClose={vi.fn()} onSelect={vi.fn()} initialConfig={configA} />,
    );
    // Reopen with configB.
    rerender(
      <InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} initialConfig={configB} />,
    );

    await screen.findByTestId('continuous-spec-picker');
    expect(screen.getByTestId('continuous-spec-picker-adjustment').value).toBe('difference');
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').value).toBe('9');
    expect(screen.getByTestId('continuous-spec-picker-strategy').value).toBe('end_of_month');
  });

  // ── 4a. readOnly renders view-only — option path ──
  it('readOnly (option): every inner input disabled and Confirm hidden', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        initialConfig={optionConfig()}
        readOnly={true}
      />,
    );
    const form = await screen.findByTestId('option-stream-form');
    expect(form).toBeTruthy();
    // The form is disabled end-to-end (OptionStreamForm's `disabled` path).
    expect(form.getAttribute('aria-disabled')).toBe('true');
    expect(screen.getByLabelText('Root').disabled).toBe(true);
    expect(screen.getByLabelText('Put').disabled).toBe(true);
    // Confirm is hidden — no mutation possible.
    expect(screen.queryByTestId('option-stream-confirm')).toBeNull();
  });

  // ── 4b. readOnly renders view-only — continuous path ──
  it('readOnly (continuous): every ContinuousSpecPicker control disabled and Select hidden', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        initialConfig={FUTURE_CONFIG}
        readOnly={true}
      />,
    );
    await screen.findByTestId('continuous-spec-picker');
    expect(screen.getByTestId('continuous-spec-picker-strategy').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-adjustment').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-cycle').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').disabled).toBe(true);
    // The "Select Continuous Series" CTA is hidden — no mutation possible.
    expect(screen.queryByText('Select Continuous Series')).toBeNull();
  });

  // ── 4c. readOnly is truly view-only: it NEVER emits, even on re-pick ──
  // Back stays enabled (navigate freely), but no selection may commit.
  it('readOnly never emits: navigating back to the category list and clicking a spot does not call onSelect', async () => {
    const onSelect = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={onSelect}
        initialConfig={FUTURE_CONFIG}
        readOnly={true}
      />,
    );
    await screen.findByTestId('continuous-spec-picker');
    // Back is ENABLED in readOnly — navigate back to the category list.
    fireEvent.click(screen.getByText('←'));
    await waitFor(() => expect(screen.getByText('Indexes')).toBeTruthy());
    // Re-pick a spot instrument — the immediate-emit path.
    fireEvent.click(screen.getByText('Indexes'));
    fireEvent.click(await screen.findByText('SPX'));
    // View-only: the modal committed nothing.
    expect(onSelect).not.toHaveBeenCalled();
  });

  // ── 5. Create mode (no initialConfig) is UNCHANGED ──
  it('create mode (no initialConfig) opens on the category list — no auto drill-down — and still builds a DEFAULT option stream', async () => {
    const onSelect = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={onSelect} />);

    // Category list is shown; no drill-down auto-entered.
    await waitFor(() => expect(screen.getByText('Futures')).toBeTruthy());
    expect(screen.getByText('Options')).toBeTruthy();
    expect(screen.queryByTestId('continuous-spec-picker')).toBeNull();
    expect(screen.queryByTestId('option-stream-form')).toBeNull();

    // Entering the Options drill-down still yields a FRESH default (not a
    // pre-fill): buildDefaultOptionStream applies — collection is the first
    // root and option_type is the default 'C'.
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await screen.findByTestId('option-stream-form');
    expect(screen.getByLabelText('Root').value).toBe('OPT_SP_500');
    expect(screen.getByLabelText('Call').checked).toBe(true);

    fireEvent.click(screen.getByTestId('option-stream-confirm'));
    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('option_stream');
    expect(emitted.option_type).toBe('C');
    expect(emitted.roll_offset).toEqual({ value: 0, unit: 'days' });
  });
});
