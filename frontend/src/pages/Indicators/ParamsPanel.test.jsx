// @vitest-environment jsdom
//
// Tests for ParamsPanel: ownPanel checkbox and value mapping adapter
// (fromPickerValue) used to bridge InstrumentPickerModal's discriminated-union
// output with the internal { collection, instrument_id } series map.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ParamsPanel, { fromPickerValue, formatSeriesRefLabel } from './ParamsPanel';

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

  it('makes a picked future/option chip the clickable edit trigger (the ✎ pencil was removed, Decision D1)', () => {
    render(<ParamsPanel {...baseProps({
      seriesLabels: ['close'],
      indicator: {
        ...baseProps().indicator,
        seriesMap: { close: { type: 'continuous', collection: 'FUT_ES' } },
      },
    })} />);
    // The chip itself now carries the picker testid + an "Edit settings" title
    // (chip-click replaces the old ✎ pencil affordance).
    const chip = screen.getByTestId('instrument-picker-close');
    expect(chip.tagName).toBe('BUTTON');
    expect(chip.title).toBe('Edit settings');
    expect(chip.textContent).toBe('FUT_ES (continuous)');
    // No pencil affordance remains anywhere.
    expect(screen.queryByTitle('Change instrument')).toBeNull();
    expect(screen.queryByText('✎')).toBeNull();
  });

  it('leaves a picked spot chip as a plain, non-clickable span (no edit trigger)', () => {
    render(<ParamsPanel {...baseProps({
      seriesLabels: ['close'],
      indicator: {
        ...baseProps().indicator,
        seriesMap: { close: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
      },
    })} />);
    // Spot has no config screen → the chip is not click-to-edit.
    expect(screen.queryByTestId('instrument-picker-close')).toBeNull();
    const chip = screen.getByText('INDEX / SPX');
    expect(chip.tagName).toBe('SPAN');
  });

  it('renders a readable label for an option_stream-shaped series ref', () => {
    render(<ParamsPanel {...baseProps({
      seriesLabels: ['atm_iv'],
      indicator: {
        ...baseProps().indicator,
        seriesMap: {
          atm_iv: {
            type: 'option_stream',
            collection: 'OPT_SP_500',
            option_type: 'C',
            cycle: null,
            maturity: { kind: 'next_third_friday', offset_months: 0 },
            selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
            stream: 'iv',
          },
        },
      },
    })} />);
    // Bug-1 regression: option_stream refs used to render as "OPT_SP_500 /
    // undefined" because the chip looked for instrument_id. The new
    // formatSeriesRefLabel summarises the relevant fields.
    const chip = screen.getByText(/OPT_SP_500/);
    expect(chip.textContent).toMatch(/Call/);
    expect(chip.textContent).toMatch(/front month/);
    expect(chip.textContent).toMatch(/ATM/);
    expect(chip.textContent).toMatch(/IV/);
    expect(chip.textContent).not.toMatch(/undefined/);
  });
});

describe('formatSeriesRefLabel', () => {
  it('returns null for null / undefined input (caller falls back gracefully)', () => {
    expect(formatSeriesRefLabel(null)).toBeNull();
    expect(formatSeriesRefLabel(undefined)).toBeNull();
  });

  it('formats spot refs as "<collection> / <instrument_id>"', () => {
    expect(formatSeriesRefLabel({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' }))
      .toBe('INDEX / SPX');
  });

  it('formats continuous refs as "<collection> (continuous)"', () => {
    expect(formatSeriesRefLabel({ type: 'continuous', collection: 'FUT_ES' }))
      .toBe('FUT_ES (continuous)');
  });

  it('summarises option_stream refs with collection / side / maturity / selection / stream', () => {
    const ref = {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: 'M',
      maturity: { kind: 'next_third_friday', offset_months: 1 },
      selection: { kind: 'by_delta', target: -0.25 },
      stream: 'mid',
    };
    const label = formatSeriesRefLabel(ref);
    expect(label).toMatch(/OPT_SP_500/);
    expect(label).toMatch(/Put/);
    expect(label).toMatch(/back month/);
    // by_delta: 25Δp (signed convention; absolute pct + side from option_type).
    expect(label).toMatch(/25Δp/);
    expect(label).toMatch(/cycle=M/);
    expect(label).toMatch(/MID/);
  });

  it('returns null for a fully empty option_stream ref (defensive — caller falls back to "Select instrument")', () => {
    expect(formatSeriesRefLabel({ type: 'option_stream' })).toBeNull();
  });
});
