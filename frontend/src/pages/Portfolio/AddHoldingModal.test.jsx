// @vitest-environment jsdom
//
// AddHoldingModal maps an InstrumentPickerModal selection into the portfolio
// leg shape. These tests pin the mapping for each instrument type, focusing
// on the option_stream roll_offset being carried through to onAddLeg — the
// picker emits it via OptionStreamForm, and the leg must preserve it so it
// reaches the backend. Option streams carry NO back-adjustment, so the leg
// must NOT gain an `adjustment` field (unlike the continuous leg).

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import AddHoldingModal from './AddHoldingModal';

afterEach(cleanup);

// Mock the picker so a click invokes its onSelect with a chosen instrument.
// The captured `onSelect` is the contract AddHoldingModal wires up; the
// captured props let us assert the option-stream restriction is threaded.
let capturedOnSelect = null;
let capturedPickerProps = null;
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  // eslint-disable-next-line react/prop-types
  default: (props) => {
    capturedOnSelect = props.onSelect;
    capturedPickerProps = props;
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
  it('carries roll_offset to the leg and does NOT add an adjustment field', () => {
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
      // A stray `adjustment` from the picked instrument must be dropped.
      adjustment: 'ratio',
      roll_offset: 7,
    });
    expect(onAddLeg).toHaveBeenCalledTimes(1);
    const leg = onAddLeg.mock.calls[0][0];
    expect(leg).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      stream: 'mid',
      roll_offset: 7,
      weight: 100,
    });
    expect('adjustment' in leg).toBe(false);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('restricts the picker option leg to the mid (price) stream', () => {
    // Issue #2 (D1): a portfolio option leg is the option PRICE only. The modal
    // must tell the picker to pin the option stream to mid (hiding the Series
    // selector); iv/greeks/volume are signal-level operands, not portfolio legs.
    renderModal();
    expect(capturedPickerProps).not.toBeNull();
    expect(capturedPickerProps.optionStreamAllowedStreams).toEqual(['mid']);
  });

  it('passes through a default roll_offset (0) and never adds adjustment', () => {
    const { onAddLeg } = renderModal();
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'fixed', date: '2025-06-20' },
      selection: { kind: 'by_delta', target: -0.25, tolerance: 0.05, strict: false },
      stream: 'iv',
      roll_offset: 0,
    });
    const leg = onAddLeg.mock.calls[0][0];
    expect('adjustment' in leg).toBe(false);
    expect(leg.roll_offset).toBe(0);
  });
});
