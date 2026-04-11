import { useCallback, useMemo, useRef } from 'react';
import styles from './TimeRangeSlider.module.css';

function parseDate(dateStr) {
  const [y, m] = dateStr.split('-').map(Number);
  return { year: y, month: m - 1 };
}

function monthIndexToDate(minDate, index) {
  const min = parseDate(minDate);
  const totalMonths = min.month + index;
  const year = min.year + Math.floor(totalMonths / 12);
  const month = totalMonths % 12;
  const mm = String(month + 1).padStart(2, '0');
  return `${year}-${mm}-01`;
}

function dateToMonthIndex(minDate, dateStr) {
  const min = parseDate(minDate);
  const d = parseDate(dateStr);
  return (d.year - min.year) * 12 + (d.month - min.month);
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function formatLabel(dateStr) {
  const { year, month } = parseDate(dateStr);
  return `${MONTH_NAMES[month]} ${year}`;
}

/**
 * Dual-handle range slider for selecting a date range by month.
 *
 * Props:
 *   minDate  — earliest available date "YYYY-MM-DD" (or null)
 *   maxDate  — latest available date "YYYY-MM-DD" (or null)
 *   startDate — current start selection "YYYY-MM-DD" (or '' = use min)
 *   endDate   — current end selection "YYYY-MM-DD" (or '' = use max)
 *   disabled  — disable interaction
 *   onChange({ startDate, endDate }) — callback with ISO date strings
 */
export default function TimeRangeSlider({ minDate, maxDate, startDate, endDate, disabled, onChange }) {
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;

  const effectiveMin = minDate || '1990-01-01';
  const effectiveMax = maxDate || new Date().toISOString().slice(0, 10);

  const totalMonths = useMemo(
    () => dateToMonthIndex(effectiveMin, effectiveMax),
    [effectiveMin, effectiveMax],
  );

  const startIdx = startDate ? dateToMonthIndex(effectiveMin, startDate) : 0;
  const endIdx = endDate ? dateToMonthIndex(effectiveMin, endDate) : totalMonths;

  const clampedStart = Math.max(0, Math.min(startIdx, totalMonths));
  const clampedEnd = Math.max(0, Math.min(endIdx, totalMonths));

  const leftPct = totalMonths > 0 ? (clampedStart / totalMonths) * 100 : 0;
  const rightPct = totalMonths > 0 ? (clampedEnd / totalMonths) * 100 : 100;

  const handleStartChange = useCallback((e) => {
    if (disabledRef.current) return;
    const val = Math.min(Number(e.target.value), clampedEnd);
    onChange({
      startDate: monthIndexToDate(effectiveMin, val),
      endDate: endDate || monthIndexToDate(effectiveMin, clampedEnd),
    });
  }, [clampedEnd, effectiveMin, endDate, onChange]);

  const handleEndChange = useCallback((e) => {
    if (disabledRef.current) return;
    const val = Math.max(Number(e.target.value), clampedStart);
    onChange({
      startDate: startDate || monthIndexToDate(effectiveMin, clampedStart),
      endDate: monthIndexToDate(effectiveMin, val),
    });
  }, [clampedStart, effectiveMin, startDate, onChange]);

  const minLabel = formatLabel(effectiveMin);
  const maxLabel = formatLabel(effectiveMax);

  const hasSelection = clampedStart > 0 || clampedEnd < totalMonths;
  const selStartLabel = startDate ? formatLabel(startDate) : minLabel;
  const selEndLabel = endDate ? formatLabel(endDate) : maxLabel;

  if (totalMonths <= 0) return null;

  return (
    <div className={`${styles.wrapper} ${disabled ? styles.disabled : ''}`}>
      <div className={styles.header}>
        <label className={styles.label}>Timeframe</label>
        {hasSelection && (
          <span className={styles.selection}>
            {selStartLabel} &mdash; {selEndLabel}
          </span>
        )}
      </div>
      <div className={styles.slider}>
        <div className={styles.track} />
        <div
          className={styles.fill}
          style={{ left: `${leftPct}%`, width: `${rightPct - leftPct}%` }}
        />
        <input
          type="range"
          className={`${styles.input} ${styles.inputStart}`}
          min={0}
          max={totalMonths}
          value={clampedStart}
          onChange={handleStartChange}
          disabled={disabled}
          aria-label="Start date"
        />
        <input
          type="range"
          className={`${styles.input} ${styles.inputEnd}`}
          min={0}
          max={totalMonths}
          value={clampedEnd}
          onChange={handleEndChange}
          disabled={disabled}
          aria-label="End date"
        />
      </div>
      <div className={styles.labels}>
        <span>{minLabel}</span>
        <span>{maxLabel}</span>
      </div>
    </div>
  );
}
