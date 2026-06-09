// @vitest-environment jsdom
//
// Tests for SignalPickerModal — two-step modal: pick signal (sourced from the
// BACKEND via listSignals), then configure inputs.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import SignalPickerModal from './SignalPickerModal';

// Mock the persistence API so we control what the backend "returns".
vi.mock('../../api/persistence', () => ({
  listSignals: vi.fn(),
  describePersistenceError: vi.fn((err) => (err && err.message) || 'Unknown error'),
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

import { listSignals } from '../../api/persistence';

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const noop = () => {};

// Build a backend SignalOut-shape payload. The backend uses ``description``
// (not ``doc``); the modal must hydrate it the same way SignalsPage does.
function makePersistedSignal(overrides = {}) {
  return {
    id: 's1',
    name: 'Test Signal',
    category: 'RESEARCH',
    description: 'a note',
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
    settings: { dont_repeat: true },
    ...overrides,
  };
}

// A deferred promise helper so we can assert the in-flight loading state.
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('<SignalPickerModal>', () => {
  beforeEach(() => {
    listSignals.mockResolvedValue([]);
  });

  it('renders nothing when closed', () => {
    render(
      <SignalPickerModal isOpen={false} onClose={noop} onSelect={noop} />,
    );
    expect(screen.queryByTestId('signal-picker')).toBeNull();
  });

  // ── RED test: signals come from the backend, not localStorage ──
  it('fetches saved signals from the backend and renders them', async () => {
    const sig1 = makePersistedSignal({ id: 's1', name: 'Alpha Signal' });
    const sig2 = makePersistedSignal({ id: 's2', name: 'Beta Signal' });
    listSignals.mockResolvedValue([sig1, sig2]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    expect(await screen.findByText('Alpha Signal')).toBeDefined();
    expect(screen.getByText('Beta Signal')).toBeDefined();
    // Default category fetch is RESEARCH (matches SignalsPage default).
    expect(listSignals).toHaveBeenCalledWith('RESEARCH');
  });

  it('renders saved signals with input and block counts', async () => {
    const sig1 = makePersistedSignal({ id: 's1', name: 'Alpha Signal' });
    const sig2 = makePersistedSignal({
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
          { id: 'x1', target_entry_block_names: ['e1'], conditions: [] },
        ],
      },
    });
    listSignals.mockResolvedValue([sig1, sig2]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    expect(await screen.findByText('Alpha Signal')).toBeDefined();
    expect(screen.getByText('Beta Signal')).toBeDefined();
    expect(screen.getByText('1 input · 1 block')).toBeDefined();
    expect(screen.getByText('2 inputs · 3 blocks')).toBeDefined();
  });

  // ── Loading state ──
  it('shows a loading state while the fetch is in flight', async () => {
    const d = deferred();
    listSignals.mockReturnValue(d.promise);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    // Loading affordance present, and NOT the empty / error message.
    expect(screen.getByTestId('signal-picker-loading')).toBeDefined();
    expect(screen.queryByText(/No saved signals/)).toBeNull();
    expect(screen.queryByText(/Failed to load/)).toBeNull();

    // Resolve and confirm loading clears.
    d.resolve([makePersistedSignal({ id: 's1', name: 'Resolved Signal' })]);
    expect(await screen.findByText('Resolved Signal')).toBeDefined();
    expect(screen.queryByTestId('signal-picker-loading')).toBeNull();
  });

  // ── Error state (must NOT be the empty / "no signals" message) ──
  it('shows an error state with retry on fetch failure (NOT the empty message)', async () => {
    listSignals.mockRejectedValue(new Error('boom'));

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    const errBox = await screen.findByTestId('signal-picker-error');
    expect(errBox).toBeDefined();
    // Crucially: a failed fetch is NOT rendered as "no signals".
    expect(screen.queryByText(/No saved signals/)).toBeNull();
    // A retry affordance exists.
    expect(screen.getByRole('button', { name: /retry/i })).toBeDefined();
  });

  it('retry refetches after an error', async () => {
    listSignals.mockRejectedValueOnce(new Error('boom'));
    listSignals.mockResolvedValueOnce([makePersistedSignal({ id: 's1', name: 'Recovered' })]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    await screen.findByTestId('signal-picker-error');
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    expect(await screen.findByText('Recovered')).toBeDefined();
    expect(listSignals).toHaveBeenCalledTimes(2);
  });

  // ── Empty state per category ──
  it('shows the empty state when the backend returns zero signals', async () => {
    listSignals.mockResolvedValue([]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    expect(await screen.findByText(/No saved signals/)).toBeDefined();
  });

  // ── Category switch refetches ──
  it('switching category refetches signals for the new category', async () => {
    listSignals.mockResolvedValue([makePersistedSignal({ id: 's1', name: 'Research One' })]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    expect(await screen.findByText('Research One')).toBeDefined();
    expect(listSignals).toHaveBeenLastCalledWith('RESEARCH');

    // Switch to PROD.
    listSignals.mockResolvedValue([makePersistedSignal({ id: 's2', name: 'Prod One' })]);
    fireEvent.change(screen.getByTestId('signal-picker-category'), { target: { value: 'PROD' } });

    expect(await screen.findByText('Prod One')).toBeDefined();
    expect(listSignals).toHaveBeenLastCalledWith('PROD');
  });

  it('category selector excludes ARCHIVE', async () => {
    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );
    await screen.findByText(/No saved signals/);
    const select = screen.getByTestId('signal-picker-category');
    const values = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    expect(values).toEqual(['RESEARCH', 'DEV', 'PROD']);
  });

  it('clicking Select transitions to step 2 (configure inputs)', async () => {
    listSignals.mockResolvedValue([makePersistedSignal({ id: 's1', name: 'My Signal' })]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: /Configure signal My Signal/ }));

    // Step 2 shows the signal name in header and input configuration
    expect(screen.getByText(/Configure: My Signal/)).toBeDefined();
    expect(screen.getByText('X')).toBeDefined(); // input id
    expect(screen.getByText(/SPX/)).toBeDefined(); // current instrument
    expect(screen.getByText('Change')).toBeDefined(); // change button
    expect(screen.getByText('Add to Portfolio')).toBeDefined();
  });

  it('step 2 shows unconfigured inputs and disables Add button', async () => {
    const signal = makePersistedSignal({
      inputs: [
        { id: 'X', instrument: null },
        { id: 'Y', instrument: { type: 'spot', collection: 'EQ', instrument_id: 'AAPL' } },
      ],
    });
    listSignals.mockResolvedValue([signal]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));

    expect(screen.getByText('Not configured')).toBeDefined();
    expect(screen.getByText('Pick')).toBeDefined(); // "Pick" for unconfigured
    expect(screen.getByText('Change')).toBeDefined(); // "Change" for configured

    // Add button should be disabled when not all inputs configured
    const addBtn = screen.getByText('Add to Portfolio');
    expect(addBtn.disabled).toBe(true);
  });

  it('step 2 disables Add button when signal has zero inputs', async () => {
    const signal = makePersistedSignal({ inputs: [] });
    listSignals.mockResolvedValue([signal]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));

    expect(screen.getByText(/no inputs/i)).toBeDefined();
    const addBtn = screen.getByText('Add to Portfolio');
    expect(addBtn.disabled).toBe(true);
  });

  it('clicking Change opens InstrumentPickerModal and updates the input', async () => {
    const signal = makePersistedSignal();
    listSignals.mockResolvedValue([signal]);
    const onSelect = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={onSelect} />,
    );

    // Go to step 2
    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));

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

  it('Add to Portfolio calls onSelect with hydrated signal and updated inputs', async () => {
    const signal = makePersistedSignal();
    listSignals.mockResolvedValue([signal]);
    const onSelect = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={onSelect} />,
    );

    // Go to step 2
    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));

    // Click Add to Portfolio (inputs are pre-filled so it's enabled)
    fireEvent.click(screen.getByText('Add to Portfolio'));

    expect(onSelect).toHaveBeenCalledTimes(1);
    const received = onSelect.mock.calls[0][0];
    expect(received.id).toBe('s1');
    expect(received.name).toBe('Test Signal');
    expect(received.inputs[0].instrument.instrument_id).toBe('SPX');
    // Hydration: backend ``description`` becomes editor-shape ``doc``.
    expect(received.doc).toBe('a note');
  });

  it('back button returns to signal list', async () => {
    const signal = makePersistedSignal({ name: 'Go Back Test' });
    listSignals.mockResolvedValue([signal]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));
    expect(screen.getByText(/Configure: Go Back Test/)).toBeDefined();

    // Click back
    fireEvent.click(screen.getByRole('button', { name: /Back to signal list/ }));

    // Should be back on step 1
    expect(screen.getByText('Go Back Test')).toBeDefined();
    expect(screen.queryByText(/Configure:/)).toBeNull();
  });

  it('calls onClose on Escape from step 1', async () => {
    listSignals.mockResolvedValue([]);
    const onClose = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={onClose} onSelect={noop} />,
    );

    await screen.findByText(/No saved signals/);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape from step 2 goes back to step 1 (not close)', async () => {
    const signal = makePersistedSignal();
    listSignals.mockResolvedValue([signal]);
    const onClose = vi.fn();

    render(
      <SignalPickerModal isOpen={true} onClose={onClose} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));
    expect(screen.getByText(/Configure:/)).toBeDefined();

    // Escape should go back, not close
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByText(/Configure:/)).toBeNull();
    expect(screen.getByText('Test Signal')).toBeDefined(); // back on step 1
  });

  it('handles multiple inputs correctly', async () => {
    const signal = makePersistedSignal({
      inputs: [
        { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
        { id: 'Y', instrument: { type: 'continuous', collection: 'CME', adjustment: 'none', cycle: null, rollOffset: 0, strategy: 'front_month' } },
      ],
    });
    listSignals.mockResolvedValue([signal]);

    render(
      <SignalPickerModal isOpen={true} onClose={noop} onSelect={noop} />,
    );

    // Go to step 2
    fireEvent.click(await screen.findByRole('button', { name: /Configure signal/ }));

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
