import { useState, useMemo } from 'react';
import InstrumentPicker from '../../components/InstrumentPicker/InstrumentPicker';
import ConfirmDialog from '../../components/ConfirmDialog';
import { nextInputId } from './storage';
import { isInputConfigured } from './blockShape';
import styles from './InputsPanel.module.css';

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
 *   [ id input | InstrumentPicker | × ]
 *
 * Props:
 *   inputs    {Array}     [{id, instrument}]
 *   onChange  {Function}  (nextInputs) => void
 */
function InputsPanel({ inputs, onChange }) {
  const list = Array.isArray(inputs) ? inputs : [];
  const [open, setOpen] = useState(list.length === 0);
  const [pendingDeleteIdx, setPendingDeleteIdx] = useState(null);

  const configuredCount = useMemo(
    () => list.filter(isInputConfigured).length,
    [list],
  );

  function handleAdd() {
    const id = nextInputId(list);
    // Default to an unset spot instrument — user must pick collection +
    // instrument_id. Until that happens, ``isInputConfigured`` is false
    // and the input doesn't flow into the Run gate.
    onChange([...list, { id, instrument: { type: 'spot', collection: '', instrument_id: '' } }]);
    setOpen(true);
  }

  function handleRenameId(idx, rawId) {
    const trimmed = (rawId || '').trim();
    if (!trimmed) return;
    // Reject duplicate ids (case-sensitive to match backend).
    if (list.some((x, i) => i !== idx && x.id === trimmed)) return;
    onChange(list.map((x, i) => (i !== idx ? x : { ...x, id: trimmed })));
  }

  function handleInstrumentChange(idx, nextInstrument) {
    onChange(list.map((x, i) => (i !== idx ? x : { ...x, instrument: nextInstrument })));
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
            return (
              <div
                key={`${idx}-${input.id}`}
                className={styles.row}
                data-testid={`input-row-${idx}`}
              >
                <span
                  className={`${styles.status} ${ok ? styles.statusOk : styles.statusWarn}`}
                  title={ok ? 'Input configured' : 'Input needs instrument'}
                  aria-hidden="true"
                />
                <input
                  type="text"
                  className={styles.idInput}
                  value={input.id || ''}
                  onChange={(e) => handleRenameId(idx, e.target.value)}
                  aria-label={`Input ${idx + 1} id`}
                  data-testid={`input-id-${idx}`}
                  spellCheck={false}
                  maxLength={16}
                />
                <div className={styles.pickerCell}>
                  <InstrumentPicker
                    value={input.instrument}
                    onChange={(next) => handleInstrumentChange(idx, next)}
                    ariaLabel={`Instrument for input ${input.id || idx + 1}`}
                    testId={`input-picker-${idx}`}
                  />
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
    </div>
  );
}

export default InputsPanel;
