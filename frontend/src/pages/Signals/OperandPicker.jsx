import { useState } from 'react';
import SeriesPicker from '../Indicators/SeriesPicker';
import styles from './Signals.module.css';

/**
 * Compact tabbed picker for a single operand. Three tabs:
 *   - Indicator  — pick one of the user's saved indicators (from the list
 *                  the parent passes in; usually pulled from the existing
 *                  Indicators localStorage).
 *   - Instrument — reuses the existing ``SeriesPicker`` so discovery is
 *                  identical to the Indicators page. Stores the picked
 *                  ``(collection, instrument_id)`` and defaults ``field``
 *                  to ``"close"`` (v1 contract).
 *   - Constant   — numeric input.
 *
 * The initial tab is derived from the ``value`` prop's ``kind`` so a
 * user editing an existing operand lands on the right tab. When
 * ``value`` is ``null`` (iter-2: unset operand), NO tab is active and
 * the body renders a "pick me" hint — the user must explicitly click a
 * tab to commit to a kind.
 *
 * Props:
 *   value             {Object|null}  current operand spec (``{kind, ...}``)
 *                                    or ``null`` when unset.
 *   onChange          {Function}     (nextOperand) => void — fires on any change
 *   indicators        {Array}        [{id, name}, ...] — already-saved indicators
 */
function OperandPicker({ value, onChange, indicators }) {
  const initialTab = (value && value.kind) || null;
  const [tab, setTab] = useState(initialTab);

  function selectTab(nextTab) {
    setTab(nextTab);
    // When the user clicks a tab, emit a stub for that tab so the
    // condition payload is always shape-valid. Per iter-2 policy we do
    // NOT pre-select an indicator id or a collection — the stub is
    // deliberately incomplete until the user picks.
    if (nextTab === 'indicator') {
      onChange({ kind: 'indicator', indicator_id: '', output: 'default' });
    } else if (nextTab === 'instrument') {
      onChange({ kind: 'instrument', collection: '', instrument_id: '', field: 'close' });
    } else {
      onChange({ kind: 'constant', value: 0 });
    }
  }

  return (
    <div className={styles.operandPicker} data-testid="operand-picker">
      <div className={styles.operandTabs} role="tablist">
        {[
          { k: 'indicator', label: 'Indicator' },
          { k: 'instrument', label: 'Instrument' },
          { k: 'constant', label: 'Constant' },
        ].map(({ k, label }) => (
          <button
            type="button"
            key={k}
            role="tab"
            aria-selected={tab === k}
            data-testid={`operand-tab-${k}`}
            className={`${styles.operandTab} ${tab === k ? styles.operandTabActive : ''}`}
            onClick={() => selectTab(k)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className={styles.operandBody}>
        {tab === null && (
          <div className={styles.operandEmpty} data-testid="operand-unset">
            Pick a kind above.
          </div>
        )}
        {tab === 'indicator' && (
          <IndicatorOperandBody
            value={value}
            indicators={indicators}
            onChange={onChange}
          />
        )}
        {tab === 'instrument' && (
          <InstrumentOperandBody
            value={value}
            onChange={onChange}
          />
        )}
        {tab === 'constant' && (
          <ConstantOperandBody value={value} onChange={onChange} />
        )}
      </div>
    </div>
  );
}

function IndicatorOperandBody({ value, indicators, onChange }) {
  const list = Array.isArray(indicators) ? indicators : [];
  const selectedId = (value && value.kind === 'indicator') ? value.indicator_id : '';
  if (list.length === 0) {
    return (
      <div className={styles.operandEmpty}>
        No saved indicators — create one on the Indicators page first.
      </div>
    );
  }
  return (
    <select
      className={styles.operandSelect}
      value={selectedId || ''}
      onChange={(e) => onChange({
        kind: 'indicator',
        indicator_id: e.target.value,
        output: 'default',
      })}
      aria-label="Pick indicator"
    >
      <option value="" disabled>Select indicator…</option>
      {list.map((ind) => (
        <option key={ind.id} value={ind.id}>{ind.name || ind.id}</option>
      ))}
    </select>
  );
}

function InstrumentOperandBody({ value, onChange }) {
  // SeriesPicker handles collection + instrument selection end-to-end.
  // It emits ``{collection, instrument_id}``; we wrap that into an
  // instrument operand with ``field: 'close'`` (v1 contract).
  const picked = (value && value.kind === 'instrument' && value.collection && value.instrument_id)
    ? { collection: value.collection, instrument_id: value.instrument_id }
    : null;
  // We render SeriesPicker inline in "live" mode: every save is pushed
  // upward as the new operand. Cancel just keeps current selection.
  // Iter-2: no ``defaultCollection`` — the user must pick the collection.
  return (
    <SeriesPicker
      value={picked}
      onSave={(entry) => onChange({
        kind: 'instrument',
        collection: entry.collection,
        instrument_id: entry.instrument_id,
        field: 'close',
      })}
      onCancel={() => { /* picker stays open — no-op */ }}
      defaultCollection={null}
      saveLabel="Use"
    />
  );
}

function ConstantOperandBody({ value, onChange }) {
  const current = (value && value.kind === 'constant' && Number.isFinite(value.value))
    ? value.value
    : 0;
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
    />
  );
}

export default OperandPicker;
