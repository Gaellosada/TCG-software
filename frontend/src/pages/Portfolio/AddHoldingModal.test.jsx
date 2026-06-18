// @vitest-environment jsdom
//
// AddHoldingModal maps an InstrumentPickerModal selection into the portfolio
// leg shape. These tests pin the mapping for each instrument type, focusing
// on the option_stream roll fields (adjustment + roll_offset) being carried
// through to onAddLeg — the picker emits them via OptionStreamForm, and the
// leg must preserve them so they reach the backend (mirrors the continuous
// leg's adjustment/rollOffset carry-through).

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import AddHoldingModal from './AddHoldingModal';

afterEach(cleanup);

// Mock the picker so a click invokes its onSelect with a chosen instrument.
// The captured `onSelect` is the contract AddHoldingModal wires up.
let capturedOnSelect = null;
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  // eslint-disable-next-line react/prop-types
  default: ({ onSelect }) => {
    capturedOnSelect = onSelect;
    return <div data-testid="picker-stub" />;
  },
}));

function renderModal() {
  const onAddLeg = vi.fn();
  const onClose = vi.fn();
  render(<AddHoldingModal isOpen onClose={onClose} onAddLeg={onAddLeg} />);
  return { onAddLeg, onClose };
}

describe('AddHoldingModal — option_stream leg mapping', () => {
  it('carries adjustment + roll_offset from the picked instrument to the leg', () => {
    const { onAddLeg, onClose } = renderModal();
    fireEvent.click(screen.getByTestId('picker-stub')); // ensure mounted
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'mid',
      adjustment: 'ratio',
      roll_offset: 7,
    });
    expect(onAddLeg).toHaveBeenCalledTimes(1);
    expect(onAddLeg.mock.calls[0][0]).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      stream: 'mid',
      adjustment: 'ratio',
      roll_offset: 7,
      weight: 100,
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('passes through default roll fields (none / 0) unchanged', () => {
    const { onAddLeg } = renderModal();
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'fixed', date: '2025-06-20' },
      selection: { kind: 'by_delta', target: -0.25, tolerance: 0.05, strict: false },
      stream: 'iv',
      adjustment: 'none',
      roll_offset: 0,
    });
    const leg = onAddLeg.mock.calls[0][0];
    expect(leg.adjustment).toBe('none');
    expect(leg.roll_offset).toBe(0);
  });
});
