import { useState } from 'react';
import ConfirmDialog from '../../components/ConfirmDialog';
import { isInputConfigured } from './blockShape';
import styles from './Signals.module.css';

/**
 * Per-block controls (v3 / iter-4): input dropdown (references one of
 * the signal's declared inputs), weight input (entry tabs only —
 * hidden on exit tabs), delete-block button gated by ConfirmDialog.
 *
 * No more inline SeriesPicker popover — the user picks instruments once
 * in the InputsPanel at the top of the page, then references them here.
 *
 * Props:
 *   block       {Object}   { input_id, weight, conditions }
 *   direction   {string}   long_entry | long_exit | short_entry | short_exit
 *   inputs      {Array}    the signal's declared inputs
 *   onChange    {Function} (nextBlock) => void
 *   onDelete    {Function} () => void
 *   blockIndex  {number}   1-based index shown in the label
 */
function BlockHeader({ block, direction, inputs, onChange, onDelete, blockIndex }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isEntry = direction === 'long_entry' || direction === 'short_entry';

  const list = Array.isArray(inputs) ? inputs : [];
  const selectedId = typeof block.input_id === 'string' ? block.input_id : '';
  const resolved = list.find((i) => i && i.id === selectedId) || null;
  const resolvedConfigured = resolved ? isInputConfigured(resolved) : false;

  function setInputId(id) {
    onChange({ ...block, input_id: id });
  }

  function setWeight(raw) {
    const n = raw === '' ? 0 : parseFloat(raw);
    if (!Number.isFinite(n) || n < 0) return;
    onChange({ ...block, weight: n });
  }

  const showUnconfiguredWarning = resolved && !resolvedConfigured;
  const showUnknownWarning = !!selectedId && !resolved;

  return (
    <div className={styles.blockHeaderRow} data-testid={`block-header-${blockIndex - 1}`}>
      <span className={styles.blockLabel}>Block {blockIndex}</span>

      <div className={styles.blockInstrumentCell}>
        <select
          className={styles.blockInputSelect}
          value={selectedId}
          onChange={(e) => setInputId(e.target.value)}
          aria-label={`Input for block ${blockIndex}`}
          data-testid={`block-input-select-${blockIndex - 1}`}
        >
          <option value="">Pick input…</option>
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

      {isEntry && (
        <div className={styles.blockWeightCell}>
          <label className={styles.conditionInlineLabel} htmlFor={`weight-${blockIndex}`}>weight</label>
          <input
            id={`weight-${blockIndex}`}
            type="number"
            step="0.1"
            min="0"
            max="1"
            className={styles.blockWeightInput}
            value={Number.isFinite(block.weight) ? block.weight : 0}
            onChange={(e) => setWeight(e.target.value)}
            aria-label={`Weight for block ${blockIndex}`}
            data-testid={`block-weight-${blockIndex - 1}`}
          />
          <span className={styles.blockWeightSuffix} aria-hidden="true">×</span>
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
