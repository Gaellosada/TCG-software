// @vitest-environment jsdom
//
// Tests for the option date range integration in the Indicators page:
//   - hasOptionStreamRef utility
//   - OptionDateRangeControl visibility toggle in ParamsPanel
//   - localStorage persistence of optionDateRange (now {start, end})
//   - computeDefaultRange
//
// PR #58 removed the preset buttons (3M/6M/1Y/2Y) and the ">1yr" warning from
// the shared OptionDateRangeControl; the value shape is now {start, end} and
// the default is a 1-year window ending today.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { hasOptionStreamRef } from './IndicatorsPage';
import { computeDefaultRange } from '../../components/OptionDateRangeControl';
import ParamsPanel from './ParamsPanel';
import { OPTION_DATE_RANGE_KEY } from './storageKeys';

// Mock API modules that InstrumentPickerModal loads on mount.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX', 'FUT_ES']),
  listInstruments: vi.fn(async () => ({
    items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 500,
  })),
  getAvailableCycles: vi.fn(async () => []),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  try { localStorage.removeItem(OPTION_DATE_RANGE_KEY); } catch { /* noop */ }
});

// ---------------------------------------------------------------------------
// hasOptionStreamRef — pure unit tests
// ---------------------------------------------------------------------------
describe('hasOptionStreamRef', () => {
  it('returns false when indicator is null', () => {
    expect(hasOptionStreamRef(null)).toBe(false);
  });

  it('returns false when seriesMap is empty', () => {
    expect(hasOptionStreamRef({ seriesMap: {} })).toBe(false);
  });

  it('returns false when all refs are spot', () => {
    expect(hasOptionStreamRef({
      seriesMap: {
        price: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
      },
    })).toBe(false);
  });

  it('returns false when all refs are continuous', () => {
    expect(hasOptionStreamRef({
      seriesMap: {
        price: { type: 'continuous', collection: 'FUT_ES' },
      },
    })).toBe(false);
  });

  it('returns true when at least one ref is option_stream', () => {
    expect(hasOptionStreamRef({
      seriesMap: {
        price: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
        iv: { type: 'option_stream', collection: 'OPT_SP_500' },
      },
    })).toBe(true);
  });

  it('returns true when all refs are option_stream', () => {
    expect(hasOptionStreamRef({
      seriesMap: {
        iv: { type: 'option_stream', collection: 'OPT_SP_500' },
      },
    })).toBe(true);
  });

  it('returns false when seriesMap has null slots', () => {
    expect(hasOptionStreamRef({
      seriesMap: { price: null },
    })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// computeDefaultRange
// ---------------------------------------------------------------------------
describe('computeDefaultRange', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 15)); // July 15 2025
  });

  it('returns a 1-year window ending today', () => {
    const { start, end } = computeDefaultRange();
    expect(end).toBe('2025-07-15');
    expect(start).toBe('2024-07-15');
  });
});

// ---------------------------------------------------------------------------
// ParamsPanel — date range control visibility
// ---------------------------------------------------------------------------
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
    showDateRange: false,
    optionDateRange: null,
    onOptionDateRangeChange: vi.fn(),
    ...overrides,
  };
}

describe('<ParamsPanel> — option date range control', () => {
  it('does NOT render date range control when showDateRange is false', () => {
    render(<ParamsPanel {...baseProps({ showDateRange: false })} />);
    expect(screen.queryByTestId('option-date-range-row')).toBeNull();
  });

  it('does NOT render date range control when optionDateRange is null', () => {
    render(<ParamsPanel {...baseProps({ showDateRange: true, optionDateRange: null })} />);
    expect(screen.queryByTestId('option-date-range-row')).toBeNull();
  });

  it('renders date range control when showDateRange is true and optionDateRange is set', () => {
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2024-07-01', end: '2025-07-01' },
    })} />);
    expect(screen.getByTestId('option-date-range-row')).toBeTruthy();
    expect(screen.getByTestId('option-date-range-control')).toBeTruthy();
    expect(screen.getByText('Option date range')).toBeTruthy();
  });

  it('does NOT render preset buttons inside the date range control', () => {
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2024-07-01', end: '2025-07-01' },
    })} />);
    expect(screen.queryByText('3M')).toBeNull();
    expect(screen.queryByText('6M')).toBeNull();
    expect(screen.queryByText('1Y')).toBeNull();
    expect(screen.queryByText('2Y')).toBeNull();
  });

  it('calls onOptionDateRangeChange with {start, end} when start date is changed', () => {
    const onChange = vi.fn();
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2024-07-01', end: '2025-07-01' },
      onOptionDateRangeChange: onChange,
    })} />);
    const startInput = screen.getByLabelText('Start date');
    fireEvent.change(startInput, { target: { value: '2024-06-01' } });
    expect(onChange).toHaveBeenCalledWith({ start: '2024-06-01', end: '2025-07-01' });
  });

  it('disables the control when running is true', () => {
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2024-07-01', end: '2025-07-01' },
      running: true,
    })} />);
    const fieldset = screen.getByTestId('option-date-range-control');
    expect(fieldset.disabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// localStorage persistence — storageKeys
// ---------------------------------------------------------------------------
describe('OPTION_DATE_RANGE_KEY', () => {
  it('follows the tcg.indicators namespace', () => {
    expect(OPTION_DATE_RANGE_KEY).toBe('tcg.indicators.optionDateRange');
  });
});
