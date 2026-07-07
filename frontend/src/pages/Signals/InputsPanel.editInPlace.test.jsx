// @vitest-environment jsdom
//
// W3b (Signals) — click-chip-to-edit against the frozen W3a contract
// (InstrumentPickerModal `initialConfig` + `readOnly`). See
// workspace/tasks/editable-instrument-inputs/output/DESIGN-iter1.md
// ("Per-surface wiring → Signals") and W3a-modals-iter1.md ("Notes for
// surface workers").
//
// Correctness crux: confirming an edit must UPDATE the input IN PLACE at
// its index (id preserved, no new input appended) — never append.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { buildDefaultOptionStream } from '../../components/OptionStreamForm';

afterEach(() => { cleanup(); });

import InputsPanel from './InputsPanel';

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX', 'FUT_ES']),
  listInstruments: vi.fn(async () => ({
    items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 0,
  })),
  getAvailableCycles: vi.fn(async () => ['H']),
}));

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
];

vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(async () => ({ roots: MOCK_ROOTS })),
}));

vi.mock('../../api/persistence', () => ({
  createBasket: vi.fn(),
  listBaskets: vi.fn(async () => []),
}));

const FUTURE_CONFIG = {
  type: 'continuous',
  collection: 'FUT_ES',
  adjustment: 'none',
  cycle: null,
  rollOffset: 2,
  strategy: 'front_month',
};

function optionConfig() {
  // Distinct from a fresh default (option_type 'C') so pre-fill is provable.
  return { ...buildDefaultOptionStream({ availableRoots: MOCK_ROOTS }), option_type: 'P' };
}

function renderPanel(inputs, extraProps = {}) {
  const onChange = vi.fn();
  const utils = render(<InputsPanel inputs={inputs} onChange={onChange} {...extraProps} />);
  // Non-empty list starts collapsed — expand it.
  fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
  return { ...utils, onChange };
}

describe('<InputsPanel> click-chip-to-edit', () => {
  // ── (a) future chip: opens pre-filled, one field changed, updates in place ──
  it('editing a future chip updates that input in place (id preserved, no new input added)', async () => {
    const seeded = [{ id: 'X', instrument: FUTURE_CONFIG }];
    const { onChange } = renderPanel(seeded);

    fireEvent.click(screen.getByTestId('input-picker-0'));
    const picker = await screen.findByTestId('continuous-spec-picker');
    expect(picker).toBeTruthy();
    // Pre-filled from the stored config.
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').value).toBe('2');

    // Change ONE field.
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByText('Select Continuous Series'));

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(next).toHaveLength(1); // no new input appended
    expect(next[0].id).toBe('X'); // identity preserved
    expect(next[0].instrument).toEqual({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'none',
      cycle: null,
      rollOffset: 5,
    });
  });

  // ── (b) option chip: opens pre-filled, one field changed, updates in place ──
  it('editing an option chip updates that input in place (id preserved, no new input added)', async () => {
    const seeded = [{ id: 'Y', instrument: optionConfig() }];
    const { onChange } = renderPanel(seeded);

    fireEvent.click(screen.getByTestId('input-picker-0'));
    await screen.findByTestId('option-stream-form');
    // Pre-filled Put (seeded distinct from the 'C' default).
    expect(screen.getByLabelText('Put').checked).toBe(true);

    // Change ONE field: flip to Call.
    fireEvent.click(screen.getByLabelText('Call'));
    fireEvent.click(screen.getByTestId('option-stream-confirm'));

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(next).toHaveLength(1); // no new input appended
    expect(next[0].id).toBe('Y'); // identity preserved
    expect(next[0].instrument.option_type).toBe('C');
    expect(next[0].instrument.collection).toBe('OPT_SP_500'); // other fields preserved
  });

  // ── (c) cancel = no mutation ──
  it('closing the modal without confirming leaves the input untouched', async () => {
    const seeded = [{ id: 'X', instrument: FUTURE_CONFIG }];
    const { onChange } = renderPanel(seeded);

    fireEvent.click(screen.getByTestId('input-picker-0'));
    await screen.findByTestId('continuous-spec-picker');
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), {
      target: { value: '9' },
    });
    // Cancel via the modal's close (×) button, not Confirm.
    fireEvent.click(screen.getByLabelText('Close'));

    expect(onChange).not.toHaveBeenCalled();
  });

  // ── (d) locked signal: chip opens the picker READ-ONLY (view, not edit) ──
  it('a locked signal opens the picker read-only: viewable but every control disabled and no emit path', async () => {
    const seeded = [{ id: 'X', instrument: FUTURE_CONFIG }];
    const { onChange } = renderPanel(seeded, { readOnly: true });

    const pickBtn = screen.getByTestId('input-picker-0');
    // Lock-respect still means "view", not "disabled outright": the button
    // itself stays clickable so a locked signal's settings are inspectable.
    expect(pickBtn.disabled).toBe(false);
    fireEvent.click(pickBtn);

    await screen.findByTestId('continuous-spec-picker');
    expect(screen.getByTestId('continuous-spec-picker-strategy').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-adjustment').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-cycle').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').disabled).toBe(true);
    // No confirm CTA — view-only, nothing can be committed.
    expect(screen.queryByText('Select Continuous Series')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  // ── (e) spot/index chips: NOT click-to-edit (no config popup — DESIGN A2) ──
  it('a configured spot chip has no click-to-edit affordance', async () => {
    const seeded = [
      { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    ];
    renderPanel(seeded);

    fireEvent.click(screen.getByTestId('input-picker-0'));
    // Give any (incorrect) async open a chance to happen, then assert absence.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  // ── an UNCONFIGURED input (fresh spot placeholder) still opens create mode ──
  it('an unconfigured input still opens the picker in create mode (no regression)', async () => {
    const seeded = [{ id: 'X', instrument: { type: 'spot', collection: '', instrument_id: '' } }];
    renderPanel(seeded);

    fireEvent.click(screen.getByTestId('input-picker-0'));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy());
    // Create mode: category list, no auto drill-down.
    expect(screen.queryByTestId('continuous-spec-picker')).toBeNull();
    expect(screen.queryByTestId('option-stream-form')).toBeNull();
  });
});
