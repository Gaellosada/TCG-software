import { useState } from 'react';
import ConfirmDialog from './ConfirmDialog';
import styles from './LockToggle.module.css';

/**
 * Small padlock icon-button that toggles the locked state of a saved entity.
 *
 * Behaviour:
 *   - When UNLOCKED and clicked → calls onSetLocked(true) immediately (no dialog).
 *   - When LOCKED and clicked → opens a neutral ConfirmDialog asking the user
 *     to confirm the unlock. On confirm → calls onSetLocked(false). On cancel →
 *     closes dialog only.
 *
 * Props:
 *   locked        {boolean}   current lock state
 *   onSetLocked   {Function}  (nextBool) => void — caller wires to the API call
 *   entityLabel   {string}    human-readable noun, e.g. "indicator" / "signal" / "portfolio"
 *   disabled      {boolean}   greys out the button when true
 */
function LockToggle({ locked, onSetLocked, entityLabel = 'item', disabled = false }) {
  const [dialogOpen, setDialogOpen] = useState(false);

  function handleClick(e) {
    e.stopPropagation();
    if (disabled) return;
    if (locked) {
      // Unlocking requires confirmation.
      setDialogOpen(true);
    } else {
      // Locking is immediate.
      onSetLocked(true);
    }
  }

  function handleConfirmUnlock() {
    setDialogOpen(false);
    onSetLocked(false);
  }

  function handleCancelUnlock() {
    setDialogOpen(false);
  }

  const actionLabel = locked
    ? `Unlock ${entityLabel}`
    : `Lock ${entityLabel}`;

  // Padlock SVGs — closed (locked) and open (unlocked).
  // Simple inline SVG for zero-dependency icon rendering.
  const LockedIcon = () => (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      width="13"
      height="13"
      fill="currentColor"
      aria-hidden="true"
    >
      {/* closed padlock */}
      <path d="M11 6V4a3 3 0 0 0-6 0v2H4a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-1zm-5 0V4a2 2 0 1 1 4 0v2H6z" />
    </svg>
  );

  const UnlockedIcon = () => (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      width="13"
      height="13"
      fill="currentColor"
      aria-hidden="true"
    >
      {/* open padlock — shackle swings left */}
      <path d="M11 1a3 3 0 0 0-3 3v2H4a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1H9V4a2 2 0 1 1 4 0v1h1V4a3 3 0 0 0-3-3z" />
    </svg>
  );

  const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  return (
    <>
      <button
        type="button"
        className={`${styles.lockBtn} ${disabled ? styles.lockBtnDisabled : ''}`}
        onClick={handleClick}
        title={actionLabel}
        aria-label={actionLabel}
        disabled={disabled}
        data-testid="lock-toggle-btn"
        data-locked={locked ? 'true' : 'false'}
      >
        {locked ? <LockedIcon /> : <UnlockedIcon />}
      </button>

      <ConfirmDialog
        open={dialogOpen}
        title={`Unlock ${capitalize(entityLabel)}?`}
        message={`This ${entityLabel} will become editable again.`}
        confirmLabel="Unlock"
        cancelLabel="Cancel"
        destructive={false}
        onConfirm={handleConfirmUnlock}
        onCancel={handleCancelUnlock}
      />
    </>
  );
}

export default LockToggle;
