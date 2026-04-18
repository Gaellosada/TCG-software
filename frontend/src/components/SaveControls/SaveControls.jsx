import styles from './SaveControls.module.css';

/**
 * Compact header bar with a Save button + Auto save checkbox.
 * Shared across Portfolio and Indicators pages.
 *
 * Props:
 *   dirty            {boolean}  — there are unsaved changes
 *   autosave         {boolean}  — autosave checkbox state
 *   onSave           {Function} — () => void, invoked by the Save button
 *   onToggleAutosave {Function} — (bool) => void, invoked when checkbox flips
 *   savedAtLabel     {string=}  — optional "saved 3s ago" style text; shown when !dirty
 *   saveDisabled     {boolean=} — force the Save button disabled
 *   className        {string=}  — extra class on the outer container
 *   leftSlot         {ReactNode=} — optional content rendered BEFORE the Save
 *                                   button (e.g. an inline name input).
 *                                   Styled to `flex: 1` so the slot occupies
 *                                   the remaining row width.
 */
function SaveControls({
  dirty,
  autosave,
  onSave,
  onToggleAutosave,
  savedAtLabel,
  saveDisabled = false,
  className,
  leftSlot,
}) {
  const rootClass = className ? `${styles.root} ${className}` : styles.root;
  const disabled = !dirty || saveDisabled;
  return (
    <div className={rootClass} data-testid="save-controls">
      {leftSlot !== undefined && (
        <div className={styles.leftSlot}>{leftSlot}</div>
      )}
      <button
        type="button"
        className={styles.saveBtn}
        onClick={onSave}
        disabled={disabled}
        aria-label="Save"
      >
        Save
      </button>
      <label className={styles.autosaveLabel}>
        <input
          type="checkbox"
          checked={!!autosave}
          onChange={(e) => onToggleAutosave(e.target.checked)}
          aria-label="Auto save"
        />
        Auto save
      </label>
      {!dirty && savedAtLabel && (
        <span className={styles.savedAt} aria-live="polite">{savedAtLabel}</span>
      )}
      {dirty && !autosave && (
        <span className={styles.unsaved} aria-live="polite">Unsaved changes</span>
      )}
    </div>
  );
}

export default SaveControls;
