// @vitest-environment jsdom
//
// Tests for the option date range integration in the Indicators page:
//   - hasOptionStreamRef utility
//   - OptionDateRangeControl visibility toggle in ParamsPanel
//   - localStorage persistence of optionDateRange
//   - computePresetRange with anchorEnd

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { hasOptionStreamRef } from './IndicatorsPage';
import { computePresetRange, DEFAULT_PRESET, PRESETS } from '../../components/OptionDateRangeControl';
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
// computePresetRange — anchorEnd tests
// ---------------------------------------------------------------------------
describe('computePresetRange — anchorEnd', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 15)); // July 15 2025
  });

  it('uses anchorEnd when provided', () => {
    const { start, end } = computePresetRange('6m', '2024-12-31');
    expect(end).toBe('2024-12-31');
    expect(start).toBe('2024-06-30'); // 6 months before Dec 31
  });

  it('uses today when anchorEnd is omitted', () => {
    const { start, end } = computePresetRange('6m');
    expect(end).toBe('2025-07-15');
    expect(start).toBe('2025-01-15');
  });

  it('3m preset anchored to a specific date', () => {
    const { start, end } = computePresetRange('3m', '2025-03-15');
    expect(end).toBe('2025-03-15');
    expect(start).toBe('2024-12-15');
  });

  it('1y preset anchored to a specific date', () => {
    const { start, end } = computePresetRange('1y', '2025-06-01');
    expect(end).toBe('2025-06-01');
    expect(start).toBe('2024-06-01');
  });

  it('clamps day for short months with anchorEnd', () => {
    // Aug 31 - 6 months = Feb 28 (non-leap 2025)
    const { start, end } = computePresetRange('6m', '2025-08-31');
    expect(end).toBe('2025-08-31');
    expect(start).toBe('2025-02-28');
  });

  it('throws for invalid anchorEnd', () => {
    expect(() => computePresetRange('6m', 'not-a-date')).toThrow('Invalid anchorEnd date');
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
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1)); // July 1 2025
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2025-01-01', end: '2025-07-01', preset: '6m' },
    })} />);
    expect(screen.getByTestId('option-date-range-row')).toBeTruthy();
    expect(screen.getByTestId('option-date-range-control')).toBeTruthy();
    expect(screen.getByText('Option date range')).toBeTruthy();
  });

  it('renders preset buttons inside the date range control', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1));
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2025-01-01', end: '2025-07-01', preset: '6m' },
    })} />);
    expect(screen.getByText('3M')).toBeTruthy();
    expect(screen.getByText('6M')).toBeTruthy();
    expect(screen.getByText('1Y')).toBeTruthy();
    expect(screen.getByText('2Y')).toBeTruthy();
  });

  it('calls onOptionDateRangeChange when a preset button is clicked', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1));
    const onChange = vi.fn();
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2025-01-01', end: '2025-07-01', preset: '6m' },
      onOptionDateRangeChange: onChange,
    })} />);
    fireEvent.click(screen.getByText('3M'));
    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0];
    expect(arg.preset).toBe('3m');
    expect(arg.end).toBe('2025-07-01');
    expect(arg.start).toBe('2025-04-01');
  });

  it('calls onOptionDateRangeChange with preset=null when start date is manually changed', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1));
    const onChange = vi.fn();
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2025-01-01', end: '2025-07-01', preset: '6m' },
      onOptionDateRangeChange: onChange,
    })} />);
    const startInput = screen.getByLabelText('Start date');
    fireEvent.change(startInput, { target: { value: '2024-06-01' } });
    expect(onChange).toHaveBeenCalledWith({
      start: '2024-06-01',
      end: '2025-07-01',
      preset: null,
    });
  });

  it('disables the control when running is true', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1));
    render(<ParamsPanel {...baseProps({
      showDateRange: true,
      optionDateRange: { start: '2025-01-01', end: '2025-07-01', preset: '6m' },
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

// ---------------------------------------------------------------------------
// Exports from OptionDateRangeControl
// ---------------------------------------------------------------------------
describe('OptionDateRangeControl exports', () => {
  it('DEFAULT_PRESET is 6m', () => {
    expect(DEFAULT_PRESET).toBe('6m');
  });

  it('PRESETS is ordered correctly', () => {
    expect(PRESETS).toEqual(['3m', '6m', '1y', '2y']);
  });
});
