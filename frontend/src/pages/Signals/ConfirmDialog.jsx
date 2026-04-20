import { useEffect, useRef } from 'react';
import styles from './ConfirmDialog.module.css';

/**
 * Reusable modal confirmation dialog. NO external lib — plain React.
 *
 * Behaviour:
 *   - Auto-focuses the confirm button on open.
 *   - Escape ⇒ cancel.
 *   - Enter  ⇒ confirm.
 *   - Backdrop click ⇒ cancel.
 *   - Focus trap: Tab / Shift-Tab wraps between confirm and cancel.
 *
 * Props:
 *   open          {boolean}   render-gate
 *   title         {string}
 *   message       {string|ReactNode}
 *   confirmLabel  {string}    default 'Confirm'
 *   cancelLabel   {string}    default 'Cancel'
 *   destructive   {boolean}   styles the confirm button as red
 *   onConfirm     {Function}
 *   onCancel      {Function}
 */
function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  onConfirm,
  onCancel,
}) {
  const confirmRef = useRef(null);
  const cancelRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    // Defer to next tick so the DOM node is mounted before focus.
    const t = setTimeout(() => {
      if (confirmRef.current) confirmRef.current.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    function onKey(e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (onCancel) onCancel();
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        if (onConfirm) onConfirm();
        return;
      }
      if (e.key === 'Tab') {
        // Focus trap between confirm + cancel.
        const targets = [cancelRef.current, confirmRef.current].filter(Boolean);
        if (targets.length < 2) return;
        const active = document.activeElement;
        if (e.shiftKey) {
          if (active === targets[0]) {
            e.preventDefault();
            targets[1].focus();
          }
        } else if (active === targets[1]) {
          e.preventDefault();
          targets[0].focus();
        }
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onCancel, onConfirm]);

  if (!open) return null;

  return (
    <div
      className={styles.backdrop}
      onMouseDown={(e) => {
        // Clicking the backdrop (not a bubbled child) cancels.
        if (e.target === e.currentTarget && onCancel) onCancel();
      }}
      data-testid="confirm-dialog-backdrop"
    >
      <div
        className={styles.card}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        data-testid="confirm-dialog"
      >
        {title && (
          <h3 id="confirm-dialog-title" className={styles.title}>{title}</h3>
        )}
        {message && <div className={styles.message}>{message}</div>}
        <div className={styles.actions}>
          <button
            ref={cancelRef}
            type="button"
            className={styles.cancelBtn}
            onClick={onCancel}
            data-testid="confirm-dialog-cancel"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`${styles.confirmBtn} ${destructive ? styles.confirmBtnDestructive : ''}`}
            onClick={onConfirm}
            data-testid="confirm-dialog-confirm"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConfirmDialog;
