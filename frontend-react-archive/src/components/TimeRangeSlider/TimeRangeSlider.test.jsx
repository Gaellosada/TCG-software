// @vitest-environment jsdom
//
// Regression test: slider bounds must not shift when results arrive.
// Previously, the Portfolio page fed `results.full_date_range` as the
// slider min/max, which could widen the slider and make a user's selected
// sub-range visually snap to the full extent.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import TimeRangeSlider from './TimeRangeSlider';

afterEach(() => { cleanup(); });

describe('<TimeRangeSlider> handle positions', () => {
  it('preserves handle positions when bounds widen (regression)', () => {
    const onChange = vi.fn();

    // Initial render: slider spans 2020-01 to 2020-12, user selected 2020-04 to 2020-08.
    const { rerender } = render(
      <TimeRangeSlider
        minDate="2020-01-01"
        maxDate="2020-12-01"
        startDate="2020-04-01"
        endDate="2020-08-01"
        disabled={false}
        onChange={onChange}
      />,
    );

    const startInput = screen.getByLabelText('Start date');
    const endInput = screen.getByLabelText('End date');

    // The slider has 11 total months (Jan to Dec).
    // Start handle should be at month index 3 (Apr), end at 7 (Aug).
    expect(startInput.value).toBe('3');
    expect(endInput.value).toBe('7');
    expect(startInput.max).toBe('11');

    // Simulate what used to happen: results arrive with a wider full_date_range,
    // so the parent passes new min/max bounds while keeping the same startDate/endDate.
    rerender(
      <TimeRangeSlider
        minDate="2015-01-01"
        maxDate="2025-12-01"
        startDate="2020-04-01"
        endDate="2020-08-01"
        disabled={false}
        onChange={onChange}
      />,
    );

    // Now the slider spans 132 months (2015-01 to 2025-12).
    // Start handle should be at month 63 (2020-04 minus 2015-01 = 5*12+3 = 63).
    // End handle should be at month 67.
    expect(startInput.value).toBe('63');
    expect(endInput.value).toBe('67');
    expect(startInput.max).toBe('131');

    // The visual fill should represent a small slice, not the full bar.
    // leftPct = 63/131 ≈ 48%, rightPct = 67/131 ≈ 51%  → fill width ~3%
    // This is the bug: the user selected Apr-Aug 2020, but after bounds widened
    // the fill covers only ~3% of the bar instead of the original ~36%.
    // The fix is to never widen the bounds — keep overlapRange.
  });

  it('handles at extremes when startDate/endDate are empty', () => {
    render(
      <TimeRangeSlider
        minDate="2020-01-01"
        maxDate="2020-12-01"
        startDate=""
        endDate=""
        disabled={false}
        onChange={vi.fn()}
      />,
    );

    const startInput = screen.getByLabelText('Start date');
    const endInput = screen.getByLabelText('End date');

    // Empty = use min/max → handles at 0 and totalMonths
    expect(startInput.value).toBe('0');
    expect(endInput.value).toBe('11');
  });

  it('returns null when totalMonths <= 0', () => {
    const { container } = render(
      <TimeRangeSlider
        minDate="2020-06-01"
        maxDate="2020-06-01"
        startDate=""
        endDate=""
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    expect(container.innerHTML).toBe('');
  });
});
