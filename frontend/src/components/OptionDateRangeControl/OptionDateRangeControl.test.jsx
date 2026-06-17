// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import OptionDateRangeControl, { computeDefaultRange } from './OptionDateRangeControl';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// computeDefaultRange — pure function tests
//
// The preset buttons (3M/6M/1Y/2Y) and ">1yr" warning were removed in PR #58.
// The default window is now a fixed 1-year lookback ending today.
// ---------------------------------------------------------------------------
describe('computeDefaultRange', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it('returns a 1-year window ending today', () => {
    vi.setSystemTime(new Date(2025, 6, 15)); // July 15 2025
    const { start, end } = computeDefaultRange();
    expect(end).toBe('2025-07-15');
    expect(start).toBe('2024-07-15');
  });

  it('handles year boundary', () => {
    vi.setSystemTime(new Date(2026, 0, 5)); // Jan 5 2026
    const { start, end } = computeDefaultRange();
    expect(end).toBe('2026-01-05');
    expect(start).toBe('2025-01-05');
  });

  it('clamps the day for short target months (Feb 29 → Feb 28 next year)', () => {
    // From Feb 29 2024 (leap), one year back lands on a non-existent Feb 29
    // 2023 → clamp to Feb 28 2023.
    vi.setSystemTime(new Date(2024, 1, 29)); // Feb 29 2024
    const { start, end } = computeDefaultRange();
    expect(end).toBe('2024-02-29');
    expect(start).toBe('2023-02-28');
  });
});

// ---------------------------------------------------------------------------
// <OptionDateRangeControl> — component tests
// ---------------------------------------------------------------------------
describe('<OptionDateRangeControl>', () => {
  let onChange;
  const baseValue = { start: '2024-07-01', end: '2025-07-01' };

  beforeEach(() => {
    onChange = vi.fn();
  });

  it('renders two date inputs and no preset buttons', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    expect(screen.getByLabelText('Start date')).toBeTruthy();
    expect(screen.getByLabelText('End date')).toBeTruthy();
    // Preset buttons are gone.
    expect(screen.queryByText('3M')).toBeNull();
    expect(screen.queryByText('6M')).toBeNull();
    expect(screen.queryByText('1Y')).toBeNull();
    expect(screen.queryByText('2Y')).toBeNull();
  });

  it('never renders the ">1yr" warning, even for a multi-year range', () => {
    const longRange = { start: '2023-01-01', end: '2025-07-01' };
    render(<OptionDateRangeControl value={longRange} onChange={onChange} />);
    expect(screen.queryByTestId('range-warning')).toBeNull();
    expect(screen.queryByText(/range exceeds 1 year/i)).toBeNull();
  });

  it('changing the start date fires onChange with {start, end} (no preset key)', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2024-03-01' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0]).toEqual({ start: '2024-03-01', end: '2025-07-01' });
    expect(onChange.mock.calls[0][0]).not.toHaveProperty('preset');
  });

  it('changing the end date fires onChange with {start, end} (no preset key)', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-08-01' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0]).toEqual({ start: '2024-07-01', end: '2025-08-01' });
    expect(onChange.mock.calls[0][0]).not.toHaveProperty('preset');
  });

  it('date inputs reflect the value prop', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} />);
    expect(screen.getByLabelText('Start date').value).toBe('2024-07-01');
    expect(screen.getByLabelText('End date').value).toBe('2025-07-01');
  });

  it('tolerates a legacy value that still carries a preset key (ignores it)', () => {
    const legacy = { start: '2024-07-01', end: '2025-07-01', preset: '6m' };
    render(<OptionDateRangeControl value={legacy} onChange={onChange} />);
    // Renders start/end from the legacy value; the preset key is inert.
    expect(screen.getByLabelText('Start date').value).toBe('2024-07-01');
    expect(screen.getByLabelText('End date').value).toBe('2025-07-01');
    // And edits drop the preset key.
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2024-09-01' } });
    expect(onChange.mock.calls[0][0]).toEqual({ start: '2024-09-01', end: '2025-07-01' });
  });

  it('disabled prop disables both date inputs', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} disabled />);
    expect(screen.getByLabelText('Start date').disabled).toBe(true);
    expect(screen.getByLabelText('End date').disabled).toBe(true);
  });

  it('disabled prop disables the fieldset', () => {
    render(<OptionDateRangeControl value={baseValue} onChange={onChange} disabled />);
    const root = screen.getByTestId('option-date-range-control');
    expect(root.disabled).toBe(true);
  });
});
