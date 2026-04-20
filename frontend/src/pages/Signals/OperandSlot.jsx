import { useEffect, useRef, useState } from 'react';
import SeriesPicker from '../Indicators/SeriesPicker';
import IndicatorParamsOverride from './IndicatorParamsOverride';
import ConfirmDialog from '../../components/ConfirmDialog';
import { defaultIndicatorOperand } from './conditionOps';
import styles from './Signals.module.css';

/**
 * One operand lives in one slot. States:
 *   - empty  (operand == null): renders a ``+`` button that opens a popover
 *             menu {Indicator, Instrument, Constant}. Picking installs the
 *             default operand for that kind.
 *   - filled: renders the appropriate inline editor + a small ``×`` button
 *             that opens a ConfirmDialog and, on confirm, resets to null.
 *
 * Props:
 *   operand      {Object|null}
 *   onChange     {Function} (nextOperand | null) => void
 *   indicators   {Array}
 *   slotLabel    {string?}  aria hint, shown in the confirm dialog
 */
function OperandSlot({ operand, onChange, indicators, slotLabel }) {
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
    else if (kind === 'instrument') {
      onChange({ kind: 'instrument', collection: '', instrument_id: '', field: 'close' });
    } else if (kind === 'constant') {
      onChange({ kind: 'constant', value: 0 });
    }
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
            onChange={onChange}
          />
        )}
        {operand.kind === 'instrument' && (
          <InstrumentEditor operand={operand} onChange={onChange} />
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

function IndicatorEditor({ operand, indicators, onChange }) {
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
      {selectedInd && (
        <IndicatorParamsOverride
          indicator={selectedInd}
          operand={operand}
          onOperandChange={onChange}
        />
      )}
    </div>
  );
}

function InstrumentEditor({ operand, onChange }) {
  const picked = (operand.collection && operand.instrument_id)
    ? { collection: operand.collection, instrument_id: operand.instrument_id }
    : null;
  return (
    <div className={styles.operandInstrumentWrap}>
      <SeriesPicker
        value={picked}
        onSave={(entry) => onChange({
          kind: 'instrument',
          collection: entry.collection,
          instrument_id: entry.instrument_id,
          field: operand.field || 'close',
        })}
        onCancel={() => { /* stays open */ }}
        defaultCollection={null}
        saveLabel="Use"
      />
    </div>
  );
}

function ConstantEditor({ operand, onChange }) {
  const current = Number.isFinite(operand.value) ? operand.value : 0;
  const [draft, setDraft] = useState(String(current));
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
