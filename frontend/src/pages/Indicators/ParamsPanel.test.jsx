// @vitest-environment jsdom
//
// Tests for ParamsPanel: ownPanel checkbox and value mapping adapter
// (fromPickerValue) used to bridge InstrumentPickerModal's discriminated-union
// output with the internal { collection, instrument_id } series map.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ParamsPanel, { fromPickerValue } from './ParamsPanel';

// InstrumentPickerModal loads collections/instruments on mount — mock the API
// so the component doesn't throw during rendering.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX', 'FUT_ES']),
  listInstruments: vi.fn(async () => ({
    items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 500,
  })),
  getAvailableCycles: vi.fn(async () => []),
}));

afterEach(() => {
  cleanup();
});

function baseProps(overrides = {}) {
  return {
    indicator: {
      id: 'u1',
      name: 'My ind',
      code: "def compute(series):\n    return series['close']",
      params: {},
      seriesMap: {},
      readonly: false,
    },
    paramsSpec: [],
    seriesLabels: [],
    onParamChange: vi.fn(),
    onSeriesSave: vi.fn(),
    onRun: vi.fn(),
    running: false,
    canRun: false,
    runDisabledReason: null,
    defaultCollection: null,
    ownPanel: false,
    onOwnPanelChange: vi.fn(),
    ...overrides,
  };
}

describe('<ParamsPanel> — ownPanel checkbox', () => {
  it('renders the checkbox labelled "Show in separate panel below"', () => {
    render(<ParamsPanel {...baseProps()} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb).toBeTruthy();
    expect(cb.checked).toBe(false);
    expect(screen.getByText(/show in separate panel below/i)).toBeTruthy();
  });

  it('reflects the current ownPanel value', () => {
    render(<ParamsPanel {...baseProps({ ownPanel: true })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.checked).toBe(true);
  });

  it('calls onOwnPanelChange with the new checked state when toggled', () => {
    const onOwnPanelChange = vi.fn();
    render(<ParamsPanel {...baseProps({ ownPanel: false, onOwnPanelChange })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    fireEvent.click(cb);
    expect(onOwnPanelChange).toHaveBeenCalledTimes(1);
    expect(onOwnPanelChange).toHaveBeenCalledWith(true);
  });

  it('is disabled (native attr) and does not fire callback when indicator is readonly', () => {
    const onOwnPanelChange = vi.fn();
    render(
      <ParamsPanel
        {...baseProps({
          indicator: { ...baseProps().indicator, readonly: true },
          ownPanel: true,
          onOwnPanelChange,
        })}
      />,
    );
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.disabled).toBe(true);
    fireEvent.click(cb);
    expect(onOwnPanelChange).not.toHaveBeenCalled();
  });

  it('is disabled when no indicator is selected', () => {
    render(<ParamsPanel {...baseProps({ indicator: null })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.disabled).toBe(true);
  });
});

describe('value mapping — fromPickerValue', () => {
  it('passes through spot type with all fields', () => {
    expect(fromPickerValue({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' }))
      .toEqual({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' });
  });

  it('passes through continuous type with all fields', () => {
    const input = {
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 2,
      strategy: 'front_month',
    };
    expect(fromPickerValue(input)).toEqual(input);
  });

  it('returns null for null input', () => {
    expect(fromPickerValue(null)).toBeNull();
  });
});

describe('<ParamsPanel> — instrument picker button', () => {
  it('renders a picker trigger button for each series label', () => {
    render(<ParamsPanel {...baseProps({ seriesLabels: ['close', 'volume'] })} />);
    expect(screen.getByTestId('instrument-picker-close')).toBeTruthy();
    expect(screen.getByTestId('instrument-picker-volume')).toBeTruthy();
  });

  it('shows "Select instrument" when no instrument is picked', () => {
    render(<ParamsPanel {...baseProps({ seriesLabels: ['close'] })} />);
    const btn = screen.getByTestId('instrument-picker-close');
    expect(btn.textContent).toBe('Select instrument');
  });

  it('shows change icon and chip when an instrument is already picked', () => {
    render(<ParamsPanel {...baseProps({
      seriesLabels: ['close'],
      indicator: {
        ...baseProps().indicator,
        seriesMap: { close: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
      },
    })} />);
    // When picked, the edit button (✎) carries the testid; the chip shows the label.
    const btn = screen.getByTestId('instrument-picker-close');
    expect(btn.title).toBe('Change instrument');
    // Chip label: "INDEX / SPX"
    expect(screen.getByText('INDEX / SPX')).toBeTruthy();
  });
});
