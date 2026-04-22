import { useState, useMemo } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';
import ConfirmDialog from '../../components/ConfirmDialog';
import { nextInputId } from './storage';
import { isInputConfigured } from './blockShape';
import styles from './InputsPanel.module.css';

/**
 * Format an instrument value as a human-readable label for the trigger button.
 */
function instrumentLabel(instrument) {
  if (!instrument) return null;
  if (instrument.type === 'continuous' && instrument.collection) {
    return instrument.collection;
  }
  if (instrument.type === 'spot' && instrument.collection && instrument.instrument_id) {
    return `${instrument.collection} / ${instrument.instrument_id}`;
  }
  return null;
}

/**
 * Top-of-page Inputs panel (iter-4). Signals declare first-class named
 * price-series inputs here; blocks and operands reference them by id.
 *
 * Collapsible:
 *   - Expanded by default if ``inputs`` is empty (user must declare at
 *     least one before anything else becomes meaningful).
 *   - Auto-expanded state is kept even if user manually collapses: only
 *     the initial `open` default keys off emptiness.
 *
 * Each row renders:
 *   [ id input | instrument button | × ]
 *
 * Props:
 *   inputs    {Array}     [{id, instrument}]
 *   onChange  {Function}  (nextInputs) => void
 */
function InputsPanel({ inputs, onChange }) {
  const list = Array.isArray(inputs) ? inputs : [];
  const [open, setOpen] = useState(list.length === 0);
  const [pendingDeleteIdx, setPendingDeleteIdx] = useState(null);
  // Index of the input row whose picker modal is open (null = closed).
  const [pickerIdx, setPickerIdx] = useState(null);
  // Per-row text drafts for in-progress id edits. We keep the user's raw
  // keystrokes here so that empty or duplicate values are visible (and
  // flagged) rather than silently reverting the input.
  const [idDrafts, setIdDrafts] = useState({});

  const configuredCount = useMemo(
    () => list.filter(isInputConfigured).length,
    [list],
  );

  function handleAdd() {
    const id = nextInputId(list);
    onChange([...list, { id, instrument: { type: 'spot', collection: '', instrument_id: '' } }]);
    setOpen(true);
  }

  function idDraftError(idx, draft) {
    const trimmed = draft.trim();
    if (!trimmed) return 'Input id cannot be empty';
    if (list.some((x, i) => i !== idx && x.id === trimmed)) {
      return `Another input already uses "${trimmed}"`;
    }
    return null;
  }

  function handleIdDraftChange(idx, rawId) {
    setIdDrafts((d) => ({ ...d, [idx]: rawId }));
    const trimmed = (rawId || '').trim();
    if (!trimmed) return;
    if (list.some((x, i) => i !== idx && x.id === trimmed)) return;
    onChange(list.map((x, i) => (i !== idx ? x : { ...x, id: trimmed })));
  }

  function handleIdDraftCommit(idx) {
    setIdDrafts((d) => {
      if (!(idx in d)) return d;
      const { [idx]: _discard, ...rest } = d;
      return rest;
    });
  }

  function handlePickerSelect(instrument) {
    if (pickerIdx !== null) {
      onChange(list.map((x, i) => (i !== pickerIdx ? x : { ...x, instrument })));
    }
    setPickerIdx(null);
  }

  function handleDelete(idx) {
    onChange(list.filter((_, i) => i !== idx));
    setPendingDeleteIdx(null);
  }

  return (
    <div className={styles.panel} data-testid="inputs-panel">
      <button
        type="button"
        className={styles.header}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="inputs-panel-body"
        data-testid="inputs-panel-toggle"
      >
        <span className={styles.chevron} aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className={styles.title}>Inputs</span>
        <span className={styles.count}>
          {configuredCount === list.length
            ? `${list.length}`
            : `${configuredCount}/${list.length}`}
        </span>
        {list.length === 0 && (
          <span className={styles.needsInput}>Add at least one</span>
        )}
      </button>
      {open && (
        <div id="inputs-panel-body" className={styles.body}>
          {list.length === 0 && (
            <div className={styles.empty}>
              No inputs yet. Add a named price series to reference from blocks
              and operands.
            </div>
          )}
          {list.map((input, idx) => {
            const ok = isInputConfigured(input);
            const label = instrumentLabel(input.instrument);
            return (
              <div
                key={idx}
                className={styles.row}
                data-testid={`input-row-${idx}`}
              >
                <span
                  className={`${styles.status} ${ok ? styles.statusOk : styles.statusWarn}`}
                  title={ok ? 'Input configured' : 'Input needs instrument'}
                  aria-hidden="true"
                />
                {(() => {
                  const draft = idDrafts[idx];
                  const displayed = draft !== undefined ? draft : (input.id || '');
                  const err = draft !== undefined ? idDraftError(idx, draft) : null;
                  return (
                    <input
                      type="text"
                      className={styles.idInput}
                      value={displayed}
                      onChange={(e) => handleIdDraftChange(idx, e.target.value)}
                      onBlur={() => handleIdDraftCommit(idx)}
                      aria-label={`Input ${idx + 1} id`}
                      aria-invalid={err ? 'true' : 'false'}
                      title={err || undefined}
                      data-testid={`input-id-${idx}`}
                      spellCheck={false}
                      maxLength={16}
                    />
                  );
                })()}
                <div className={styles.pickerCell}>
                  <button
                    type="button"
                    className={styles.pickBtn}
                    onClick={() => setPickerIdx(idx)}
                    aria-label={`Instrument for input ${input.id || idx + 1}`}
                    data-testid={`input-picker-${idx}`}
                  >
                    {label || 'Select instrument'}
                  </button>
                </div>
                <button
                  type="button"
                  className={styles.deleteBtn}
                  onClick={() => setPendingDeleteIdx(idx)}
                  title={`Remove input ${input.id || idx + 1}`}
                  aria-label={`Remove input ${input.id || idx + 1}`}
                  data-testid={`input-delete-${idx}`}
                >
                  ×
                </button>
              </div>
            );
          })}
          <button
            type="button"
            className={styles.addBtn}
            onClick={handleAdd}
            data-testid="inputs-add-btn"
          >
            + Add input
          </button>
        </div>
      )}
      <ConfirmDialog
        open={pendingDeleteIdx !== null}
        title="Delete input?"
        message={
          pendingDeleteIdx !== null && list[pendingDeleteIdx]
            ? `Remove input "${list[pendingDeleteIdx].id}"? Any block or operand referencing it will become unrunnable until you fix them.`
            : ''
        }
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => handleDelete(pendingDeleteIdx)}
        onCancel={() => setPendingDeleteIdx(null)}
      />
      <InstrumentPickerModal
        isOpen={pickerIdx !== null}
        onClose={() => setPickerIdx(null)}
        onSelect={handlePickerSelect}
        title="Select Instrument"
      />
    </div>
  );
}

export default InputsPanel;
