import styles from './RiskFreeRateInput.module.css';

function RiskFreeRateInput({
  valuePct,
  onChange,
  ariaLabel,
  label,
  className,
}) {
  const cls = className ? `${styles.control} ${className}` : styles.control;
  return (
    <label className={cls}>
      {label && <span className={styles.label}>{label}</span>}
      <input
        type="number"
        step="0.01"
        min="0"
        value={valuePct}
        onChange={onChange}
        className={styles.input}
        aria-label={ariaLabel}
      />
      <span className={styles.unit}>%</span>
    </label>
  );
}

export default RiskFreeRateInput;
