import { useMemo, useState } from 'react';
import { parseIndicatorSpec } from '../Indicators/paramParser';
import { isInputConfigured } from './blockShape';
import styles from './Signals.module.css';

/**
 * Per-operand override controls for an Indicator reference (v4).
 *
 * v4 behaviour (ORDERS bullet #4):
 *   - 0 params & 0 non-primary series → non-clickable "No parameters" tag.
 *   - 1 param (and no other series) → inline `<paramName>: <value>` editor,
 *     no dropdown.
 *   - 2+ editable controls → collapsible "Parameters" dropdown (unchanged
 *     from v3).
 *
 * All three branches write to the SAME storage fields (`params_override`
 * and `series_override`) via the shared `writeOverrides` helper exported
 * below. Flipping an indicator from 1 → 2 params does NOT orphan state:
 * the storage shape is identical in every branch.
 *
 * Reads the indicator's base ``params`` / ``seriesMap`` (the Indicators-
 * page defaults, already reconciled against the parsed spec).
 *
 * Props:
 *   indicator         {Object|null}  {id, name, code, params, seriesMap}
 *   operand           {Object}       the indicator operand (kind='indicator')
 *   inputs            {Array}        the signal's declared inputs
 *   onOperandChange   {Function}     (nextOperand) => void
 */

/**
 * Pure — merge the shallow-cloned override maps back onto the operand.
 * Collapses empty maps to `null` so a no-edit round-trip is a structural
 * no-op. This is the SINGLE place that writes params_override /
 * series_override. Every UI branch (tag / inline / dropdown) calls this.
 */
export function writeOverrides(operand, nextParams, nextSeries) {
  const p = nextParams && Object.keys(nextParams).length > 0 ? nextParams : null;
  const s = nextSeries && Object.keys(nextSeries).length > 0 ? nextSeries : null;
  return { ...operand, params_override: p, series_override: s };
}

/**
 * Pure — compute the param value that should be displayed right now
 * (override wins over the indicator default wins over the parser default).
 */
export function effectiveParamValue(spec, baseParams, paramsOverride) {
  if (paramsOverride && spec.name in paramsOverride) return paramsOverride[spec.name];
  if (baseParams && spec.name in baseParams) return baseParams[spec.name];
  return spec.default;
}

/** Parse `rawValue` as the declared param type and return the coerced value,
 *  or `undefined` if the value should be removed from the override map. */
export function coerceParamInput(type, rawValue) {
  if (rawValue === '' || rawValue === null || rawValue === undefined) return undefined;
  if (type === 'int') {
    const n = parseInt(rawValue, 10);
    return Number.isFinite(n) ? n : undefined;
  }
  if (type === 'float') {
    const n = parseFloat(rawValue);
    return Number.isFinite(n) ? n : undefined;
  }
  if (type === 'bool') {
    return !!rawValue;
  }
  return rawValue;
}

function IndicatorParamsOverride({ indicator, operand, inputs, onOperandChange }) {
  const [expanded, setExpanded] = useState(false);

  const spec = useMemo(() => {
    if (!indicator || typeof indicator.code !== 'string') {
      return { params: [], seriesLabels: [] };
    }
    try { return parseIndicatorSpec(indicator.code); } catch { return { params: [], seriesLabels: [] }; }
  }, [indicator]);

  const baseParams = (indicator && indicator.params) || {};
  const baseSeries = (indicator && indicator.seriesMap) || {};
  const paramsOverride = operand.params_override || {};
  const seriesOverride = operand.series_override || {};
  const inputList = Array.isArray(inputs) ? inputs : [];

  const hasAnyOverride = Object.keys(paramsOverride).length > 0
    || Object.keys(seriesOverride).length > 0;

  // Non-primary series labels are the only ones editable here (primary
  // label is bound to the operand's input_id). Factoring that out lets us
  // decide 0 / 1 / 2+ tiers honestly.
  const editableSeriesLabels = (spec.seriesLabels || []).slice(1);
  const totalEditableControls = (spec.params?.length || 0) + editableSeriesLabels.length;

  function setParam(name, type, rawValue) {
    const next = { ...paramsOverride };
    const coerced = coerceParamInput(type, rawValue);
    if (coerced === undefined) {
      delete next[name];
    } else {
      next[name] = coerced;
    }
    onOperandChange(writeOverrides(operand, next, seriesOverride));
  }

  function resetParam(name) {
    const next = { ...paramsOverride };
    delete next[name];
    onOperandChange(writeOverrides(operand, next, seriesOverride));
  }

  function setSeries(label, inputId) {
    const next = { ...seriesOverride };
    if (!inputId) {
      delete next[label];
    } else {
      next[label] = inputId;
    }
    onOperandChange(writeOverrides(operand, paramsOverride, next));
  }

  function resetSeries(label) {
    const next = { ...seriesOverride };
    delete next[label];
    onOperandChange(writeOverrides(operand, paramsOverride, next));
  }

  function resetAll() {
    onOperandChange(writeOverrides(operand, {}, {}));
  }

  // ── Tier 1: no editable controls at all → non-clickable tag. ──────────
  if (totalEditableControls === 0) {
    return (
      <span
        className={styles.indicatorOverrideNoParams}
        data-testid="indicator-override-no-params"
        title={indicator ? `${indicator.name || indicator.id} has no parameters` : 'No parameters'}
      >
        No parameters
      </span>
    );
  }

  // ── Tier 2: exactly one param, no editable series → inline editor. ────
  if (spec.params?.length === 1 && editableSeriesLabels.length === 0) {
    const p = spec.params[0];
    const displayV = effectiveParamValue(p, baseParams, paramsOverride);
    const isOverridden = p.name in paramsOverride;
    return (
      <span
        className={styles.indicatorOverrideInline}
        data-testid="indicator-override-inline"
      >
        <span className={styles.indicatorOverrideInlineLabel}>{p.name}:</span>
        {p.type === 'bool' ? (
          <input
            type="checkbox"
            className={styles.indicatorOverrideInlineCheck}
            checked={!!displayV}
            onChange={(e) => setParam(p.name, p.type, e.target.checked)}
            aria-label={`${p.name} override`}
            data-testid={`indicator-override-inline-${p.name}`}
          />
        ) : (
          <input
            type="number"
            className={styles.indicatorOverrideInlineInput}
            step={p.type === 'int' ? '1' : 'any'}
            value={displayV === null || displayV === undefined ? '' : displayV}
            onChange={(e) => setParam(p.name, p.type, e.target.value)}
            aria-label={`${p.name} override`}
            data-testid={`indicator-override-inline-${p.name}`}
          />
        )}
        {isOverridden && (
          <button
            type="button"
            className={styles.indicatorOverrideReset}
            onClick={() => resetParam(p.name)}
            title={`Reset ${p.name} to default`}
            aria-label={`Reset ${p.name}`}
          >
            ↺
          </button>
        )}
      </span>
    );
  }

  // ── Tier 3: 2+ editable controls → existing collapsible dropdown. ─────
  const overrideCount = Object.keys(paramsOverride).length
    + Object.keys(seriesOverride).length;

  return (
    <div className={styles.indicatorOverride} data-testid="indicator-override">
      <button
        type="button"
        className={styles.indicatorOverrideSummary}
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        data-testid="indicator-override-toggle"
        title={indicator ? `Params for ${indicator.name || indicator.id}` : 'No indicator'}
      >
        <span className={styles.indicatorOverrideChevron} aria-hidden="true">
          {expanded ? '▾' : '▸'}
        </span>
        <span className={styles.indicatorOverrideName}>Parameters</span>
        {hasAnyOverride && (
          <span className={styles.indicatorOverrideBadge} title="Has overrides">
            {overrideCount}
          </span>
        )}
      </button>
      {expanded && indicator && (
        <div className={styles.indicatorOverrideGrid}>
          {spec.params.map((p) => {
            const baseV = (p.name in baseParams) ? baseParams[p.name] : p.default;
            const overrideV = paramsOverride[p.name];
            const isOverridden = p.name in paramsOverride;
            const displayV = isOverridden ? overrideV : baseV;
            return (
              <div key={`p-${p.name}`} className={styles.indicatorOverrideRow}>
                <label className={styles.indicatorOverrideLabel} title={`type: ${p.type}, default: ${p.default}`}>
                  {p.name}
                </label>
                {p.type === 'bool' ? (
                  <input
                    type="checkbox"
                    checked={!!displayV}
                    onChange={(e) => setParam(p.name, p.type, e.target.checked)}
                    aria-label={`${p.name} override`}
                  />
                ) : (
                  <input
                    type="number"
                    className={styles.indicatorOverrideInput}
                    step={p.type === 'int' ? '1' : 'any'}
                    value={displayV === null || displayV === undefined ? '' : displayV}
                    onChange={(e) => setParam(p.name, p.type, e.target.value)}
                    aria-label={`${p.name} override`}
                    data-testid={`override-param-${p.name}`}
                  />
                )}
                {isOverridden && (
                  <button
                    type="button"
                    className={styles.indicatorOverrideReset}
                    onClick={() => resetParam(p.name)}
                    title={`Reset ${p.name}`}
                    aria-label={`Reset ${p.name}`}
                  >
                    ↺
                  </button>
                )}
              </div>
            );
          })}
          {spec.seriesLabels.map((label, idx) => {
            const overrideInputId = seriesOverride[label];
            const isOverridden = label in seriesOverride;
            const isPrimary = idx === 0;
            const baseS = baseSeries[label] || null;
            const baseStr = baseS
              ? `${baseS.collection}:${baseS.instrument_id}`
              : '(unset)';
            return (
              <div key={`s-${label}`} className={styles.indicatorOverrideRow}>
                <label className={styles.indicatorOverrideLabel}>
                  {label}{isPrimary ? ' (primary)' : ''}
                </label>
                {isPrimary ? (
                  <span className={styles.operandEmpty} style={{ fontSize: '0.75rem' }}>
                    bound via operand input
                  </span>
                ) : (
                  <select
                    className={styles.indicatorOverrideInput}
                    value={overrideInputId || ''}
                    onChange={(e) => setSeries(label, e.target.value || null)}
                    aria-label={`${label} series override`}
                    data-testid={`override-series-${label}`}
                  >
                    <option value="">default ({baseStr})</option>
                    {inputList.map((input) => {
                      const ok = isInputConfigured(input);
                      return (
                        <option key={input.id} value={input.id}>
                          {input.id}{!ok ? ' (needs instrument)' : ''}
                        </option>
                      );
                    })}
                  </select>
                )}
                {!isPrimary && isOverridden && (
                  <button
                    type="button"
                    className={styles.indicatorOverrideReset}
                    onClick={() => resetSeries(label)}
                    title={`Reset ${label}`}
                    aria-label={`Reset ${label} series`}
                  >
                    ↺
                  </button>
                )}
              </div>
            );
          })}
          {hasAnyOverride && (
            <button
              type="button"
              className={styles.indicatorOverrideResetAll}
              onClick={resetAll}
              data-testid="indicator-override-reset-all"
            >
              Reset all overrides
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default IndicatorParamsOverride;
