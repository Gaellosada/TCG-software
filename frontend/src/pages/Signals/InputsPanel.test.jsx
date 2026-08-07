// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';

afterEach(() => { cleanup(); });

import InputsPanel from './InputsPanel';
import { normaliseSpecForRequest } from './requestBuilder';

// InstrumentPickerModal (child) pulls from /api/data/*; stub the network
// layer so its useEffect doesn't blow up in jsdom.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX', 'FUT_ES']),
  listInstruments: vi.fn(async () => ({
    items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 0,
  })),
  getAvailableCycles: vi.fn(async () => ['HMUZ']),
}));

function renderPanel(initialInputs = []) {
  const onChange = vi.fn();
  const utils = render(<InputsPanel inputs={initialInputs} onChange={onChange} />);
  return { ...utils, onChange };
}

describe('<InputsPanel>', () => {
  it('auto-expands when the inputs array is empty', () => {
    renderPanel([]);
    // Body is rendered (has the "No inputs yet" empty-state copy and the add-btn).
    expect(screen.getByTestId('inputs-add-btn')).toBeDefined();
    expect(screen.getByText(/No inputs yet/i)).toBeDefined();
    expect(screen.getByTestId('inputs-panel-toggle').getAttribute('aria-expanded'))
      .toBe('true');
  });

  it('header toggle collapses and re-expands the body', () => {
    renderPanel([]);
    const toggle = screen.getByTestId('inputs-panel-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    // Collapse.
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('inputs-add-btn')).toBeNull();

    // Re-expand.
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('inputs-add-btn')).toBeDefined();
  });

  it('Add input appends a row with the next free single-letter id (X then Y)', () => {
    // First click from empty list → id "X".
    const { onChange, rerender } = renderPanel([]);
    fireEvent.click(screen.getByTestId('inputs-add-btn'));
    expect(onChange).toHaveBeenCalledTimes(1);
    const firstPayload = onChange.mock.calls[0][0];
    expect(firstPayload).toHaveLength(1);
    expect(firstPayload[0].id).toBe('X');
    // Default unset spot instrument — user must pick.
    expect(firstPayload[0].instrument).toEqual({
      type: 'spot', collection: '', instrument_id: '',
    });

    // Simulate the parent applying the state and rerendering, then click
    // Add again — next letter in the alphabet is "Y".
    rerender(<InputsPanel inputs={firstPayload} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('inputs-add-btn'));
    const secondPayload = onChange.mock.calls[1][0];
    expect(secondPayload).toHaveLength(2);
    expect(secondPayload[1].id).toBe('Y');
  });

  it('delete opens ConfirmDialog; Cancel leaves the row; Confirm removes it', () => {
    const seeded = [
      { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    ];
    const { onChange, rerender } = renderPanel(seeded);
    // Non-empty list → panel starts collapsed; expand it to expose the row.
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));

    // Open the confirm dialog.
    act(() => { fireEvent.click(screen.getByTestId('input-delete-0')); });
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();

    // Cancel — onChange NOT called, row still present.
    act(() => { fireEvent.click(screen.getByTestId('confirm-dialog-cancel')); });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('input-row-0')).toBeDefined();

    // Reopen and confirm — onChange called with the row removed.
    act(() => { fireEvent.click(screen.getByTestId('input-delete-0')); });
    act(() => { fireEvent.click(screen.getByTestId('confirm-dialog-confirm')); });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual([]);

    // Apply the delete by rerendering with the new value; dialog should close.
    rerender(<InputsPanel inputs={[]} onChange={onChange} />);
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  it('duplicate id entered via the renamer is silently rejected', () => {
    const seeded = [
      { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
      { id: 'Y', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    ];
    const { onChange } = renderPanel(seeded);
    // Non-empty list → panel starts collapsed; expand it.
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));

    // Try to rename row 1 (id="Y") to "X" — a duplicate of row 0.
    const idInput = screen.getByTestId('input-id-1');
    fireEvent.change(idInput, { target: { value: 'X' } });

    // Component silently refuses — onChange should NOT be called with a
    // list containing two "X" entries.
    if (onChange.mock.calls.length > 0) {
      for (const call of onChange.mock.calls) {
        const ids = call[0].map((x) => x.id);
        const unique = new Set(ids);
        expect(unique.size).toBe(ids.length);
      }
    }
    // Non-duplicate rename still works.
    fireEvent.change(idInput, { target: { value: 'Z' } });
    expect(onChange).toHaveBeenCalled();
    const lastPayload = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(lastPayload[1].id).toBe('Z');
  });

  it('count chip reads "N" when all inputs configured, "K/N" otherwise', () => {
    const inputs = [
      { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
      { id: 'Y', instrument: { type: 'spot', collection: '', instrument_id: '' } },
    ];
    renderPanel(inputs);
    // 1 of 2 configured.
    const toggle = screen.getByTestId('inputs-panel-toggle');
    expect(toggle.textContent).toMatch(/1\/2/);
  });

  it('picker button labels a configured inline basket input as "Basket: leg1, leg2"', () => {
    // Regression for the UX bug where a configured basket input still
    // rendered "Select instrument" because instrumentLabel() returned
    // null for type === "basket".
    const inputs = [
      {
        id: 'X',
        instrument: {
          type: 'basket',
          kind: 'inline',
          asset_class: 'equity',
          legs: [
            { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' }, weight: 0.6 },
            { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'QQQ' }, weight: 0.4 },
          ],
        },
      },
    ];
    renderPanel(inputs);
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    const pickerBtn = screen.getByTestId('input-picker-0');
    expect(pickerBtn.textContent).toBe('Basket: SPY, QQQ');
    expect(pickerBtn.textContent).not.toMatch(/Select instrument/);
  });

  it('picker button labels a configured option-basket input with collection·option_type per leg', () => {
    // Bug 1 (iter-4) was about per-leg radio independence on the
    // composer; this pin ensures the InputsPanel label likewise shows
    // the option_types so the user can verify the basket at a glance.
    const inputs = [
      {
        id: 'X',
        instrument: {
          type: 'basket',
          kind: 'inline',
          asset_class: 'option',
          legs: [
            { instrument: { type: 'option_stream', collection: 'OPT_ES', option_type: 'C' }, weight: 1.0 },
            { instrument: { type: 'option_stream', collection: 'OPT_ES', option_type: 'P' }, weight: 1.0 },
          ],
        },
      },
    ];
    renderPanel(inputs);
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    expect(screen.getByTestId('input-picker-0').textContent).toBe('Basket: OPT_ES·C, OPT_ES·P');
  });

  it('picker button labels a saved-basket input as "Basket: <basket_id>"', () => {
    const inputs = [
      {
        id: 'X',
        instrument: { type: 'basket', kind: 'saved', basket_id: 'BSK_TECH_2026' },
      },
    ];
    renderPanel(inputs);
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    expect(screen.getByTestId('input-picker-0').textContent).toBe('Basket: BSK_TECH_2026');
  });

  it('falls back to "Select instrument" for an unconfigured (0-leg) inline basket', () => {
    const inputs = [
      {
        id: 'X',
        instrument: { type: 'basket', kind: 'inline', asset_class: 'equity', legs: [] },
      },
    ];
    renderPanel(inputs);
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    expect(screen.getByTestId('input-picker-0').textContent).toMatch(/Select instrument/);
  });
});

// R1 fix: expose the per-input net-position cap so a non-dev can build the
// (A AND B) OR C legs from the UI without the OR-branches doubling the
// position. State holds the wire-shape FRACTION pair; the UI edits percent.
describe('<InputsPanel> — per-input net-position cap (OR-branch clamp)', () => {
  const SPX = { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' };
  const seed = (extra = {}) => [{ id: 'X', instrument: SPX, ...extra }];
  const lastCall = (m) => m.mock.calls[m.mock.calls.length - 1];

  function renderExpanded(inputs) {
    const res = renderPanel(inputs);
    // A non-empty list starts collapsed — expand to expose the rows.
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    return res;
  }

  it('an input WITHOUT a cap shows the "+ Net cap" toggle and no cap fields', () => {
    renderExpanded(seed());
    expect(screen.getByTestId('input-cap-toggle-0')).toBeDefined();
    expect(screen.queryByTestId('input-cap-low-0')).toBeNull();
  });

  it('clicking "+ Net cap" enables the long-or-flat default [0, 1]', () => {
    const { onChange } = renderExpanded(seed());
    fireEvent.click(screen.getByTestId('input-cap-toggle-0'));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0][0].position_cap).toEqual([0, 1]);
  });

  it('renders a stored fraction cap as percent (1.0 → "100") and hides the toggle', () => {
    renderExpanded(seed({ position_cap: [0, 1] }));
    expect(screen.getByTestId('input-cap-low-0').value).toBe('0');
    expect(screen.getByTestId('input-cap-high-0').value).toBe('100');
    expect(screen.queryByTestId('input-cap-toggle-0')).toBeNull();
  });

  it('editing the high field (percent) writes back a fraction: 50 → 0.5', () => {
    const { onChange } = renderExpanded(seed({ position_cap: [0, 1] }));
    fireEvent.change(screen.getByTestId('input-cap-high-0'), { target: { value: '50' } });
    expect(lastCall(onChange)[0][0].position_cap).toEqual([0, 0.5]);
  });

  it('editing the low field to a negative percent → negative fraction: -100 → -1', () => {
    const { onChange } = renderExpanded(seed({ position_cap: [0, 1] }));
    fireEvent.change(screen.getByTestId('input-cap-low-0'), { target: { value: '-100' } });
    expect(lastCall(onChange)[0][0].position_cap).toEqual([-1, 1]);
  });

  it('a fractional percent round-trips through display without binary-float noise', () => {
    // 10% == fraction 0.1; 0.1 * 100 === 10.000000000000002 in JS, so the
    // display formatter must round it back to "10".
    renderExpanded(seed({ position_cap: [0, 0.1] }));
    expect(screen.getByTestId('input-cap-high-0').value).toBe('10');
  });

  it('a partial draft ("" then "-") never commits a NaN bound to state', () => {
    const { onChange } = renderExpanded(seed({ position_cap: [0, 1] }));
    fireEvent.change(screen.getByTestId('input-cap-low-0'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('input-cap-low-0'), { target: { value: '-' } });
    for (const call of onChange.mock.calls) {
      const cap = call[0][0].position_cap;
      if (Array.isArray(cap)) {
        expect(Number.isNaN(cap[0])).toBe(false);
        expect(Number.isNaN(cap[1])).toBe(false);
      }
    }
    // The field still shows the user's raw draft.
    expect(screen.getByTestId('input-cap-low-0').value).toBe('-');
  });

  it('low > high marks both fields invalid (aria-invalid) as a wire-drop warning', () => {
    renderExpanded(seed({ position_cap: [1, 0] }));
    expect(screen.getByTestId('input-cap-low-0').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByTestId('input-cap-high-0').getAttribute('aria-invalid')).toBe('true');
  });

  it('clear cap removes position_cap entirely (byte-identical no-cap input)', () => {
    const { onChange } = renderExpanded(seed({ position_cap: [0, 1] }));
    fireEvent.click(screen.getByTestId('input-cap-clear-0'));
    const out = lastCall(onChange)[0][0];
    expect('position_cap' in out).toBe(false);
    expect(out).toEqual({ id: 'X', instrument: SPX });
  });

  it('an edited cap round-trips through the real request builder as a fraction; a no-cap input emits no key', () => {
    const capped = normaliseSpecForRequest({ id: 's', name: 'n', inputs: seed({ position_cap: [0, 0.5] }) });
    expect(capped.inputs[0].position_cap).toEqual([0, 0.5]);
    const bare = normaliseSpecForRequest({ id: 's', name: 'n', inputs: seed() });
    expect('position_cap' in bare.inputs[0]).toBe(false);
  });

  it('locked (readOnly) hides the add-cap toggle but shows a set cap read-only', () => {
    const onChange = vi.fn();
    const { rerender } = render(<InputsPanel inputs={seed()} onChange={onChange} readOnly />);
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    expect(screen.queryByTestId('input-cap-toggle-0')).toBeNull();

    rerender(<InputsPanel inputs={seed({ position_cap: [0, 1] })} onChange={onChange} readOnly />);
    const low = screen.getByTestId('input-cap-low-0');
    expect(low.hasAttribute('readonly')).toBe(true);
    expect(screen.queryByTestId('input-cap-clear-0')).toBeNull();
  });
});
