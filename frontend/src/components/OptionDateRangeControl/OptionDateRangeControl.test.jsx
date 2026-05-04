// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import OptionDateRangeControl, {
  computePresetRange,
  DEFAULT_PRESET,
  PRESETS,
} from './OptionDateRangeControl';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// computePresetRange — pure function tests
// ---------------------------------------------------------------------------
describe('computePresetRange', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it('returns correct range for 6m preset', () => {
    vi.setSystemTime(new Date(2025, 6, 15)); // July 15 2025
    const { start, end } = computePresetRange('6m');
    expect(end).toBe('2025-07-15');
    expect(start).toBe('2025-01-15');
  });

  it('returns correct range for 3m preset', () => {
    vi.setSystemTime(new Date(2025, 3, 10)); // April 10 2025
    const { start, end } = computePresetRange('3m');
    expect(end).toBe('2025-04-10');
    expect(start).toBe('2025-01-10');
  });

  it('returns correct range for 1y preset', () => {
    vi.setSystemTime(new Date(2025, 5, 1)); // June 1 2025
    const { start, end } = computePresetRange('1y');
    expect(end).toBe('2025-06-01');
    expect(start).toBe('2024-06-01');
  });

  it('returns correct range for 2y preset', () => {
    vi.setSystemTime(new Date(2025, 11, 31)); // Dec 31 2025
    const { start, end } = computePresetRange('2y');
    expect(end).toBe('2025-12-31');
    expect(start).toBe('2023-12-31');
  });

  it('handles month underflow (wraps year)', () => {
    vi.setSystemTime(new Date(2025, 1, 15)); // Feb 15 2025
    const { start, end } = computePresetRange('3m');
    expect(end).toBe('2025-02-15');
    expect(start).toBe('2024-11-15');
  });

  it('clamps day for short months (Aug 31 - 6m = Feb 28)', () => {
    vi.setSystemTime(new Date(2025, 7, 31)); // Aug 31 2025
    const { start } = computePresetRange('6m');
    // 6 months back from Aug 31 = Feb 28 (2025 not leap year)
    expect(start).toBe('2025-02-28');
  });

  it('handles leap year: Feb 29 target month', () => {
    vi.setSystemTime(new Date(2024, 7, 31)); // Aug 31 2024 (leap year)
    const { start } = computePresetRange('6m');
    // 6 months back from Aug 31 2024 = Feb 29 2024 (leap year, 29 days)
    expect(start).toBe('2024-02-29');
  });

  it('throws for unknown preset', () => {
    expect(() => computePresetRange('5y')).toThrow('Unknown preset: 5y');
  });

  it('uses anchorEnd when provided', () => {
    const { start, end } = computePresetRange('6m', '2024-12-31');
    expect(end).toBe('2024-12-31');
    expect(start).toBe('2024-06-30');
  });

  it('throws for invalid anchorEnd', () => {
    expect(() => computePresetRange('6m', 'not-a-date')).toThrow('Invalid anchorEnd date');
  });
});

// ---------------------------------------------------------------------------
// Exported constants
// ---------------------------------------------------------------------------
describe('exports', () => {
  it('DEFAULT_PRESET is 6m', () => {
    expect(DEFAULT_PRESET).toBe('6m');
  });

  it('PRESETS is ordered correctly', () => {
    expect(PRESETS).toEqual(['3m', '6m', '1y', '2y']);
  });
});

// ---------------------------------------------------------------------------
// <OptionDateRangeControl> — component tests
// ---------------------------------------------------------------------------
describe('<OptionDateRangeControl>', () => {
  let onChange;
  const baseValue = { start: '2025-01-01', end: '2025-07-01', preset: '6m' };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 6, 1)); // July 1 2025
    onChange = vi.fn();
  });

  it('renders four preset buttons and two date inputs', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    expect(screen.getByText('3M')).toBeTruthy();
    expect(screen.getByText('6M')).toBeTruthy();
    expect(screen.getByText('1Y')).toBeTruthy();
    expect(screen.getByText('2Y')).toBeTruthy();
    expect(screen.getByLabelText('Start date')).toBeTruthy();
    expect(screen.getByLabelText('End date')).toBeTruthy();
  });

  it('highlights the active preset button via aria-pressed', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    const btn6m = screen.getByText('6M');
    expect(btn6m.getAttribute('aria-pressed')).toBe('true');
    const btn3m = screen.getByText('3M');
    expect(btn3m.getAttribute('aria-pressed')).toBe('false');
  });

  it('no preset highlighted when preset is null', () => {
    const customValue = { ...baseValue, preset: null };
    render(<OptionDateRangeControl value={customValue} onChange={onChange} />);
    for (const label of ['3M', '6M', '1Y', '2Y']) {
      expect(screen.getByText(label).getAttribute('aria-pressed')).toBe('false');
    }
  });

  it('clicking a preset fires onChange with computed range', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    fireEvent.click(screen.getByText('3M'));
    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0];
    expect(arg.preset).toBe('3m');
    expect(arg.end).toBe('2025-07-01');
    expect(arg.start).toBe('2025-04-01');
  });

  it('changing start date fires onChange with preset=null', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    const startInput = screen.getByLabelText('Start date');
    fireEvent.change(startInput, { target: { value: '2024-06-01' } });
    expect(onChange).toHaveBeenCalledWith({
      start: '2024-06-01',
      end: '2025-07-01',
      preset: null,
    });
  });

  it('changing end date fires onChange with preset=null', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    const endInput = screen.getByLabelText('End date');
    fireEvent.change(endInput, { target: { value: '2025-08-01' } });
    expect(onChange).toHaveBeenCalledWith({
      start: '2025-01-01',
      end: '2025-08-01',
      preset: null,
    });
  });

  it('shows warning when range exceeds 1 year', () => {
    const longRange = { start: '2023-01-01', end: '2025-07-01', preset: '2y' };
    render(<OptionDateRangeControl value={longRange} onChange={onChange} />);
    expect(screen.getByTestId('range-warning')).toBeTruthy();
    expect(screen.getByText(/range exceeds 1 year/i)).toBeTruthy();
  });

  it('does not show warning when range is within 1 year', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    expect(screen.queryByTestId('range-warning')).toBeNull();
  });

  it('disabled prop disables date inputs', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} disabled />);
    expect(screen.getByLabelText('Start date').disabled).toBe(true);
    expect(screen.getByLabelText('End date').disabled).toBe(true);
  });

  it('disabled prop disables the fieldset', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} disabled />);
    const root = screen.getByTestId('option-date-range-control');
    expect(root.disabled).toBe(true);
  });

  it('date inputs reflect value prop', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    expect(screen.getByLabelText('Start date').value).toBe('2025-01-01');
    expect(screen.getByLabelText('End date').value).toBe('2025-07-01');
  });

  it('clicking a preset with anchorEnd anchors to the provided date, not today', () => {
    // System time is July 1 2025, but anchorEnd overrides to Dec 31 2024.
    render(
      <OptionDateRangeControl
        value={baseValue}
        onChange={onChange}
        anchorEnd="2024-12-31"
      />,
    );
    fireEvent.click(screen.getByText('6M'));
    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0];
    expect(arg.preset).toBe('6m');
    // end anchored to anchorEnd, not today (2025-07-01)
    expect(arg.end).toBe('2024-12-31');
    expect(arg.start).toBe('2024-06-30');
  });

  it('without anchorEnd, clicking a preset uses today as anchor', () => {
    // System time set to July 1 2025 in beforeEach.
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    fireEvent.click(screen.getByText('6M'));
    expect(onChange).toHaveBeenCalledOnce();
    const arg = onChange.mock.calls[0][0];
    expect(arg.end).toBe('2025-07-01'); // today
    expect(arg.start).toBe('2025-01-01');
  });

  it('anchorEnd prop does not affect manual date input changes', () => {
    render(
      <OptionDateRangeControl
        value={baseValue}
        onChange={onChange}
        anchorEnd="2024-12-31"
      />,
    );
    const startInput = screen.getByLabelText('Start date');
    fireEvent.change(startInput, { target: { value: '2024-03-01' } });
    // Manual change still sets preset=null and uses the typed value
    expect(onChange).toHaveBeenCalledWith({
      start: '2024-03-01',
      end: '2025-07-01',
      preset: null,
    });
  });
});
