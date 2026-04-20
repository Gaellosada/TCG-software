import { useState } from 'react';
import SeriesPicker from '../Indicators/SeriesPicker';
import ConfirmDialog from '../../components/ConfirmDialog';
import styles from './Signals.module.css';

/**
 * Per-block controls: instrument picker (reuses Indicators SeriesPicker),
 * weight input (entry tabs only — hidden on exit tabs), delete-block button
 * gated by a ConfirmDialog.
 *
 * Props:
 *   block       {Object}   { instrument, weight, conditions }
 *   direction   {string}   long_entry | long_exit | short_entry | short_exit
 *   onChange    {Function} (nextBlock) => void
 *   onDelete    {Function} () => void
 *   blockIndex  {number}   1-based index shown in the label
 */
function BlockHeader({ block, direction, onChange, onDelete, blockIndex }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [instPickerOpen, setInstPickerOpen] = useState(false);

  const isEntry = direction === 'long_entry' || direction === 'short_entry';
  const instrument = block.instrument;
  const instSummary = instrument
    ? `${instrument.collection}:${instrument.instrument_id}`
    : 'Pick instrument…';

  function setInstrument(entry) {
    onChange({
      ...block,
      instrument: { collection: entry.collection, instrument_id: entry.instrument_id },
    });
    setInstPickerOpen(false);
  }

  function setWeight(raw) {
    const n = raw === '' ? 0 : parseFloat(raw);
    if (!Number.isFinite(n) || n < 0) return;
    onChange({ ...block, weight: n });
  }

  return (
    <div className={styles.blockHeaderRow} data-testid={`block-header-${blockIndex - 1}`}>
      <span className={styles.blockLabel}>Block {blockIndex}</span>

      <div className={styles.blockInstrumentCell}>
        <button
          type="button"
          className={`${styles.blockInstrumentBtn} ${!instrument ? styles.blockInstrumentBtnEmpty : ''}`}
          onClick={() => setInstPickerOpen((v) => !v)}
          aria-label="Pick instrument for block"
          data-testid={`block-instrument-btn-${blockIndex - 1}`}
        >
          {instSummary}
        </button>
        {instPickerOpen && (
          <div className={styles.blockInstrumentPopover} data-testid="block-instrument-popover">
            <SeriesPicker
              value={instrument ? { collection: instrument.collection, instrument_id: instrument.instrument_id } : null}
              onSave={setInstrument}
              onCancel={() => setInstPickerOpen(false)}
              defaultCollection={null}
              saveLabel="Use"
            />
          </div>
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
