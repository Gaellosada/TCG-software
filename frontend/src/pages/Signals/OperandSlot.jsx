import { useEffect, useRef, useState } from 'react';
import IndicatorParamsOverride from './IndicatorParamsOverride';
import ConfirmDialog from '../../components/ConfirmDialog';
import { defaultIndicatorOperand, defaultInstrumentOperand } from './conditionOps';
import { isInputConfigured } from './blockShape';
import styles from './Signals.module.css';

/**
 * One operand lives in one slot (v3 / iter-4). States:
 *   - empty  (operand == null): renders a ``+`` button that opens a popover
 *             menu {Indicator, Instrument, Constant}. Picking installs the
 *             default operand for that kind.
 *   - filled: renders the appropriate inline editor + a small ``×`` button
 *             that opens a ConfirmDialog and, on confirm, resets to null.
 *
 * v3 operand shapes:
 *   indicator:   { kind:'indicator', indicator_id, input_id, output,
 *                  params_override, series_override: {label: input_id} }
 *   instrument:  { kind:'instrument', input_id, field }
 *   constant:    { kind:'constant', value }
 *
 * Props:
 *   operand      {Object|null}
 *   onChange     {Function}  (nextOperand | null) => void
 *   indicators   {Array}     available indicator specs (for indicator kind)
 *   inputs       {Array}     the signal's declared inputs (for input-id refs)
 *   slotLabel    {string?}   aria hint, shown in the confirm dialog
 */
function OperandSlot({ operand, onChange, indicators, inputs, slotLabel }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const rootRef = useRef(null);

  // Close the add-menu when clicking outside.
  useEffect(() => {
    if (!menuOpen) return undefined;
    function onDoc(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) {
        setMenuOpen(false);
      }
    }
    function onKey(e) {
      if (e.key === 'Escape') setMenuOpen(false);
    }
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  function pickKind(kind) {
    setMenuOpen(false);
    if (kind === 'indicator') onChange(defaultIndicatorOperand());
    else if (kind === 'instrument') onChange(defaultInstrumentOperand());
    else if (kind === 'constant') onChange({ kind: 'constant', value: 0 });
  }

  const empty = operand === null || operand === undefined;

  if (empty) {
    return (
      <div className={styles.operandSlot} ref={rootRef} data-testid="operand-slot-empty">
        <button
          type="button"
          className={styles.operandAddBtn}
          onClick={() => setMenuOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-label={slotLabel ? `Add operand for ${slotLabel}` : 'Add operand'}
          data-testid="operand-add-btn"
        >
          +
        </button>
        {menuOpen && (
          <div className={styles.operandMenu} role="menu" data-testid="operand-menu">
            <button
              type="button"
              role="menuitem"
              className={styles.operandMenuItem}
              onClick={() => pickKind('indicator')}
              data-testid="operand-menu-indicator"
            >
              Indicator
            </button>
            <button
              type="button"
              role="menuitem"
              className={styles.operandMenuItem}
              onClick={() => pickKind('instrument')}
              data-testid="operand-menu-instrument"
            >
              Instrument
            </button>
            <button
              type="button"
              role="menuitem"
              className={styles.operandMenuItem}
              onClick={() => pickKind('constant')}
              data-testid="operand-menu-constant"
            >
              Constant
            </button>
          </div>
        )}
      </div>
    );
  }

  // Filled — render the appropriate inline editor plus a clear button.
  return (
    <div className={styles.operandSlotFilled} ref={rootRef} data-testid="operand-slot-filled">
      <div className={styles.operandEditor}>
        {operand.kind === 'indicator' && (
          <IndicatorEditor
            operand={operand}
            indicators={indicators}
            inputs={inputs}
            onChange={onChange}
          />
        )}
        {operand.kind === 'instrument' && (
          <InstrumentEditor operand={operand} inputs={inputs} onChange={onChange} />
        )}
        {operand.kind === 'constant' && (
          <ConstantEditor operand={operand} onChange={onChange} />
        )}
      </div>
      <button
        type="button"
        className={styles.operandClearBtn}
        onClick={() => setConfirmClear(true)}
        title="Clear operand"
        aria-label={slotLabel ? `Clear operand ${slotLabel}` : 'Clear operand'}
        data-testid="operand-clear-btn"
      >
        ×
      </button>
      <ConfirmDialog
        open={confirmClear}
        title="Clear operand?"
        message="The operand will be reset to empty and you'll need to pick a new kind."
        confirmLabel="Clear"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => { setConfirmClear(false); onChange(null); }}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}

/**
 * Small reusable select: bind a value to one of the signal's declared
 * input ids. Shows " (needs instrument)" next to ids that aren't fully
 * configured so the user knows they still have work to do.
 */
function InputIdSelect({ value, inputs, onChange, ariaLabel, testId }) {
  const list = Array.isArray(inputs) ? inputs : [];
  return (
    <select
      className={styles.operandSelect}
      value={value || ''}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel || 'Input'}
      data-testid={testId}
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
  );
}

function IndicatorEditor({ operand, indicators, inputs, onChange }) {
  const list = Array.isArray(indicators) ? indicators : [];
  const selectedId = operand.indicator_id || '';
  const selectedInd = list.find((i) => i.id === selectedId) || null;

  if (list.length === 0) {
    return (
      <div className={styles.operandEmpty}>
        No saved indicators — create one on the Indicators page.
      </div>
    );
  }

  return (
    <div className={styles.operandIndicator}>
      <select
        className={styles.operandSelect}
        value={selectedId}
        onChange={(e) => onChange({
          ...operand,
          indicator_id: e.target.value,
          output: operand.output || 'default',
          // Clear overrides when switching indicator — they belong to the
          // previous indicator's params.
          params_override: null,
          series_override: null,
        })}
        aria-label="Pick indicator"
        data-testid="operand-indicator-select"
      >
        <option value="" disabled>Select indicator…</option>
        {list.map((ind) => (
          <option key={ind.id} value={ind.id}>{ind.name || ind.id}</option>
        ))}
      </select>
      <InputIdSelect
        value={operand.input_id}
        inputs={inputs}
        onChange={(id) => onChange({ ...operand, input_id: id })}
        ariaLabel="Indicator input binding"
        testId="operand-indicator-input"
      />
      {selectedInd && (
        <IndicatorParamsOverride
          indicator={selectedInd}
          operand={operand}
          inputs={inputs}
          onOperandChange={onChange}
        />
      )}
    </div>
  );
}

function InstrumentEditor({ operand, inputs, onChange }) {
  return (
    <div className={styles.operandInstrumentWrap}>
      <InputIdSelect
        value={operand.input_id}
        inputs={inputs}
        onChange={(id) => onChange({
          kind: 'instrument',
          input_id: id,
          field: operand.field || 'close',
        })}
        ariaLabel="Instrument input"
        testId="operand-instrument-input"
      />
      <select
        className={styles.operandSelect}
        value={operand.field || 'close'}
        onChange={(e) => onChange({ ...operand, field: e.target.value })}
        aria-label="Instrument field"
        data-testid="operand-instrument-field"
      >
        <option value="open">open</option>
        <option value="high">high</option>
        <option value="low">low</option>
        <option value="close">close</option>
        <option value="volume">volume</option>
      </select>
    </div>
  );
}

function ConstantEditor({ operand, onChange }) {
  const current = Number.isFinite(operand.value) ? operand.value : 0;
  const [draft, setDraft] = useState(String(current));
  // Sync draft when the underlying operand value changes (e.g. parent
  // swaps the operand object via direction tab switch or undo).
  useEffect(() => {
    setDraft(String(current));
  }, [current]);
  return (
    <input
      className={styles.operandConstant}
      type="number"
      step="any"
      value={draft}
      onChange={(e) => {
        setDraft(e.target.value);
        const n = parseFloat(e.target.value);
        if (Number.isFinite(n)) onChange({ kind: 'constant', value: n });
      }}
      onBlur={() => {
        const n = parseFloat(draft);
        if (!Number.isFinite(n)) setDraft(String(current));
      }}
      aria-label="Constant value"
      data-testid="operand-constant-input"
    />
  );
}

export default OperandSlot;
