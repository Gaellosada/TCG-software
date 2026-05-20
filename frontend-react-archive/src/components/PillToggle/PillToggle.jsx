import styles from './PillToggle.module.css';

/**
 * Reusable pill-style toggle button group.
 *
 * @param {Array<{value: string, label: string}>} options
 * @param {string} value - currently selected value
 * @param {(value: string) => void} onChange
 * @param {string} [ariaLabel]
 * @param {string} [tooltip] - native tooltip shown on hover over the group
 */
export default function PillToggle({ options, value, onChange, ariaLabel, tooltip }) {
  return (
    <div className={styles.pillToggle} role="group" aria-label={ariaLabel} title={tooltip}>
      {options.map((opt) => (
        <button
          key={opt.value}
          className={`${styles.pillBtn} ${value === opt.value ? styles.pillActive : ''}`}
          type="button"
          onClick={() => onChange(opt.value)}
          aria-pressed={value === opt.value}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
