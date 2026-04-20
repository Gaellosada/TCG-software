// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import OperandPicker from './OperandPicker';

// Stub the data API used by SeriesPicker in the Instrument tab.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX']),
  listInstruments: vi.fn(async () => ({ items: [], total: 0, skip: 0, limit: 0 })),
}));

describe('OperandPicker tab switching', () => {
  it('renders all three tabs', () => {
    render(
      <OperandPicker
        value={{ kind: 'constant', value: 0 }}
        onChange={() => {}}
        indicators={[{ id: 'sma-20', name: '20-day SMA' }]}
      />,
    );
    expect(screen.getByTestId('operand-tab-indicator')).toBeDefined();
    expect(screen.getByTestId('operand-tab-instrument')).toBeDefined();
    expect(screen.getByTestId('operand-tab-constant')).toBeDefined();
  });

  it('opens on the tab matching the value.kind', () => {
    render(
      <OperandPicker
        value={{ kind: 'indicator', indicator_id: 'sma-20', output: 'default' }}
        onChange={() => {}}
        indicators={[{ id: 'sma-20', name: '20-day SMA' }]}
      />,
    );
    expect(screen.getByTestId('operand-tab-indicator').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('operand-tab-constant').getAttribute('aria-selected')).toBe('false');
  });

  // Iter-2: switching to Indicator emits an INCOMPLETE stub (empty
  // indicator_id). The user must pick an id from the dropdown — we no
  // longer auto-seed the first indicator. See iter-2 ORDERS "no default
  // injection".
  it('switching to Indicator emits an empty-indicator stub (no default id injected)', () => {
    const onChange = vi.fn();
    render(
      <OperandPicker
        value={{ kind: 'constant', value: 0 }}
        onChange={onChange}
        indicators={[
          { id: 'sma-20', name: '20-day SMA' },
          { id: 'rsi-14', name: '14-day RSI' },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId('operand-tab-indicator'));
    expect(onChange).toHaveBeenCalledWith({
      kind: 'indicator',
      indicator_id: '',
      output: 'default',
    });
  });

  it('switching to Constant emits a constant operand with value 0', () => {
    const onChange = vi.fn();
    render(
      <OperandPicker
        value={{ kind: 'indicator', indicator_id: 'sma-20', output: 'default' }}
        onChange={onChange}
        indicators={[{ id: 'sma-20', name: '20-day SMA' }]}
      />,
    );
    fireEvent.click(screen.getByTestId('operand-tab-constant'));
    expect(onChange).toHaveBeenCalledWith({ kind: 'constant', value: 0 });
  });

  it('switching to Instrument emits an instrument stub with field=close', () => {
    const onChange = vi.fn();
    render(
      <OperandPicker
        value={{ kind: 'constant', value: 0 }}
        onChange={onChange}
        indicators={[]}
      />,
    );
    fireEvent.click(screen.getByTestId('operand-tab-instrument'));
    expect(onChange).toHaveBeenCalledWith({
      kind: 'instrument',
      collection: '',
      instrument_id: '',
      field: 'close',
    });
  });

  it('constant input emits a new value on change', () => {
    const onChange = vi.fn();
    render(
      <OperandPicker
        value={{ kind: 'constant', value: 0 }}
        onChange={onChange}
        indicators={[]}
      />,
    );
    const input = screen.getByLabelText('Constant value');
    fireEvent.change(input, { target: { value: '42.5' } });
    expect(onChange).toHaveBeenCalledWith({ kind: 'constant', value: 42.5 });
  });

  it('renders an empty-state message when the indicator tab is opened with zero indicators', () => {
    render(
      <OperandPicker
        value={{ kind: 'indicator', indicator_id: '', output: 'default' }}
        onChange={() => {}}
        indicators={[]}
      />,
    );
    expect(screen.getByText(/No saved indicators/i)).toBeDefined();
  });
});
