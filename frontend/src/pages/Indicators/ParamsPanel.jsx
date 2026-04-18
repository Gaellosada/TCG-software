import { useState, useEffect, useRef } from 'react';
import SeriesPicker from './SeriesPicker';
import { getSeriesSummary } from '../../api/seriesSummary';
import styles from './ParamsPanel.module.css';

/**
 * Right panel — parameters, time-series slots, and Run button.
 *
 * The indicator's name header moved out of this panel in iter-7 — it
 * now lives above the code editor (see IndicatorsPage). Everything in
 * here is derived from the current indicator's parsed spec: ``paramsSpec``
 * yields one labelled input (number or checkbox) per typed param, and
 * ``seriesLabels`` yields one slot per unique label referenced in the
 * code. Both are driven by the parent page.
 *
 * Props:
 *   indicator        {Object|null}  { id, name, code, params, seriesMap, readonly? }
 *   paramsSpec       {Array}        [{ name, type, default }]
 *   seriesLabels     {Array<string>}  unique labels referenced by the code
 *   onParamChange    {Function}     (name, value) => void
 *   onSeriesSave     {Function}     (label, { collection, instrument_id }) => void
 *   onRun            {Function}     () => void
 *   running          {boolean}
 *   canRun           {boolean}
 *   defaultCollection {string|null} hint for SeriesPicker mounts
 *
 *   Note: run errors are rendered in the chart panel (IndicatorChart)
 *   — this panel no longer shows a duplicate banner.
 */
function ParamsPanel({
  indicator,
  paramsSpec,
  seriesLabels,
  onParamChange,
  onSeriesSave,
  onRun,
  running,
  canRun,
  defaultCollection,
}) {
  // Labels currently in inline-edit mode. Also tracks "add" slots for
  // empty labels (in which case value=null).
  const [editingLabels, setEditingLabels] = useState(() => new Set());

  // Per-input raw string drafts for numeric fields. Keyed by param name.
  // Allows the user to type "-", "1.", "-0." etc. without snapping to 0.
  // A draft is removed when the user blurs and a valid number is committed.
  const [numericDrafts, setNumericDrafts] = useState({});
  // Track previous indicator id to reset drafts on indicator switch.
  const prevIndicatorIdRef = useRef(indicator?.id);
  // Series rows with their details panel expanded, keyed by label.
  const [expandedLabels, setExpandedLabels] = useState(() => new Set());
  // Async summaries keyed by label — { loading, error, data }.
  const [summaries, setSummaries] = useState({});

  useEffect(() => {
    setEditingLabels(new Set());
    setExpandedLabels(new Set());
    setSummaries({});
    // Reset numeric drafts when switching indicator so stale drafts don't leak.
    if (prevIndicatorIdRef.current !== indicator?.id) {
      prevIndicatorIdRef.current = indicator?.id;
      setNumericDrafts({});
    }
  }, [indicator?.id]);

  function toggleDetails(label, picked) {
    setExpandedLabels((prev) => {
      const next = new Set(prev);
      if (next.has(label)) {
        next.delete(label);
        return next;
      }
      next.add(label);
      return next;
    });
    if (!picked || !picked.collection || !picked.instrument_id) return;
    // Mark loading and fetch. Subsequent toggles are no-ops until state
    // clears because the cache in seriesSummary returns the same promise.
    setSummaries((prev) => ({
      ...prev,
      [label]: { loading: true, error: null, data: null, ref: picked },
    }));
    getSeriesSummary(picked)
      .then((data) => {
        setSummaries((prev) => ({
          ...prev,
          [label]: { loading: false, error: null, data, ref: picked },
        }));
      })
      .catch((err) => {
        // ``err`` is a FetchError with {kind, title, message}. Surface both
        // title and message so the details pane reads as "Could not reach
        // the server — Failed to fetch" instead of a single blurb.
        const title = err?.title || 'Could not load preview';
        const message = err?.message || String(err) || 'Failed to load preview';
        setSummaries((prev) => ({
          ...prev,
          [label]: {
            loading: false,
            error: { title, message, kind: err?.kind || 'unknown' },
            data: null,
            ref: picked,
          },
        }));
      });
  }

  const disabled = !indicator;
  const params = indicator?.params || {};
  const seriesMap = indicator?.seriesMap || {};

  function startEdit(label) {
    setEditingLabels((prev) => {
      const next = new Set(prev);
      next.add(label);
      return next;
    });
  }

  function stopEdit(label) {
    setEditingLabels((prev) => {
      const next = new Set(prev);
      next.delete(label);
      return next;
    });
  }

  function handlePickerSave(label, entry) {
    onSeriesSave(label, entry);
    stopEdit(label);
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Parameters</span>
      </div>

      {/* Parameters section — derived from the typed signature. */}
      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <div className={styles.sectionLabel}>Parameters (from code)</div>
        </div>
        {(!paramsSpec || paramsSpec.length === 0) ? (
          <div className={styles.placeholder}>
            No parameters — declare them in the def compute signature.
          </div>
        ) : (
          <div className={styles.paramsList}>
            {paramsSpec.map((spec) => {
              const raw = params[spec.name];
              if (spec.type === 'bool') {
                const val = typeof raw === 'boolean' ? raw : !!spec.default;
                return (
                  <div key={spec.name} className={styles.paramRow}>
                    <label className={`${styles.paramLabel} codeRefLabel`} title={`${spec.name}: bool`}>
                      {spec.name}
                    </label>
                    <input
                      className={styles.paramCheckbox}
                      type="checkbox"
                      checked={val}
                      onChange={(e) => onParamChange(spec.name, e.target.checked)}
                      disabled={disabled}
                      aria-label={spec.name}
                    />
                  </div>
                );
              }
              // Display: draft string if mid-edit, else committed numeric value.
              const committed = Number.isFinite(raw) ? raw : (Number.isFinite(spec.default) ? spec.default : 0);
              const hasDraft = Object.prototype.hasOwnProperty.call(numericDrafts, spec.name);
              const displayValue = hasDraft ? numericDrafts[spec.name] : String(committed);

              function handleNumericChange(e) {
                const text = e.target.value;
                // Keep the raw string in local draft state so intermediate
                // inputs ("-", "1.", "-0.") are preserved without snapping.
                setNumericDrafts((prev) => ({ ...prev, [spec.name]: text }));
                // Only propagate if the current text parses to a finite number.
                const n = parseFloat(text);
                if (Number.isFinite(n)) {
                  onParamChange(spec.name, n);
                }
                // Otherwise: hold the draft, do not commit 0 or NaN upstream.
              }

              function handleNumericBlur() {
                // On blur: if draft is empty or non-numeric, reset to committed value.
                const draft = numericDrafts[spec.name];
                if (draft !== undefined) {
                  const n = parseFloat(draft);
                  if (!Number.isFinite(n)) {
                    // Revert display to the last committed value; don't mutate params.
                    setNumericDrafts((prev) => {
                      const next = { ...prev };
                      delete next[spec.name];
                      return next;
                    });
                  } else {
                    // Commit valid parsed value and clear draft.
                    onParamChange(spec.name, n);
                    setNumericDrafts((prev) => {
                      const next = { ...prev };
                      delete next[spec.name];
                      return next;
                    });
                  }
                }
              }

              return (
                <div key={spec.name} className={styles.paramRow}>
                  <label className={`${styles.paramLabel} codeRefLabel`} title={`${spec.name}: ${spec.type}`}>
                    {spec.name}
                  </label>
                  <input
                    className={styles.paramInput}
                    type="number"
                    step={spec.type === 'int' ? '1' : 'any'}
                    value={displayValue}
                    onChange={handleNumericChange}
                    onBlur={handleNumericBlur}
                    disabled={disabled}
                    aria-label={spec.name}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className={styles.divider} />

      {/* Series section — one slot per unique label in the code. */}
      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <div className={styles.sectionLabel}>Inputs (series)</div>
        </div>

        {(!seriesLabels || seriesLabels.length === 0) ? (
          <div className={styles.placeholder}>
            No series — reference them via series['label'] in your code.
          </div>
        ) : (
          <div className={styles.seriesList}>
            {seriesLabels.map((label) => {
              const picked = seriesMap[label] || null;
              const isEditing = editingLabels.has(label);
              if (isEditing || !picked) {
                return (
                  <div key={label} className={styles.seriesRow}>
                    <div className={styles.seriesLabelWrap}>
                      <span className={`${styles.seriesLabelText} codeRefLabel`}>{label}</span>
                      <SeriesPicker
                        value={picked}
                        onSave={(entry) => handlePickerSave(label, entry)}
                        onCancel={() => stopEdit(label)}
                        defaultCollection={defaultCollection}
                        saveLabel="Save"
                      />
                    </div>
                  </div>
                );
              }
              const isExpanded = expandedLabels.has(label);
              const summary = summaries[label];
              return (
                <div key={label} className={styles.seriesRowGroup}>
                  <div className={styles.seriesRow}>
                    <span className={`${styles.seriesLabelText} codeRefLabel`}>{label}</span>
                    <span className={styles.seriesChip}>
                      {picked.collection} / {picked.instrument_id}
                    </span>
                    <button
                      className={styles.iconBtn}
                      onClick={() => toggleDetails(label, picked)}
                      title={isExpanded ? 'Hide details' : 'Show details'}
                      aria-label={`${isExpanded ? 'Hide' : 'Show'} details for ${label}`}
                      aria-expanded={isExpanded}
                      disabled={disabled}
                    >
                      ⓘ
                    </button>
                    <button
                      className={styles.iconBtn}
                      onClick={() => startEdit(label)}
                      title="Edit series"
                      aria-label={`Edit series ${label}`}
                      disabled={disabled}
                    >
                      ✎
                    </button>
                  </div>
                  {isExpanded && (
                    <div className={styles.detailsPane} role="region" aria-label={`Details for ${label}`}>
                      <div className={styles.detailsHeader}>
                        <span className="codeRefLabel">series[&#39;{label}&#39;]</span>
                      </div>
                      {summary?.loading && (
                        <div className={styles.detailsBody}>Loading…</div>
                      )}
                      {summary?.error && (
                        <div className={styles.detailsBody} data-error-kind={summary.error.kind}>
                          <strong>{summary.error.title}</strong>
                          {summary.error.message ? ` — ${summary.error.message}` : null}
                        </div>
                      )}
                      {summary?.data && (
                        <pre className={styles.detailsCode}>
{`→ np.ndarray[float64]
length:  ${summary.data.length}
dates:   ${summary.data.start ?? '—'} … ${summary.data.end ?? '—'}`}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className={styles.divider} />

      {/* Run section */}
      <div className={styles.section}>
        <div className={styles.sectionLabel}>Run</div>
        <button
          className={styles.runBtn}
          onClick={onRun}
          disabled={!canRun}
          aria-label="Run indicator"
        >
          {running ? 'Computing...' : 'Run'}
        </button>
      </div>
    </div>
  );
}

export default ParamsPanel;
