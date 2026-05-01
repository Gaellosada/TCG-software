import { useMemo, useCallback } from 'react';
import styles from './OptionStreamForm.module.css';

/**
 * Standalone, side-effect-free form for picking every field needed to
 * construct an OptionStreamRef request. Designed for cross-page reuse — the
 * future Continuous tab composes this form by wrapping it with extra
 * RollRule + adjustment fields, so the prop shape below is locked.
 *
 * Output object emitted via onChange (and read from `value`):
 *   {
 *     type: 'option_stream',
 *     collection: 'OPT_SP_500',
 *     option_type: 'C' | 'P',
 *     cycle: null | 'M' | 'W' | 'Q',
 *     maturity: { kind, ... },           // discriminated union (kind field)
 *     selection: { kind, ... },          // discriminated union (kind field)
 *     stream: 'mid'|'iv'|'delta'|'gamma'|'vega'|'theta'|'open_interest'|'volume',
 *   }
 *
 * Validation:
 *   - greek streams (gamma/vega/theta) require root.has_greeks === true;
 *   - by_delta + stream === 'delta' is tautological.
 * These produce a UI-side hint surface (the `validationError` returned via
 * the form's read-only public field, also signalled via the `disabled`
 * styling on the dependent fields). The backend has the hard guard.
 */

const ALL_OPTION_TYPES = ['C', 'P'];
const ALL_CYCLES = [null, 'M', 'W', 'Q'];
const ALL_STREAMS = ['mid', 'iv', 'delta', 'gamma', 'vega', 'theta', 'open_interest', 'volume'];
const GREEK_STREAMS = new Set(['gamma', 'vega', 'theta']);
const ALL_MATURITY_KINDS = ['next_third_friday', 'end_of_month', 'plus_n_days', 'fixed'];
const ALL_SELECTION_KINDS = ['by_strike', 'by_moneyness', 'by_delta'];

const MATURITY_LABELS = {
  next_third_friday: 'Next 3rd Friday',
  end_of_month: 'End of Month',
  plus_n_days: '+N Days',
  fixed: 'Fixed Date',
};

const SELECTION_LABELS = {
  by_strike: 'By Strike',
  by_moneyness: 'By Moneyness (K/S)',
  by_delta: 'By Delta',
};

const STREAM_LABELS = {
  mid: 'Mid',
  iv: 'IV',
  delta: 'Delta',
  gamma: 'Gamma',
  vega: 'Vega',
  theta: 'Theta',
  open_interest: 'Open Interest',
  volume: 'Volume',
};

const CYCLE_LABELS = { _any: 'Any', M: 'Monthly (M)', W: 'Weekly (W)', Q: 'Quarterly (Q)' };

/**
 * Build a default-shaped MaturityRule for a given kind.
 */
function defaultMaturity(kind) {
  switch (kind) {
    case 'next_third_friday':
      return { kind: 'next_third_friday', offset_months: 0 };
    case 'end_of_month':
      return { kind: 'end_of_month', offset_months: 0 };
    case 'plus_n_days':
      return { kind: 'plus_n_days', n: 30 };
    case 'fixed':
      return { kind: 'fixed', date: '' };
    default:
      return { kind: 'next_third_friday', offset_months: 0 };
  }
}

/**
 * Build a default-shaped SelectionCriterion for a given kind.
 * by_delta uses a signed target — sign carries the side.
 */
function defaultSelection(kind, optionType = 'C') {
  switch (kind) {
    case 'by_strike':
      return { kind: 'by_strike', strike: 0 };
    case 'by_moneyness':
      return { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 };
    case 'by_delta': {
      const sign = optionType === 'P' ? -1 : 1;
      return { kind: 'by_delta', target: 0.25 * sign, tolerance: 0.05, strict: false };
    }
    default:
      return { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 };
  }
}

/**
 * Build the full default OptionStreamRef given a roots list (picks the
 * first root) and the active allowed-* lists.
 */
export function buildDefaultOptionStream({
  availableRoots,
  allowedMaturityKinds = ALL_MATURITY_KINDS,
  allowedSelectionKinds = ALL_SELECTION_KINDS,
  allowedStreams = ALL_STREAMS,
  allowedOptionTypes = ALL_OPTION_TYPES,
  allowedCycles = ALL_CYCLES,
}) {
  const root = availableRoots && availableRoots.length > 0 ? availableRoots[0] : null;
  return {
    type: 'option_stream',
    collection: root ? root.collection : '',
    option_type: allowedOptionTypes[0] || 'C',
    cycle: allowedCycles[0] === undefined ? null : allowedCycles[0],
    maturity: defaultMaturity(allowedMaturityKinds[0] || 'next_third_friday'),
    selection: defaultSelection(allowedSelectionKinds[0] || 'by_moneyness', allowedOptionTypes[0] || 'C'),
    stream: allowedStreams[0] || 'mid',
  };
}

/**
 * Inspect the proposed value + roots and report any UI-side validation
 * issue. Returns null when the form is valid, else { code, message,
 * field } (field is the dom-id-able key the consumer can highlight).
 */
export function validateOptionStream(value, availableRoots) {
  if (!value || !value.collection) {
    return { code: 'NO_ROOT', message: 'Pick a root.', field: 'collection' };
  }
  const root = (availableRoots || []).find((r) => r.collection === value.collection);
  if (root && root.has_greeks === false && GREEK_STREAMS.has(value.stream)) {
    return {
      code: 'STREAM_UNAVAILABLE_FOR_ROOT',
      message: 'This root does not have greeks.',
      field: 'stream',
    };
  }
  if (
    value.selection
    && value.selection.kind === 'by_delta'
    && value.stream === 'delta'
  ) {
    return {
      code: 'TAUTOLOGICAL_OPTION_STREAM',
      message: 'Delta of a delta-targeted contract is tautological.',
      field: 'stream',
    };
  }
  return null;
}

/**
 * Standalone form. Reads everything from props; emits the next value via
 * `onChange`. Does no fetching. The parent owns the value/state.
 */
export default function OptionStreamForm({
  value,
  onChange,
  availableRoots,
  allowedMaturityKinds = ALL_MATURITY_KINDS,
  allowedSelectionKinds = ALL_SELECTION_KINDS,
  allowedStreams = ALL_STREAMS,
  allowedOptionTypes = ALL_OPTION_TYPES,
  allowedCycles = ALL_CYCLES,
  disabled = false,
}) {
  // Resolve a usable value: if the parent supplies null we still render
  // safely against a sensible default. Exposing onChange below means the
  // parent will adopt the default on first interaction.
  const v = useMemo(() => (
    value || buildDefaultOptionStream({
      availableRoots,
      allowedMaturityKinds,
      allowedSelectionKinds,
      allowedStreams,
      allowedOptionTypes,
      allowedCycles,
    })
  ), [value, availableRoots, allowedMaturityKinds, allowedSelectionKinds, allowedStreams, allowedOptionTypes, allowedCycles]);

  const validation = useMemo(() => validateOptionStream(v, availableRoots), [v, availableRoots]);

  const emit = useCallback((patch) => {
    onChange({ ...v, ...patch });
  }, [v, onChange]);

  const setRoot = useCallback((collection) => emit({ collection }), [emit]);

  const setOptionType = useCallback((option_type) => {
    // When the side flips, also flip the sign of by_delta target so it
    // stays meaningful (positive for calls, negative for puts).
    let nextSelection = v.selection;
    if (v.selection && v.selection.kind === 'by_delta') {
      const sign = option_type === 'P' ? -1 : 1;
      const mag = Math.abs(v.selection.target);
      nextSelection = { ...v.selection, target: mag * sign };
    }
    onChange({ ...v, option_type, selection: nextSelection });
  }, [v, onChange]);

  const setCycle = useCallback((rawCycle) => {
    const cycle = rawCycle === '' || rawCycle === '_any' ? null : rawCycle;
    emit({ cycle });
  }, [emit]);

  const setStream = useCallback((stream) => emit({ stream }), [emit]);

  const setMaturityKind = useCallback((kind) => {
    emit({ maturity: defaultMaturity(kind) });
  }, [emit]);

  const setMaturityField = useCallback((field, raw) => {
    const next = { ...v.maturity };
    if (field === 'offset_months' || field === 'n') {
      const parsed = parseInt(raw, 10);
      next[field] = Number.isNaN(parsed) ? 0 : parsed;
    } else {
      next[field] = raw;
    }
    emit({ maturity: next });
  }, [v.maturity, emit]);

  const setSelectionKind = useCallback((kind) => {
    emit({ selection: defaultSelection(kind, v.option_type) });
  }, [emit, v.option_type]);

  const setSelectionField = useCallback((field, raw) => {
    const next = { ...v.selection };
    if (field === 'strict') {
      next.strict = !!raw;
    } else {
      const parsed = parseFloat(raw);
      next[field] = Number.isNaN(parsed) ? 0 : parsed;
    }
    emit({ selection: next });
  }, [v.selection, emit]);

  const cycleSelectValue = v.cycle == null ? '_any' : v.cycle;
  const cycleAllowed = allowedCycles.map((c) => (c == null ? '_any' : c));

  return (
    <div className={styles.form} data-testid="option-stream-form" aria-disabled={disabled}>
      {/* Root */}
      <label className={styles.row}>
        <span className={styles.label}>Root</span>
        <select
          className={styles.input}
          value={v.collection}
          onChange={(e) => setRoot(e.target.value)}
          disabled={disabled}
          aria-label="Root"
        >
          {(availableRoots || []).length === 0 ? (
            <option value="">(no roots loaded)</option>
          ) : (
            (availableRoots || []).map((r) => (
              <option key={r.collection} value={r.collection}>
                {r.root_label || r.name || r.collection}
                {r.has_greeks === false ? ' (no greeks)' : ''}
              </option>
            ))
          )}
        </select>
      </label>

      {/* Option type */}
      <fieldset className={styles.row} disabled={disabled}>
        <legend className={styles.label}>Type</legend>
        <div className={styles.radioGroup}>
          {allowedOptionTypes.map((t) => (
            <label key={t} className={styles.radio}>
              <input
                type="radio"
                name="option-type"
                value={t}
                checked={v.option_type === t}
                onChange={() => setOptionType(t)}
                disabled={disabled}
              />
              <span>{t === 'C' ? 'Call' : 'Put'}</span>
            </label>
          ))}
        </div>
      </fieldset>

      {/* Cycle */}
      <label className={styles.row}>
        <span className={styles.label}>Cycle</span>
        <select
          className={styles.input}
          value={cycleSelectValue}
          onChange={(e) => setCycle(e.target.value)}
          disabled={disabled}
          aria-label="Cycle"
        >
          {cycleAllowed.map((c) => (
            <option key={c} value={c}>{CYCLE_LABELS[c] || c}</option>
          ))}
        </select>
      </label>

      {/* Maturity rule */}
      <div className={styles.row}>
        <span className={styles.label}>Maturity</span>
        <div className={styles.subgroup}>
          <select
            className={styles.input}
            value={v.maturity.kind}
            onChange={(e) => setMaturityKind(e.target.value)}
            disabled={disabled}
            aria-label="Maturity rule"
          >
            {allowedMaturityKinds.map((k) => (
              <option key={k} value={k}>{MATURITY_LABELS[k] || k}</option>
            ))}
          </select>
          {v.maturity.kind === 'next_third_friday' && (
            <label className={styles.fieldInline}>
              Offset (months)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.offset_months}
                onChange={(e) => setMaturityField('offset_months', e.target.value)}
                disabled={disabled}
                aria-label="Offset months"
              />
            </label>
          )}
          {v.maturity.kind === 'end_of_month' && (
            <label className={styles.fieldInline}>
              Offset (months)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.offset_months}
                onChange={(e) => setMaturityField('offset_months', e.target.value)}
                disabled={disabled}
                aria-label="Offset months"
              />
            </label>
          )}
          {v.maturity.kind === 'plus_n_days' && (
            <label className={styles.fieldInline}>
              N (days)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.n}
                onChange={(e) => setMaturityField('n', e.target.value)}
                disabled={disabled}
                aria-label="Plus N days"
              />
            </label>
          )}
          {v.maturity.kind === 'fixed' && (
            <label className={styles.fieldInline}>
              Date
              <input
                type="date"
                className={styles.input}
                value={v.maturity.date || ''}
                onChange={(e) => setMaturityField('date', e.target.value)}
                disabled={disabled}
                aria-label="Fixed date"
              />
            </label>
          )}
        </div>
      </div>

      {/* Selection criterion */}
      <div className={styles.row}>
        <span className={styles.label}>Selection</span>
        <div className={styles.subgroup}>
          <select
            className={styles.input}
            value={v.selection.kind}
            onChange={(e) => setSelectionKind(e.target.value)}
            disabled={disabled}
            aria-label="Selection criterion"
          >
            {allowedSelectionKinds.map((k) => (
              <option key={k} value={k}>{SELECTION_LABELS[k] || k}</option>
            ))}
          </select>
          {v.selection.kind === 'by_strike' && (
            <label className={styles.fieldInline}>
              Strike
              <input
                type="number"
                className={styles.input}
                step="any"
                value={v.selection.strike}
                onChange={(e) => setSelectionField('strike', e.target.value)}
                disabled={disabled}
                aria-label="Strike"
              />
            </label>
          )}
          {v.selection.kind === 'by_moneyness' && (
            <>
              <label className={styles.fieldInline}>
                Target K/S
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.target}
                  onChange={(e) => setSelectionField('target', e.target.value)}
                  disabled={disabled}
                  aria-label="Moneyness target"
                />
              </label>
              <label className={styles.fieldInline}>
                Tolerance
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.tolerance}
                  onChange={(e) => setSelectionField('tolerance', e.target.value)}
                  disabled={disabled}
                  aria-label="Moneyness tolerance"
                />
              </label>
            </>
          )}
          {v.selection.kind === 'by_delta' && (
            <>
              <label className={styles.fieldInline}>
                Target delta (signed)
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.target}
                  onChange={(e) => setSelectionField('target', e.target.value)}
                  disabled={disabled}
                  aria-label="Delta target"
                />
              </label>
              <label className={styles.fieldInline}>
                Tolerance
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.tolerance}
                  onChange={(e) => setSelectionField('tolerance', e.target.value)}
                  disabled={disabled}
                  aria-label="Delta tolerance"
                />
              </label>
              <label className={styles.fieldInline}>
                <input
                  type="checkbox"
                  checked={!!v.selection.strict}
                  onChange={(e) => setSelectionField('strict', e.target.checked)}
                  disabled={disabled}
                  aria-label="Strict"
                />
                Strict
              </label>
            </>
          )}
        </div>
      </div>

      {/* Stream */}
      <label className={styles.row}>
        <span className={styles.label}>Stream</span>
        <select
          className={styles.input}
          value={v.stream}
          onChange={(e) => setStream(e.target.value)}
          disabled={disabled}
          aria-label="Stream"
        >
          {allowedStreams.map((s) => (
            <option key={s} value={s}>{STREAM_LABELS[s] || s}</option>
          ))}
        </select>
      </label>

      {validation && (
        <div
          className={styles.validation}
          role="alert"
          data-testid="option-stream-validation"
          data-error-code={validation.code}
        >
          {validation.message}
        </div>
      )}
    </div>
  );
}

// Public helpers — used by composing components (the future Continuous tab
// reuses these when it builds its own initial value).
export {
  ALL_OPTION_TYPES,
  ALL_CYCLES,
  ALL_STREAMS,
  ALL_MATURITY_KINDS,
  ALL_SELECTION_KINDS,
  GREEK_STREAMS,
  defaultMaturity,
  defaultSelection,
};
