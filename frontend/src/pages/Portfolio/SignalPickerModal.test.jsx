// @vitest-environment jsdom
//
// Tests for SignalPickerModal — two-step modal: pick signal, configure inputs.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import SignalPickerModal from './SignalPickerModal';

// Mock the Signals storage module so we control what signals are "saved".
vi.mock('../Signals/storage', () => ({
  loadState: vi.fn(() => ({ signals: [] })),
}));

// Mock InstrumentPickerModal to a simple stub — we test interaction, not its internals.
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  default: ({ isOpen, onSelect, onClose, title }) => {
    if (!isOpen) return null;
    return (
      <div data-testid="instrument-picker-stub">
        <span>{title}</span>
        <button
          data-testid="stub-pick-spot"
          onClick={() => onSelect({ type: 'spot', collection: 'EQ', instrument_id: 'AAPL' })}
        >
          Pick Spot
        </button>
        <button data-testid="stub-close" onClick={onClose}>Close</button>
      </div>
    );
  },
}));

import { loadState } from '../Signals/storage';

afterEach(() => { cleanup(); });

const noop = () => {};

function makeFakeSignal(overrides = {}) {
  return {
    id: 's1',
    name: 'Test Signal',
    inputs: [
      { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    ],
    rules: {
      entries: [
        {
          id: 'e1',
          input_id: 'X',
          weight: 50,
          conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }],
        },
      ],
      exits: [],
    },
    ...overrides,
  };
}

describe('<SignalPickerModal>', () => {
  beforeEach(() => {
    loadState.mockReturnValue({ signals: [] });
  });

  it('renders nothing when closed', () => {
    render(
      <SignalPickerModal isOpen={false} onClose={noop} onSelect={noop} />,
    );
    expect(screen.queryByTestId('signal-picker')).toBeNull();
  });

  it('renders empty state when no signals saved', () => {
    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );
    expect(screen.getByText(/No saved signals/)).toBeDefined();
  });

  it('renders saved signals with input and block counts', () => {
    const sig1 = makeFakeSignal({ id: 's1', name: 'Alpha Signal' });
    const sig2 = makeFakeSignal({
      id: 's2',
      name: 'Beta Signal',
      inputs: [
        { id: 'X', instrument: null },
        { id: 'Y', instrument: null },
      ],
      rules: {
        // 2 entries + 1 exit → 3 blocks total (test checks count only).
        entries: [
          { id: 'e1', input_id: 'X', weight: 25, conditions: [] },
          { id: 'e2', input_id: 'X', weight: -25, conditions: [] },
        ],
        exits: [
          { id: 'x1', input_id: 'Y', weight: 0, target_entry_block_id: 'e1', conditions: [] },
        ],
      },
    });
    loadState.mockReturnValue({ signals: [sig1, sig2] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    expect(screen.getByText('Alpha Signal')).toBeDefined();
    expect(screen.getByText('Beta Signal')).toBeDefined();
    expect(screen.getByText('1 input \u00B7 1 block')).toBeDefined();
    expect(screen.getByText('2 inputs \u00B7 3 blocks')).toBeDefined();
  });

  it('clicking Select transitions to step 2 (configure inputs)', () => {
    const signal = makeFakeSignal({ id: 's1', name: 'My Signal' });
    loadState.mockReturnValue({ signals: [signal] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Configure signal My Signal/ }));

    // Step 2 shows the signal name in header and input configuration
    expect(screen.getByText(/Configure: My Signal/)).toBeDefined();
    expect(screen.getByText('X')).toBeDefined(); // input id
    expect(screen.getByText(/SPX/)).toBeDefined(); // current instrument
    expect(screen.getByText('Change')).toBeDefined(); // change button
    expect(screen.getByText('Add to Portfolio')).toBeDefined();
  });

  it('step 2 shows unconfigured inputs and disables Add button', () => {
    const signal = makeFakeSignal({
      inputs: [
        { id: 'X', instrument: null },
        { id: 'Y', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'AAPL' } },
      ],
    });
    loadState.mockReturnValue({ signals: [signal] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));

    expect(screen.getByText('Not configured')).toBeDefined();
    expect(screen.getByText('Pick')).toBeDefined(); // "Pick" for unconfigured
    expect(screen.getByText('Change')).toBeDefined(); // "Change" for configured

    // Add button should be disabled when not all inputs configured
    const addBtn = screen.getByText('Add to Portfolio');
    expect(addBtn.disabled).toBe(true);
  });

  it('step 2 disables Add button when signal has zero inputs', () => {
    const signal = makeFakeSignal({ inputs: [] });
    loadState.mockReturnValue({ signals: [signal] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));

    expect(screen.getByText(/no inputs/i)).toBeDefined();
    const addBtn = screen.getByText('Add to Portfolio');
    expect(addBtn.disabled).toBe(true);
  });

  it('clicking Change opens InstrumentPickerModal and updates the input', () => {
    const signal = makeFakeSignal();
    loadState.mockReturnValue({ signals: [signal] });
    const onSelect = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={onSelect} />,
    );

    // Go to step 2
    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));

    // Click Change on input X
    fireEvent.click(screen.getByText('Change'));

    // InstrumentPickerModal stub should appear
    expect(screen.getByTestId('instrument-picker-stub')).toBeDefined();

    // Pick a spot instrument via stub
    fireEvent.click(screen.getByTestId('stub-pick-spot'));

    // Instrument picker should close, input should be updated
    expect(screen.queryByTestId('instrument-picker-stub')).toBeNull();
    expect(screen.getByText(/AAPL/)).toBeDefined();
  });

  it('Add to Portfolio calls onSelect with updated inputs', () => {
    const signal = makeFakeSignal();
    loadState.mockReturnValue({ signals: [signal] });
    const onSelect = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={onSelect} />,
    );

    // Go to step 2
    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));

    // Click Add to Portfolio (inputs are pre-filled so it's enabled)
    fireEvent.click(screen.getByText('Add to Portfolio'));

    expect(onSelect).toHaveBeenCalledTimes(1);
    const received = onSelect.mock.calls[0][0];
    expect(received.id).toBe('s1');
    expect(received.inputs[0].instrument.instrument_id).toBe('SPX');
  });

  it('back button returns to signal list', () => {
    const signal = makeFakeSignal({ name: 'Go Back Test' });
    loadState.mockReturnValue({ signals: [signal] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));
    expect(screen.getByText(/Configure: Go Back Test/)).toBeDefined();

    // Click back
    fireEvent.click(screen.getByRole('button', { name: /Back to signal list/ }));

    // Should be back on step 1
    expect(screen.getByText('Go Back Test')).toBeDefined();
    expect(screen.queryByText(/Configure:/)).toBeNull();
  });

  it('calls onClose on Escape from step 1', () => {
    loadState.mockReturnValue({ signals: [] });
    const onClose = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={onClose} onSelect={noop} />,
    );

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape from step 2 goes back to step 1 (not close)', () => {
    const signal = makeFakeSignal();
    loadState.mockReturnValue({ signals: [signal] });
    const onClose = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={onClose} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));
    expect(screen.getByText(/Configure:/)).toBeDefined();

    // Escape should go back, not close
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByText(/Configure:/)).toBeNull();
    expect(screen.getByText('Test Signal')).toBeDefined(); // back on step 1
  });

  it('handles multiple inputs correctly', () => {
    const signal = makeFakeSignal({
      inputs: [
        { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
        { id: 'Y', instrument: { type: 'continuous', collection: 'CME', adjustment: 'none', cycle: null, rollOffset: 0, strategy: 'front_month' } },
      ],
    });
    loadState.mockReturnValue({ signals: [signal] });

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(screen.getByRole('button', { name: /Configure signal/ }));

    // Both inputs should be visible
    expect(screen.getByText('X')).toBeDefined();
    expect(screen.getByText('Y')).toBeDefined();
    expect(screen.getByText(/SPX/)).toBeDefined();
    expect(screen.getByText(/CME/)).toBeDefined();

    // Two Change buttons
    const changeButtons = screen.getAllByText('Change');
    expect(changeButtons).toHaveLength(2);
  });
});
