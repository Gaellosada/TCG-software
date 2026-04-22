import { useState, useRef, useEffect } from 'react';
import ConfirmDialog from '../../components/ConfirmDialog';
import { isInputConfigured } from './blockShape';
import styles from './Signals.module.css';

/**
 * Per-block controls (v4): for entries, input dropdown (references one
 * of the signal's declared inputs), signed weight input with % suffix
 * and direction badge; for exits, the input picker and weight column
 * are hidden (the operating input is derived from the target entry
 * picked elsewhere in the block editor). Delete-block button is gated
 * by ConfirmDialog.
 *
 * Props:
 *   block       {Object}   { id, [input_id, weight on entries,
 *                            target_entry_block_id on exits] }
 *   section     {string}   'entries' | 'exits'
 *   inputs      {Array}    the signal's declared inputs
 *   onChange    {Function} (nextBlock) => void
 *   onDelete    {Function} () => void
 *   blockIndex  {number}   1-based index shown in the label
 *   status      {string}   'ok' | 'warn' (optional)
 *   blockIdx    {number}   0-based index for data-testid (optional)
 *   derivedInputLabel {string?} exits only: a read-only display hint
 *     (e.g. "(input from target: X)") rendered in place of the picker.
 */
function BlockHeader({ block, section, inputs, onChange, onDelete, blockIndex, status, blockIdx, derivedInputLabel }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editing, setEditing] = useState(false);
  // Local draft for the weight input so the user can type freely before blur
  const [weightDraft, setWeightDraft] = useState(null);
  const nameRef = useRef(null);

  const isEntry = section === 'entries';

  const list = Array.isArray(inputs) ? inputs : [];
  const selectedId = typeof block.input_id === 'string' ? block.input_id : '';
  const resolved = list.find((i) => i && i.id === selectedId) || null;
  const resolvedConfigured = resolved ? isInputConfigured(resolved) : false;

  const displayName = block.name || `Block ${blockIndex}`;

  // Committed weight (number, clamped by storage / parent) used for badge
  const committedWeight = Number.isFinite(block.weight) ? block.weight : 0;

  useEffect(() => {
    if (editing && nameRef.current) {
      nameRef.current.focus();
      nameRef.current.select();
    }
  }, [editing]);

  // Sync weightDraft when block.weight changes from outside (e.g. load)
  useEffect(() => {
    setWeightDraft(null);
  }, [block.weight]);

  function commitName() {
    if (!nameRef.current) return;
    const trimmed = nameRef.current.value.trim();
    setEditing(false);
    if (!trimmed || trimmed === `Block ${blockIndex}`) {
      onChange({ ...block, name: '' });
    } else {
      onChange({ ...block, name: trimmed });
    }
  }

  function setInputId(id) {
    onChange({ ...block, input_id: id });
  }

  /**
   * Clamp weight to [-100, +100] on blur or on explicit commit.
   * Intermediate string (e.g. "-") is left in draft while editing.
   */
  function commitWeight(raw) {
    setWeightDraft(null);
    const n = raw === '' || raw === '-' ? 0 : parseFloat(raw);
    if (!Number.isFinite(n)) {
      onChange({ ...block, weight: 0 });
      return;
    }
    const clamped = Math.max(-100, Math.min(100, n));
    onChange({ ...block, weight: clamped });
  }

  const showUnconfiguredWarning = resolved && !resolvedConfigured;
  const showUnknownWarning = !!selectedId && !resolved;

  // Determine badge
  function badgeProps() {
    if (committedWeight > 0) {
      return { className: styles.badgeLong, text: 'long', ariaLabel: 'direction: long' };
    }
    if (committedWeight < 0) {
      return { className: styles.badgeShort, text: 'short', ariaLabel: 'direction: short' };
    }
    return { className: styles.badgeNeutral, text: '—', ariaLabel: 'direction: neutral' };
  }

  const badge = isEntry ? badgeProps() : null;

  // Display value in the weight input: use the draft string if mid-edit,
  // otherwise the committed numeric value
  const weightDisplayValue = weightDraft !== null
    ? weightDraft
    : committedWeight;

  return (
    <div className={styles.blockHeaderRow} data-testid={`block-header-${blockIndex - 1}`}>
      {status && (
        <span
          className={`${styles.blockStatusDot} ${status === 'ok' ? styles.blockStatusDotOk : styles.blockStatusDotWarn}`}
          title={status === 'ok' ? 'Block ready' : 'Block not yet runnable (pick input + at least one complete condition)'}
          data-testid={`block-status-${blockIdx}`}
          data-runnable={status === 'ok' ? 'true' : 'false'}
          aria-hidden="true"
        />
      )}
      {editing ? (
        <input
          ref={nameRef}
          type="text"
          className={styles.blockNameInput}
          defaultValue={displayName}
          onBlur={commitName}
          onKeyDown={(e) => { if (e.key === 'Enter') commitName(); if (e.key === 'Escape') setEditing(false); }}
          maxLength={32}
          data-testid={`block-name-input-${blockIndex - 1}`}
        />
      ) : (
        <>
          <span className={styles.blockLabel}>{displayName}</span>
          <button
            type="button"
            className={styles.blockEditNameBtn}
            onClick={() => setEditing(true)}
            title="Rename block"
            aria-label={`Rename block ${blockIndex}`}
            data-testid={`block-edit-name-${blockIndex - 1}`}
          >
            ✎
          </button>
        </>
      )}

      <span className={styles.blockDirectionLabel}>
        {isEntry ? 'entry on' : 'exit'}
      </span>
      {isEntry ? (
        <div className={styles.blockInstrumentCell}>
          <select
            className={styles.blockInputSelect}
            value={selectedId}
            onChange={(e) => setInputId(e.target.value)}
            aria-label={`Input for block ${blockIndex}`}
            data-testid={`block-input-select-${blockIndex - 1}`}
          >
            <option value="">…</option>
            {list.map((input) => {
              const ok = isInputConfigured(input);
              return (
                <option key={input.id} value={input.id}>
                  {input.id}{!ok ? ' (needs instrument)' : ''}
                </option>
              );
            })}
          </select>
          {showUnconfiguredWarning && (
            <span className={styles.blockInputWarn} title="This input has no instrument yet">!</span>
          )}
          {showUnknownWarning && (
            <span className={styles.blockInputWarn} title={`Unknown input id "${selectedId}"`}>?</span>
          )}
        </div>
      ) : (
        derivedInputLabel ? (
          <span
            className={styles.blockDerivedInputLabel}
            data-testid={`block-derived-input-${blockIndex - 1}`}
          >
            {derivedInputLabel}
          </span>
        ) : null
      )}

      {isEntry && (
        <div className={styles.blockWeightCell}>
          <label className={styles.conditionInlineLabel} htmlFor={`weight-${blockIndex}`}>weight</label>
          <div className={styles.weightInputWrap}>
            <input
              id={`weight-${blockIndex}`}
              type="number"
              step="1"
              min="-100"
              max="100"
              className={styles.weightInput}
              value={weightDisplayValue}
              onChange={(e) => setWeightDraft(e.target.value)}
              onBlur={(e) => commitWeight(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') commitWeight(e.target.value); }}
              aria-label={`Weight for block ${blockIndex}`}
              data-testid={`block-weight-${blockIndex - 1}`}
            />
            <span className={styles.weightSuffix} aria-hidden="true">%</span>
          </div>
          {badge && (
            <span
              className={badge.className}
              aria-label={badge.ariaLabel}
              data-testid={`block-badge-${blockIndex - 1}`}
            >
              {badge.text}
            </span>
          )}
        </div>
      )}

      <button
        type="button"
        className={styles.blockDeleteBtn}
        onClick={() => setConfirmDelete(true)}
        title={`Remove block ${blockIndex}`}
        aria-label={`Remove block ${blockIndex}`}
        data-testid={`remove-block-${blockIndex - 1}`}
      >
        ×
      </button>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete block?"
        message="This block and all of its conditions will be removed."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => { setConfirmDelete(false); onDelete(); }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}

export default BlockHeader;
