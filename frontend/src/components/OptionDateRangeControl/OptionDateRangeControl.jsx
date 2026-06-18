import styles from './OptionDateRangeControl.module.css';

/**
 * Number of months in the default lookback window. The control no longer
 * exposes preset buttons (3M/6M/1Y/2Y were removed in PR #58 — they added
 * friction without value, and the ">1yr" slow-request warning was dropped
 * alongside them). The default window is a fixed 1-year lookback ending
 * today; the user adjusts it with the two date inputs.
 */
const DEFAULT_WINDOW_MONTHS = 12;

/** Format a Date as YYYY-MM-DD (local time). */
function toISO(date) {
  const yy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yy}-${mm}-${dd}`;
}

/**
 * Pure function: compute the default ``{ start, end }`` ISO-date window.
 *
 * ``end`` is today; ``start`` is today minus ``DEFAULT_WINDOW_MONTHS`` months,
 * using the built-in ``Date(year, month - N, day)`` rollover so month
 * arithmetic is correct for short months and leap years. The day is clamped
 * to the last valid day of the target month (so e.g. Aug 31 − 6m = Feb 28,
 * not Mar 3).
 *
 * @returns {{ start: string, end: string }}
 */
export function computeDefaultRange() {
  const anchor = new Date();
  const y = anchor.getFullYear();
  const m = anchor.getMonth(); // 0-based
  const d = anchor.getDate();

  const end = toISO(anchor);

  const target = new Date(y, m - DEFAULT_WINDOW_MONTHS, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  const startDate = new Date(target.getFullYear(), target.getMonth(), Math.min(d, lastDay));
  const start = toISO(startDate);

  return { start, end };
}

/**
 * Compact date-range picker — two native ``<input type="date">`` fields.
 *
 * The preset buttons and the ">1yr" warning were removed (PR #58); the value
 * shape is now just ``{ start, end }`` (ISO 'YYYY-MM-DD' strings). For
 * backward compatibility the component tolerates a legacy ``{ start, end,
 * preset }`` value — it simply ignores the ``preset`` key — and always emits
 * a plain ``{ start, end }`` upward.
 *
 * @param {{ start: string, end: string }} value
 * @param {(v: { start: string, end: string }) => void} onChange
 * @param {boolean} [disabled]
 */
export default function OptionDateRangeControl({ value, onChange, disabled = false }) {
  const { start, end } = value;

  const handleStartChange = (e) => {
    onChange({ start: e.target.value, end });
  };

  const handleEndChange = (e) => {
    onChange({ start, end: e.target.value });
  };

  return (
    <fieldset
      className={styles.root}
      data-testid="option-date-range-control"
      disabled={disabled}
    >
      <label className={styles.dateLabel}>
        From
        <input
          type="date"
          className={styles.dateInput}
          value={start}
          onChange={handleStartChange}
          disabled={disabled}
          aria-label="Start date"
        />
      </label>

      <label className={styles.dateLabel}>
        To
        <input
          type="date"
          className={styles.dateInput}
          value={end}
          onChange={handleEndChange}
          disabled={disabled}
          aria-label="End date"
        />
      </label>
    </fieldset>
  );
}
