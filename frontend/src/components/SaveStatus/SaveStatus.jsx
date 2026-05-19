/**
 * SaveStatus — tiny inline indicator for backend autosave state.
 *
 * Props:
 *   status         'idle' | 'saving' | 'saved' | 'error'
 *   label?         Optional prefix label (e.g. "Cloud:")
 *   errorMessage?  Optional detailed error message — when status is
 *                  ``'error'``, this is exposed as the native title
 *                  tooltip and a screen-reader-accessible description.
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

function SaveStatus({ status, label = 'Cloud', errorMessage = null }) {
  if (status === 'idle') return null;
  const cls =
    status === 'error' ? styles.error
    : status === 'saved' ? styles.saved
    : styles.saving;
  const title = status === 'error' && errorMessage ? errorMessage : undefined;
  return (
    <span
      className={`${styles.root} ${cls}`}
      data-testid="save-status"
      data-status={status}
      data-error-message={status === 'error' && errorMessage ? errorMessage : undefined}
      role="status"
      aria-live="polite"
      title={title}
    >
      {label}: {LABEL[status] || status}
      {status === 'error' && errorMessage ? (
        <span className={styles.errorDetail} data-testid="save-status-error-detail">
          {' '}— {errorMessage}
        </span>
      ) : null}
    </span>
  );
}

export default SaveStatus;
