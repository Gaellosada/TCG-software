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

  it('forwards futures-notional sizing (sizing_mode + futures_reference) when the user opts in', () => {
    const { onAddLeg } = renderModal();
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
      stream: 'mid',
      roll_offset: 0,
      hold_between_rolls: true,
      nav_times: 1.0,
      sizing_mode: 'futures_notional',
      futures_reference: 'nearest_abs',
    });
    const leg = onAddLeg.mock.calls[0][0];
    expect(leg.sizing_mode).toBe('futures_notional');
    expect(leg.futures_reference).toBe('nearest_abs');
  });

  it('a premium-notional (default) leg carries NO sizing_mode / futures_reference keys (byte-identical)', () => {
    const { onAddLeg } = renderModal();
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'mid',
      roll_offset: 0,
      // No sizing_mode / futures_reference — the untouched default.
    });
    const leg = onAddLeg.mock.calls[0][0];
    expect('sizing_mode' in leg).toBe(false);
    expect('futures_reference' in leg).toBe(false);
  });

  it('seeds futures_reference default when sizing is futures but reference is absent', () => {
    const { onAddLeg } = renderModal();
    capturedOnSelect({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
      stream: 'mid',
      roll_offset: 0,
      sizing_mode: 'futures_notional',
      // futures_reference intentionally omitted.
    });
    const leg = onAddLeg.mock.calls[0][0];
    expect(leg.sizing_mode).toBe('futures_notional');
    expect(leg.futures_reference).toBe('nearest_on_or_after');
  });
});
