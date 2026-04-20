import { useMemo, useState } from 'react';
import { parseIndicatorSpec } from '../Indicators/paramParser';
import styles from './Signals.module.css';

/**
 * Per-operand override controls for an Indicator reference.
 *
 * Reads the indicator's base ``params`` / ``seriesMap`` (the Indicators-
 * page defaults, already reconciled against the parsed spec), and renders
 * one editor per param and per series label. Edits produce
 * ``operand.params_override[name]`` / ``operand.series_override[label]``
 * entries. Clearing a field back to the default removes the key; when
 * the override object becomes empty, it collapses to ``null`` so
 * round-trip equals default.
 *
 * Props:
 *   indicator         {Object|null}  {id, name, code, params, seriesMap}
 *   operand           {Object}       the indicator operand (kind='indicator')
 *   onOperandChange   {Function}     (nextOperand) => void
 */
function IndicatorParamsOverride({ indicator, operand, onOperandChange }) {
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

  function setSeries(label, partial) {
    // partial = {collection, instrument_id}
    const nextSeries = { ...seriesOverride };
    if (!partial || (!partial.collection && !partial.instrument_id)) {
      delete nextSeries[label];
    } else {
      nextSeries[label] = partial;
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
          {spec.seriesLabels.map((label) => {
            const baseS = baseSeries[label] || null;
            const overrideS = seriesOverride[label];
            const isOverridden = label in seriesOverride;
            const display = isOverridden ? overrideS : baseS;
            const displayStr = display
              ? `${display.collection}:${display.instrument_id}`
              : '';
            return (
              <div key={`s-${label}`} className={styles.indicatorOverrideRow}>
                <label className={styles.indicatorOverrideLabel}>{label}</label>
                <input
                  type="text"
                  className={styles.indicatorOverrideInput}
                  value={displayStr}
                  placeholder="COLLECTION:SYMBOL"
                  onChange={(e) => {
                    const raw = e.target.value;
                    const [col, sym] = raw.split(':');
                    if (!col && !sym) setSeries(label, null);
                    else setSeries(label, { collection: col || '', instrument_id: sym || '' });
                  }}
                  aria-label={`${label} series override`}
                  data-testid={`override-series-${label}`}
                />
                {isOverridden && (
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
