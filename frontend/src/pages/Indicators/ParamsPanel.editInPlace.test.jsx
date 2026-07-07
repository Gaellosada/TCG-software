// @vitest-environment jsdom
//
// W3b (Indicators) — click-chip-to-edit against the frozen W3a contract
// (InstrumentPickerModal `initialConfig` + `readOnly`). See
// workspace/tasks/editable-instrument-inputs/output/DESIGN-iter1.md
// ("Per-surface wiring → Indicators", "Named decision D1") and
// W3a-modals-iter1.md ("Notes for surface workers").
//
// Correctness crux: confirming an edit routes through `onSeriesSave(label,
// entry)` (the existing setter) so IndicatorsPage.handleSeriesSave merges it
// in place (`{...seriesMap, [label]: entry}`) — the edited slot's ref is
// replaced, every OTHER slot is preserved, and no duplicate series is added.
// The `Harness` below mirrors handleSeriesSave's keyed-merge so this property
// is exercised at the ParamsPanel boundary.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { buildDefaultOptionStream } from '../../components/OptionStreamForm';
import ParamsPanel from './ParamsPanel';

afterEach(() => { cleanup(); });

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

const SPOT_CONFIG = { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' };

function optionConfig() {
  // Distinct from a fresh default (option_type 'C') so pre-fill is provable.
  return { ...buildDefaultOptionStream({ availableRoots: MOCK_ROOTS }), option_type: 'P' };
}

// Stateful wrapper that owns seriesMap and merges edits exactly the way
// IndicatorsPage.handleSeriesSave does (`{...prev, [label]: entry}`), so a
// confirm proves update-in-place + other-slots-preserved + no-duplicate.
function renderPanel({ seriesMap, seriesLabels, readOnly = false }) {
  const onSeriesSaveSpy = vi.fn();
  function Harness() {
    const [map, setMap] = useState(seriesMap);
    return (
      <ParamsPanel
        indicator={{
          id: 'u1',
          name: 'My ind',
          code: "def compute(series):\n    return series['close']",
          params: {},
          seriesMap: map,
          readonly: false,
        }}
        paramsSpec={[]}
        seriesLabels={seriesLabels}
        onParamChange={vi.fn()}
        onSeriesSave={(label, entry) => {
          onSeriesSaveSpy(label, entry);
          setMap((prev) => ({ ...prev, [label]: entry }));
        }}
        onRun={vi.fn()}
        running={false}
        canRun={false}
        runDisabledReason={null}
        defaultCollection={null}
        ownPanel={false}
        onOwnPanelChange={vi.fn()}
        showDateRange={false}
        optionDateRange={null}
        onOptionDateRangeChange={vi.fn()}
        readOnly={readOnly}
      />
    );
  }
  const utils = render(<Harness />);
  return { ...utils, onSeriesSaveSpy };
}

describe('<ParamsPanel> click-chip-to-edit', () => {
  // ── (a) future chip: opens pre-filled with the effective ref, one field
  //        changed, updates that slot in place, other slot preserved ──
  it('editing a future chip updates that slot in place (effective-ref prefill, other slot preserved, no duplicate)', async () => {
    const { onSeriesSaveSpy } = renderPanel({
      seriesMap: { close: FUTURE_CONFIG, volume: SPOT_CONFIG },
      seriesLabels: ['close', 'volume'],
    });

    fireEvent.click(screen.getByTestId('instrument-picker-close'));
    await screen.findByTestId('continuous-spec-picker');
    // Pre-filled from the effective (stored) ref driving the series.
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').value).toBe('2');

    // Change ONE field.
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByText('Select Continuous Series'));

    // Routed through onSeriesSave once, for the SAME label (no new key).
    expect(onSeriesSaveSpy).toHaveBeenCalledTimes(1);
    expect(onSeriesSaveSpy).toHaveBeenCalledWith(
      'close',
      expect.objectContaining({ type: 'continuous', collection: 'FUT_ES', rollOffset: 5, strategy: 'front_month' }),
    );
    // Other slot preserved (still exactly one 'volume' chip; no duplicate).
    expect(screen.getByText('INDEX / SPX')).toBeTruthy();
  });

  // ── (b) option chip: opens pre-filled, one field changed, updates in place ──
  it('editing an option chip updates that slot in place (id/other fields preserved, no duplicate)', async () => {
    const { onSeriesSaveSpy } = renderPanel({
      seriesMap: { atm_iv: optionConfig(), volume: SPOT_CONFIG },
      seriesLabels: ['atm_iv', 'volume'],
    });

    fireEvent.click(screen.getByTestId('instrument-picker-atm_iv'));
    await screen.findByTestId('option-stream-form');
    // Pre-filled Put (seeded distinct from the 'C' default).
    expect(screen.getByLabelText('Put').checked).toBe(true);

    // Change ONE field: flip to Call.
    fireEvent.click(screen.getByLabelText('Call'));
    fireEvent.click(screen.getByTestId('option-stream-confirm'));

    expect(onSeriesSaveSpy).toHaveBeenCalledTimes(1);
    const [label, entry] = onSeriesSaveSpy.mock.calls[0];
    expect(label).toBe('atm_iv');
    expect(entry.option_type).toBe('C');
    expect(entry.collection).toBe('OPT_SP_500'); // other fields preserved
    // Other slot preserved.
    expect(screen.getByText('INDEX / SPX')).toBeTruthy();
  });

  // ── (c) the ✎ pencil is GONE; the chip itself is the edit trigger (D1) ──
  it('removes the ✎ pencil — the chip itself opens the picker', async () => {
    renderPanel({ seriesMap: { close: FUTURE_CONFIG }, seriesLabels: ['close'] });

    // Pencil affordance removed entirely.
    expect(screen.queryByTitle('Change instrument')).toBeNull();
    expect(screen.queryByText('✎')).toBeNull();

    // The chip carries the picker trigger + an explicit "Edit settings" title.
    const chip = screen.getByTestId('instrument-picker-close');
    expect(chip.tagName).toBe('BUTTON');
    expect(chip.title).toBe('Edit settings');

    fireEvent.click(chip);
    expect(await screen.findByTestId('continuous-spec-picker')).toBeTruthy();
  });

  // ── (d) cancel = no mutation ──
  it('closing the modal without confirming leaves the slot untouched', async () => {
    const { onSeriesSaveSpy } = renderPanel({
      seriesMap: { close: FUTURE_CONFIG },
      seriesLabels: ['close'],
    });

    fireEvent.click(screen.getByTestId('instrument-picker-close'));
    await screen.findByTestId('continuous-spec-picker');
    fireEvent.change(screen.getByTestId('continuous-spec-picker-roll-offset'), {
      target: { value: '9' },
    });
    // Cancel via the modal's close (×) button, not Confirm.
    fireEvent.click(screen.getByLabelText('Close'));

    expect(onSeriesSaveSpy).not.toHaveBeenCalled();
  });

  // ── (e) a LOCKED indicator opens the picker READ-ONLY (view, not edit) ──
  it('a locked indicator opens the picker read-only: viewable but every control disabled and no emit path', async () => {
    const { onSeriesSaveSpy } = renderPanel({
      seriesMap: { close: FUTURE_CONFIG },
      seriesLabels: ['close'],
      readOnly: true,
    });

    const chip = screen.getByTestId('instrument-picker-close');
    // View-only still means clickable (inspectable), not inert.
    expect(chip.disabled).toBe(false);
    expect(chip.title).toBe('View settings');
    fireEvent.click(chip);

    await screen.findByTestId('continuous-spec-picker');
    expect(screen.getByTestId('continuous-spec-picker-strategy').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-adjustment').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-cycle').disabled).toBe(true);
    expect(screen.getByTestId('continuous-spec-picker-roll-offset').disabled).toBe(true);
    // No confirm CTA — view-only, nothing can be committed.
    expect(screen.queryByText('Select Continuous Series')).toBeNull();
    expect(onSeriesSaveSpy).not.toHaveBeenCalled();
  });

  // ── (f) spot/index chips: NOT click-to-edit (no config popup — DESIGN A2) ──
  it('a spot chip has no click-to-edit affordance (plain span, no trigger)', async () => {
    renderPanel({ seriesMap: { close: SPOT_CONFIG }, seriesLabels: ['close'] });

    // No picker trigger on a spot chip.
    expect(screen.queryByTestId('instrument-picker-close')).toBeNull();
    const chip = screen.getByText('INDEX / SPX');
    expect(chip.tagName).toBe('SPAN');

    fireEvent.click(chip);
    // Give any (incorrect) async open a chance to happen, then assert absence.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  // ── (g) an UNPICKED slot still opens create mode (no regression) ──
  it('an unpicked slot still opens the picker in create mode (no regression)', async () => {
    renderPanel({ seriesMap: {}, seriesLabels: ['close'] });

    fireEvent.click(screen.getByTestId('instrument-picker-close'));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy());
    // Create mode: category list, no auto drill-down.
    expect(screen.queryByTestId('continuous-spec-picker')).toBeNull();
    expect(screen.queryByTestId('option-stream-form')).toBeNull();
  });
});
