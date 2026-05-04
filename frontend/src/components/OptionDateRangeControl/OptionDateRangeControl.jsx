import PillToggle from '../PillToggle';
import styles from './OptionDateRangeControl.module.css';

/** Ordered list of available preset keys. */
export const PRESETS = ['3m', '6m', '1y', '2y'];

/** Default preset used when no explicit value is provided. */
export const DEFAULT_PRESET = '6m';

/** Months offset for each preset key. */
const PRESET_MONTHS = { '3m': 3, '6m': 6, '1y': 12, '2y': 24 };

/**
 * Pure function: compute { start, end } ISO-date strings for a preset key.
 *
 * When `anchorEnd` is provided (an ISO 'YYYY-MM-DD' string), the range is
 * anchored to that date instead of today. This lets presets anchor to the
 * root's `last_trade_date` so the range stays within the actual data window.
 *
 * `end` is `anchorEnd` (or today).  `start` is `end` minus N months, using
 * the built-in Date(year, month - N, day) rollover so month arithmetic is
 * correct for short months and leap years.
 *
 * @param {string} preset      One of PRESETS ('3m', '6m', '1y', '2y').
 * @param {string} [anchorEnd] Optional ISO date to anchor the end of the range.
 * @returns {{ start: string, end: string }}
 */
export function computePresetRange(preset, anchorEnd) {
  const months = PRESET_MONTHS[preset];
  if (months === undefined) {
    throw new Error(`Unknown preset: ${preset}`);
  }

  const anchor = anchorEnd ? new Date(`${anchorEnd}T00:00:00`) : new Date();
  if (Number.isNaN(anchor.getTime())) {
    throw new Error(`Invalid anchorEnd date: ${anchorEnd}`);
  }

  const y = anchor.getFullYear();
  const m = anchor.getMonth(); // 0-based
  const d = anchor.getDate();

  const end = toISO(anchor);

  // Date constructor handles month underflow (e.g. month = -2 wraps to
  // the previous year).  If `d` overflows the target month the Date
  // constructor rolls forward (e.g. Mar 31 - 1 month = Mar 3 on a
  // non-leap year).  Clamp to the last day of the target month to avoid
  // that.
  const target = new Date(y, m - months, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  const startDate = new Date(target.getFullYear(), target.getMonth(), Math.min(d, lastDay));
  const start = toISO(startDate);

  return { start, end };
}

/** Format a Date as YYYY-MM-DD (local time). */
function toISO(date) {
  const yy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yy}-${mm}-${dd}`;
}

/** Check whether a date range exceeds one year. */
function rangeExceedsOneYear(start, end) {
  if (!start || !end) return false;
  const s = new Date(start);
  const e = new Date(end);
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return false;
  // 365.25 days in ms — approximate but sufficient for a UI warning.
  return e - s > 365.25 * 24 * 60 * 60 * 1000;
}

const PILL_OPTIONS = PRESETS.map((p) => ({ value: p, label: p.toUpperCase() }));

/**
 * Compact date-range picker with preset buttons (3M, 6M, 1Y, 2Y) and two
 * native `<input type="date">` fields.
 *
 * @param {{ start: string, end: string, preset: string|null }} value
 * @param {(v: { start: string, end: string, preset: string|null }) => void} onChange
 * @param {boolean} [disabled]
 */
export default function OptionDateRangeControl({ value, onChange, disabled = false }) {
  const { start, end, preset } = value;

  const handlePreset = (key) => {
    const range = computePresetRange(key);
    onChange({ ...range, preset: key });
  };

  const handleStartChange = (e) => {
    onChange({ start: e.target.value, end, preset: null });
  };

  const handleEndChange = (e) => {
    onChange({ start, end: e.target.value, preset: null });
  };

  const showWarning = rangeExceedsOneYear(start, end);

  return (
    <fieldset
      className={styles.root}
      data-testid="option-date-range-control"
      disabled={disabled}
    >
      <PillToggle
        options={PILL_OPTIONS}
        value={preset ?? ''}
        onChange={handlePreset}
        ariaLabel="Date range presets"
      />

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

      {showWarning && (
        <span className={styles.warning} role="status" data-testid="range-warning">
          Range exceeds 1 year — large requests may be slow
        </span>
      )}
    </fieldset>
  );
}
