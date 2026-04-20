import { useMemo, useState } from 'react';
import { parseIndicatorSpec } from '../Indicators/paramParser';
import { isInputConfigured } from './blockShape';
import styles from './Signals.module.css';

/**
 * Per-operand override controls for an Indicator reference (v3 / iter-4).
 *
 * Reads the indicator's base ``params`` / ``seriesMap`` (the Indicators-
 * page defaults, already reconciled against the parsed spec), and renders
 * one editor per param and per series label.
 *
 * v3 change: ``series_override`` is now ``{ label -> input_id }`` — labels
 * are rebound to one of the signal's declared inputs (by id). Non-primary
 * labels default to the indicator's baseSeriesMap instrument; the primary
 * label is already replaced by the operand's ``input_id``.
 *
 * Props:
 *   indicator         {Object|null}  {id, name, code, params, seriesMap}
 *   operand           {Object}       the indicator operand (kind='indicator')
 *   inputs            {Array}        the signal's declared inputs
 *   onOperandChange   {Function}     (nextOperand) => void
 */
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

  function writeOperand(nextParams, nextSeries) {
    // Empty overrides collapse to null so a round-trip with no edits
    // matches the untouched operand.
    const p = Object.keys(nextParams).length === 0 ? null : nextParams;
    const s = Object.keys(nextSeries).length === 0 ? null : nextSeries;
    onOperandChange({
      ...operand,
      params_override: p,
      series_override: s,
    });
  }

  function setParam(name, type, rawValue) {
    const nextParams = { ...paramsOverride };
    if (rawValue === '' || rawValue === null || rawValue === undefined) {
      delete nextParams[name];
    } else if (type === 'int') {
      const n = parseInt(rawValue, 10);
      if (!Number.isFinite(n)) { delete nextParams[name]; } else { nextParams[name] = n; }
    } else if (type === 'float') {
      const n = parseFloat(rawValue);
      if (!Number.isFinite(n)) { delete nextParams[name]; } else { nextParams[name] = n; }
    } else if (type === 'bool') {
      nextParams[name] = !!rawValue;
    } else {
      nextParams[name] = rawValue;
    }
    writeOperand(nextParams, seriesOverride);
  }

  function resetParam(name) {
    const nextParams = { ...paramsOverride };
    delete nextParams[name];
    writeOperand(nextParams, seriesOverride);
  }

  function setSeries(label, inputId) {
    // v3: each series override is the string id of one of the signal's
    // declared inputs (or absent = use the base series_map from the
    // indicator defaults).
    const nextSeries = { ...seriesOverride };
    if (!inputId) {
      delete nextSeries[label];
    } else {
      nextSeries[label] = inputId;
    }
    writeOperand(paramsOverride, nextSeries);
  }

  function resetSeries(label) {
    const nextSeries = { ...seriesOverride };
    delete nextSeries[label];
    writeOperand(paramsOverride, nextSeries);
  }

  function resetAll() {
    onOperandChange({
      ...operand,
      params_override: null,
      series_override: null,
    });
  }

  const summary = indicator
    ? `${indicator.name || indicator.id}${hasAnyOverride ? ' *' : ''}`
    : 'No indicator';

  return (
    <div className={styles.indicatorOverride} data-testid="indicator-override">
      <button
        type="button"
        className={styles.indicatorOverrideSummary}
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        data-testid="indicator-override-toggle"
      >
        <span className={styles.indicatorOverrideChevron} aria-hidden="true">
          {expanded ? '▾' : '▸'}
        </span>
        <span className={styles.indicatorOverrideName}>{summary}</span>
        {hasAnyOverride && (
          <span className={styles.indicatorOverrideBadge} title="Has overrides">override</span>
        )}
      </button>
      {expanded && indicator && (
        <div className={styles.indicatorOverrideGrid}>
          {spec.params.length === 0 && spec.seriesLabels.length === 0 && (
            <div className={styles.operandEmpty}>No params or series in this indicator.</div>
          )}
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
            // The primary label (idx=0) is implicitly bound via the
            // operand's ``input_id`` — show a hint so the user doesn't
            // try to "also" override it here.
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
