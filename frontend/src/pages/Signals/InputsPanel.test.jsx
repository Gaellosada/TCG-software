// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';

afterEach(() => { cleanup(); });

import InputsPanel from './InputsPanel';

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
});
