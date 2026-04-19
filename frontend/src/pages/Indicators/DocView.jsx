import { useState, useEffect, useRef } from 'react';
import Markdown from 'react-markdown';
import styles from './DocView.module.css';

/**
 * Renders indicator documentation. Read mode renders ``value`` as
 * markdown via ``react-markdown``; edit mode swaps to a textarea. An
 * explicit "Edit" button (top-right) gates the transition — it is
 * only rendered when ``!readOnly``.
 *
 * Commit semantics:
 *   - Blur on the textarea ⇒ ``onChange(text)`` and swap back to read.
 *   - Escape ⇒ abandon draft, swap back to read (no ``onChange``).
 *   - The parent owns ``value``; this component only surfaces edits.
 *
 * Read-only treatment (default indicators): no Edit button, cursor
 * stays ``default``, dim overlay + ``Read-only`` badge — same pattern
 * as ``CodeEditor``. The ``data-readonly`` attribute on the wrapper
 * matches what the code editor uses so tests / integration hooks can
 * target both panels uniformly.
 *
 * Props:
 *   value       {string}   — markdown source
 *   onChange    {(string) => void}
 *   readOnly    {boolean}
 *   placeholder {string}   — optional. Defaults based on ``readOnly``.
 */
function DocView({ value, onChange, readOnly, placeholder }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(typeof value === 'string' ? value : '');
  // Track whether the last unmount was a cancel — avoids committing the
  // draft on Escape-induced blur. Escape sets this flag right before
  // toggling editing off, and blur consults it.
  const cancelingRef = useRef(false);
  const textareaRef = useRef(null);

  // Sync local draft with the authoritative value whenever the
  // indicator or the externally-provided doc changes. We only
  // overwrite when NOT editing so a user's in-progress edits are not
  // stomped.
  useEffect(() => {
    if (!editing) {
      setDraft(typeof value === 'string' ? value : '');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  // Defensive: if ``readOnly`` flips on while the user is editing
  // (e.g. user selected a default indicator mid-edit in the code tab
  // — shouldn't happen via the current UI, but cheap to guard), drop
  // out of edit mode without committing.
  useEffect(() => {
    if (readOnly && editing) {
      cancelingRef.current = true;
      setEditing(false);
    }
  }, [readOnly, editing]);

  // Focus the textarea on entering edit mode.
  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [editing]);

  const hasValue = typeof value === 'string' && value.length > 0;
  const effectivePlaceholder = placeholder
    || (readOnly ? 'No documentation provided.' : 'No documentation yet. Click Edit to add some.');

  const wrapperClass = readOnly ? `${styles.wrapper} ${styles.readonly}` : styles.wrapper;

  function handleEditClick() {
    if (readOnly) return;
    setDraft(typeof value === 'string' ? value : '');
    cancelingRef.current = false;
    setEditing(true);
  }

  function handleBlur() {
    if (cancelingRef.current) {
      cancelingRef.current = false;
      setEditing(false);
      return;
    }
    // Commit on blur.
    if (onChange && draft !== value) {
      onChange(draft);
    }
    setEditing(false);
  }

  function handleKeyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelingRef.current = true;
      // Reset the draft back to the canonical value so if the user
      // re-enters edit mode immediately they don't see stale text.
      setDraft(typeof value === 'string' ? value : '');
      // Blur triggers the cancel branch in handleBlur.
      if (textareaRef.current) {
        textareaRef.current.blur();
      } else {
        setEditing(false);
      }
    }
  }

  return (
    <div className={wrapperClass} data-readonly={readOnly ? 'true' : 'false'}>
      {!readOnly && !editing && (
        <button
          type="button"
          className={styles.editButton}
          onClick={handleEditClick}
          aria-label="Edit documentation"
        >
          Edit
        </button>
      )}

      {editing ? (
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={handleBlur}
          onKeyDown={handleKeyDown}
          aria-label="Indicator documentation"
          spellCheck={false}
          placeholder="Write markdown here…"
        />
      ) : hasValue ? (
        <div className={styles.markdown}>
          <Markdown>{value}</Markdown>
        </div>
      ) : (
        <div className={styles.placeholder} data-testid="docview-placeholder">
          {effectivePlaceholder}
        </div>
      )}

      {readOnly && (
        <>
          <div className={styles.readonlyOverlay} aria-hidden="true" />
          <span className={styles.readonlyBadge}>Read-only</span>
        </>
      )}
    </div>
  );
}

export default DocView;
