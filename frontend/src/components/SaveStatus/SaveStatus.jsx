/**
 * SaveStatus — tiny inline indicator for backend autosave state.
 *
 * Props:
 *   status   'idle' | 'saving' | 'saved' | 'error'
 *   label?   Optional prefix label (e.g. "Cloud:")
 *
 * Stays out of the way: returns nothing when status is ``'idle'`` so
 * the host UI doesn't reserve dead space.
 */
import styles from './SaveStatus.module.css';

const LABEL = {
  saving: 'saving…',
  saved: 'saved',
  error: 'save failed',
};

function SaveStatus({ status, label = 'Cloud' }) {
  if (status === 'idle') return null;
  const cls =
    status === 'error' ? styles.error
    : status === 'saved' ? styles.saved
    : styles.saving;
  return (
    <span
      className={`${styles.root} ${cls}`}
      data-testid="save-status"
      data-status={status}
      role="status"
      aria-live="polite"
    >
      {label}: {LABEL[status] || status}
    </span>
  );
}

export default SaveStatus;
