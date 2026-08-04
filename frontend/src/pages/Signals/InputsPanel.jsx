import { useState, useMemo } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';
import ConfirmDialog from '../../components/ConfirmDialog';
import { nextInputId } from './storage';
import { isInputConfigured } from './blockShape';
import styles from './InputsPanel.module.css';

/**
 * One-line summary of a single basket leg's inner instrument. Shared
 * between the inline-basket label and any other site that wants a
 * short per-leg tag.
 */
function basketLegLabel(leg) {
  const inst = leg && leg.instrument;
  if (!inst) return '?';
  if (inst.type === 'spot') return inst.instrument_id || inst.collection || '?';
  if (inst.type === 'continuous') return inst.collection || '?';
  if (inst.type === 'option_stream') {
    const parts = [inst.collection, inst.option_type].filter(Boolean);
    return parts.length > 0 ? parts.join('·') : '?';
  }
  return '?';
}

// Default cap enabled by the "+ Net cap" toggle: long-or-flat (fraction
// units, 1.0 == 100% of capital). This is the canonical fix for the
// OR-of-blocks doubling problem for a LONG leg — two +100% OR-branches that
// would otherwise net +200% are clamped back to +100%. The user adjusts the
// bounds (e.g. a short leg → low -100, high 0) after enabling.
const DEFAULT_CAP = [0, 1];

/**
 * Format a stored cap bound (wire units — a FRACTION, 1.0 == 100% of
 * capital) as a percent string for the cap fields. Rounds away binary-float
 * noise so a fraction like 0.1 renders "10", not "10.000000000000002".
 * Non-finite → '' (defensive; the control only renders finite bounds).
 */
function fracToPctStr(frac) {
  if (typeof frac !== 'number' || !Number.isFinite(frac)) return '';
  return String(Number((frac * 100).toFixed(6)));
}

/**
 * Parse a cap field's raw percent text → a FRACTION (÷100), or ``null`` when
 * the text is not yet a finite number (empty / lone sign / lone dot — states
 * a user passes through mid-typing). ``Number()`` (not ``parseFloat``) so
 * "12abc" is rejected outright rather than silently truncated to 12.
 */
function pctTextToFrac(raw) {
  const t = (raw ?? '').trim();
  if (t === '' || t === '-' || t === '+' || t === '.' || t === '-.' || t === '+.') {
    return null;
  }
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  return n / 100;
}

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
  if (instrument.type === 'option_stream' && instrument.collection) {
    const parts = [instrument.collection, instrument.option_type].filter(Boolean);
    return parts.join(' · ');
  }
  if (instrument.type === 'basket') {
    if (instrument.kind === 'saved' && instrument.basket_id) {
      return `Basket: ${instrument.basket_id}`;
    }
    if (
      instrument.kind === 'inline' &&
      Array.isArray(instrument.legs) &&
      instrument.legs.length > 0
    ) {
      return `Basket: ${instrument.legs.map(basketLegLabel).join(', ')}`;
    }
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
 *   readOnly  {boolean}   when true the panel is VIEW-only: the expand/collapse
 *                         toggle and the instrument-picker open trigger still
 *                         work (so the user can inspect ids + instruments), but
 *                         every EDIT control (id text, add, delete, and picking
 *                         a new instrument) is disabled.
 */
function InputsPanel({ inputs, onChange, readOnly = false }) {
  const list = Array.isArray(inputs) ? inputs : [];
  const [open, setOpen] = useState(list.length === 0);
  const [pendingDeleteIdx, setPendingDeleteIdx] = useState(null);
  // Index of the input row whose picker modal is open (null = closed).
  const [pickerIdx, setPickerIdx] = useState(null);
  // Per-row text drafts for in-progress id edits. We keep the user's raw
  // keystrokes here so that empty or duplicate values are visible (and
  // flagged) rather than silently reverting the input.
  const [idDrafts, setIdDrafts] = useState({});
  // Per-field text drafts for in-progress cap edits, keyed `${idx}:low|high`.
  // Same rationale as idDrafts: keep the user's raw keystrokes visible (incl.
  // a partial "-" or "") without committing a NaN bound into state.
  const [capDrafts, setCapDrafts] = useState({});

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

  // --- per-input net-position cap (OR-branch clamp) ---------------------

  // Enable the cap with the long-or-flat default; the user then adjusts the
  // bounds. Spread preserves id + instrument.
  function handleCapEnable(idx) {
    onChange(list.map((x, i) => (i !== idx ? x : { ...x, position_cap: [...DEFAULT_CAP] })));
  }

  // Remove the cap entirely — drops the key so the wire/persist payload is
  // byte-identical to a never-capped input (no stray position_cap).
  function handleCapClear(idx) {
    setCapDrafts((d) => {
      const rest = { ...d };
      delete rest[`${idx}:low`];
      delete rest[`${idx}:high`];
      return rest;
    });
    onChange(list.map((x, i) => {
      if (i !== idx) return x;
      const { position_cap: _drop, ...rest } = x;
      return rest;
    }));
  }

  function handleCapDraftChange(idx, bound, raw) {
    setCapDrafts((d) => ({ ...d, [`${idx}:${bound}`]: raw }));
    const frac = pctTextToFrac(raw);
    if (frac === null) return; // mid-typing (empty / lone sign) — keep draft, don't commit NaN
    onChange(list.map((x, i) => {
      if (i !== idx) return x;
      const cur = Array.isArray(x.position_cap) && x.position_cap.length === 2
        ? x.position_cap
        : [...DEFAULT_CAP];
      const next = bound === 'low' ? [frac, cur[1]] : [cur[0], frac];
      return { ...x, position_cap: next };
    }));
  }

  function handleCapDraftCommit(idx, bound) {
    setCapDrafts((d) => {
      const key = `${idx}:${bound}`;
      if (!(key in d)) return d;
      const { [key]: _discard, ...rest } = d;
      return rest;
    });
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
            const instType = input.instrument && input.instrument.type;
            // Only future/option configs have a settings screen worth
            // re-opening. Spot/index chips have none (no config popup to
            // show) and stay a static label once picked — no click-to-edit.
            const isEditableType = instType === 'continuous' || instType === 'option_stream';
            const staticChip = ok && instType === 'spot';
            // A configured future/option chip stays open-able even when the
            // signal is LOCKED — it opens the shared picker in its readOnly
            // (view-only) mode rather than being disabled outright, so a
            // locked signal's settings are still inspectable. Every other
            // case (not yet configured, or a non-editable configured type)
            // keeps the prior disabled-under-lock behaviour.
            const pickDisabled = readOnly && !(ok && isEditableType);
            const pickTitle = readOnly
              ? ((ok && isEditableType) ? 'View settings' : (label || 'No instrument'))
              : ((ok && isEditableType) ? 'Edit settings' : undefined);
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
                      readOnly={readOnly}
                    />
                  );
                })()}
                <div className={styles.pickerCell}>
                  {/* Click-to-edit: a configured future/option chip re-opens
                      the picker pre-filled with its stored config (settings
                      only — see initialConfig on the modal below). Locked
                      signals still open it, but read-only (view + navigate,
                      no commit). Spot/index chips have no settings screen,
                      so they never attach a click handler (staticChip). */}
                  <button
                    type="button"
                    className={`${styles.pickBtn} ${staticChip ? styles.pickBtnStatic : ''}`}
                    onClick={staticChip ? undefined : () => setPickerIdx(idx)}
                    aria-label={`Instrument for input ${input.id || idx + 1}`}
                    data-testid={`input-picker-${idx}`}
                    disabled={pickDisabled}
                    title={pickTitle}
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
                  disabled={readOnly}
                >
                  ×
                </button>
                {(() => {
                  // Net-position cap (fraction units in state; edited in %).
                  // Clamps the SUM of every latched entry block bound to this
                  // input each bar, so OR-branches don't stack beyond the
                  // intended weight (engine: signal_exec.py np.clip). When
                  // unset the key is absent → byte-identical uncapped path.
                  const cap = Array.isArray(input.position_cap)
                    && input.position_cap.length === 2
                    ? input.position_cap
                    : null;
                  const capInvalid = cap !== null && Number(cap[0]) > Number(cap[1]);
                  const invalidTitle = capInvalid
                    ? 'Low must be ≤ high, otherwise the cap is dropped and the position is left uncapped'
                    : undefined;
                  if (cap === null) {
                    // Locked + no cap → nothing to show or inspect.
                    if (readOnly) return null;
                    return (
                      <div className={styles.capRow}>
                        <button
                          type="button"
                          className={styles.capToggle}
                          onClick={() => handleCapEnable(idx)}
                          data-testid={`input-cap-toggle-${idx}`}
                          title="Cap this input's net position so OR-branches don't stack beyond the intended weight (two +100% OR-branches would otherwise net +200%)."
                        >
                          + Net cap
                        </button>
                      </div>
                    );
                  }
                  return (
                    <div className={styles.capRow}>
                      <span
                        className={styles.capLabel}
                        title="Net-position cap (% of capital). Clamps the sum of every latched entry block bound to this input each bar, so OR-branches don't stack beyond the intended weight. Long-or-flat leg: low 0, high 100. Short-or-flat: low -100, high 0."
                      >
                        Net cap %
                      </span>
                      <input
                        type="text"
                        inputMode="numeric"
                        className={styles.capField}
                        value={
                          capDrafts[`${idx}:low`] !== undefined
                            ? capDrafts[`${idx}:low`]
                            : fracToPctStr(cap[0])
                        }
                        onChange={(e) => handleCapDraftChange(idx, 'low', e.target.value)}
                        onBlur={() => handleCapDraftCommit(idx, 'low')}
                        aria-label={`Input ${input.id || idx + 1} net position cap low percent`}
                        aria-invalid={capInvalid ? 'true' : 'false'}
                        title={invalidTitle}
                        data-testid={`input-cap-low-${idx}`}
                        readOnly={readOnly}
                        spellCheck={false}
                      />
                      <span className={styles.capTo} aria-hidden="true">to</span>
                      <input
                        type="text"
                        inputMode="numeric"
                        className={styles.capField}
                        value={
                          capDrafts[`${idx}:high`] !== undefined
                            ? capDrafts[`${idx}:high`]
                            : fracToPctStr(cap[1])
                        }
                        onChange={(e) => handleCapDraftChange(idx, 'high', e.target.value)}
                        onBlur={() => handleCapDraftCommit(idx, 'high')}
                        aria-label={`Input ${input.id || idx + 1} net position cap high percent`}
                        aria-invalid={capInvalid ? 'true' : 'false'}
                        title={invalidTitle}
                        data-testid={`input-cap-high-${idx}`}
                        readOnly={readOnly}
                        spellCheck={false}
                      />
                      {!readOnly && (
                        <button
                          type="button"
                          className={styles.capClear}
                          onClick={() => handleCapClear(idx)}
                          data-testid={`input-cap-clear-${idx}`}
                          aria-label={`Remove net position cap on input ${input.id || idx + 1}`}
                          title="Remove cap (net position left uncapped)"
                        >
                          clear cap
                        </button>
                      )}
                    </div>
                  );
                })()}
              </div>
            );
          })}
          <button
            type="button"
            className={styles.addBtn}
            onClick={handleAdd}
            data-testid="inputs-add-btn"
            disabled={readOnly}
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
        allowBaskets={true}
        // Signals inputs are backtested → surface the option-stream select-and-hold
        // (fixed-contract dollar P&L) + nav_times controls in the direct options
        // drill-down.  (Not on the basket-leg sub-picker: the backend rejects
        // hold_between_rolls on a basket leg.)
        showOptionHoldControls={true}
        // Edit mode: pre-fill from the raw stored config of the row being
        // edited (the exact object onSelect gave us — no reshaping). null
        // for an unconfigured row falls back to create mode, unchanged.
        initialConfig={pickerIdx !== null ? list[pickerIdx].instrument : null}
        // Locked signal → view-only (onSelect never fires inside the modal).
        readOnly={readOnly}
      />
    </div>
  );
}

export default InputsPanel;
