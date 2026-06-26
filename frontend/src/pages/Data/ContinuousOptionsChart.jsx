import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { useOptionRoots } from '../../hooks/marketQueries';
import Chart from '../../components/Chart';
import OptionStreamForm, { buildDefaultOptionStream } from '../../components/OptionStreamForm';
import OptionDateRangeControl, { computeDefaultRange } from '../../components/OptionDateRangeControl';
import { resolveOptionStream } from '../../api/options';
import { TRACE_COLORS } from '../../utils/chartTheme';
import styles from './ChartBase.module.css';

/**
 * Build a label for an option stream ref that is meaningful in a chart
 * legend. Combines stream type + selection kind + maturity kind.
 */
function buildStreamLabel(ref) {
  const parts = [];
  if (ref.stream) parts.push(ref.stream.toUpperCase());
  if (ref.option_type) parts.push(ref.option_type === 'C' ? 'Call' : 'Put');
  if (ref.selection?.kind) parts.push(ref.selection.kind.replace(/_/g, ' '));
  return parts.join(' / ') || 'Option Stream';
}

function ContinuousOptionsChart({ collection }) {
  // ── Fetch option roots for the form dropdown (SWR: shared cache with
  //    CategoryBrowser; renders instantly on re-navigation) ──
  const { data: rootsData, loading: rootsLoading, error: rootsError } = useOptionRoots();

  const availableRoots = useMemo(() => {
    if (!rootsData || !rootsData.roots) return [];
    return rootsData.roots;
  }, [rootsData]);

  // ── Stream ref state (controlled by OptionStreamForm) ──
  const [streamRef, setStreamRef] = useState(null);

  // Initialize/reinitialize streamRef when roots load or collection changes.
  // Pre-select the matching root from the collection prop.
  useEffect(() => {
    if (availableRoots.length === 0) return;
    setStreamRef((prev) => {
      // Only re-init if no value or collection changed
      if (prev && prev.collection === collection) return prev;
      const base = buildDefaultOptionStream({ availableRoots });
      // Override collection to match the current Data page selection
      const matchingRoot = availableRoots.find((r) => r.collection === collection);
      return {
        ...base,
        collection: matchingRoot ? matchingRoot.collection : base.collection,
      };
    });
  }, [availableRoots, collection]);

  // ── Date range state — default 1-year lookback ending today. ──
  const [dateRange, setDateRange] = useState(() => computeDefaultRange());

  // ── Resolution state ──
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState(null);
  const abortRef = useRef(null);

  // Cleanup abort controller on unmount
  useEffect(() => {
    return () => {
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  const handleResolve = useCallback(async () => {
    if (!streamRef || !streamRef.collection) return;

    // Abort any previous request
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);
    setResult(null);
    setProgress(0);

    const label = buildStreamLabel(streamRef);

    try {
      const data = await resolveOptionStream(
        [{ ref: streamRef, label }],
        dateRange.start,
        dateRange.end,
        {
          signal: controller.signal,
          onProgress: (frac) => setProgress(frac),
        },
      );
      if (!controller.signal.aborted) {
        setResult(data);
      }
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (!controller.signal.aborted) {
        setError(err);
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
        setProgress(null);
      }
    }
  }, [streamRef, dateRange]);

  // ── Build chart traces from result ──
  const { traces, pointCount, dateRangeLabel } = useMemo(() => {
    if (!result || !result.dates || result.dates.length === 0) {
      return { traces: [], pointCount: 0, dateRangeLabel: '' };
    }

    const t = [];
    let colorIdx = 0;
    for (const [label, stream] of Object.entries(result.streams)) {
      t.push({
        x: result.dates,
        y: stream.values,
        type: 'scatter',
        mode: 'lines',
        name: label,
        line: { color: TRACE_COLORS[colorIdx % TRACE_COLORS.length], width: 1 },
        hovertemplate: '%{x}<br>' + label + ': %{y:,.4f}<extra></extra>',
        connectgaps: false,
      });
      colorIdx++;
    }

    const first = result.dates[0];
    const last = result.dates[result.dates.length - 1];

    return {
      traces: t,
      pointCount: result.dates.length,
      dateRangeLabel: `${first} to ${last}`,
    };
  }, [result]);

  // ── Build roll markers from result.rolls (flat array across labels) ──
  //
  // CONTRACT B.7: `result.rolls` is keyed by stream label, each value
  // is a list of `{ date, sold, bought }` events. Flatten to a flat
  // marker array Chart can consume. Drop entries where the value is
  // null on either side — without a Y position we cannot pin the dot
  // to the price line (CONTRACT C: missing-value ruling).
  const markers = useMemo(() => {
    const out = [];
    const rolls = result?.rolls;
    if (!rolls) return out;
    for (const label of Object.keys(rolls)) {
      const events = rolls[label];
      if (!Array.isArray(events)) continue;
      for (const roll of events) {
        if (roll?.sold?.value != null) {
          out.push({
            x: roll.date,
            y: roll.sold.value,
            kind: 'sell',
            tooltip: roll.sold,
          });
        }
        if (roll?.bought?.value != null) {
          out.push({
            x: roll.date,
            y: roll.bought.value,
            kind: 'buy',
            tooltip: roll.bought,
          });
        }
      }
    }
    return out;
  }, [result]);

  // ── Snap notice (Issue #2 D2) ──
  //
  // A non-NearestToTarget maturity rule whose arithmetic expiration is not
  // listed is snapped to the nearest listed expiration; the resolver records a
  // per-date `snapped_to:<iso>` diagnostic. Surface a small notice so the user
  // knows the maturity they picked was substituted (reuses the diagnostics
  // already in the response — no per-date diagnostics renderer).
  const snappedExpirations = useMemo(() => {
    const streams = result?.streams;
    if (!streams) return [];
    const seen = new Set();
    for (const stream of Object.values(streams)) {
      const diags = stream?.diagnostics;
      if (!Array.isArray(diags)) continue;
      for (const d of diags) {
        if (typeof d === 'string' && d.startsWith('snapped_to:')) {
          seen.add(d.slice('snapped_to:'.length));
        }
      }
    }
    return Array.from(seen).sort();
  }, [result]);

  // ── Render ──

  if (rootsLoading) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>Loading option roots...</div>
      </div>
    );
  }

  if (rootsError) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>
          Failed to load option roots: {rootsError.message}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container} data-testid="continuous-options-chart">
      <div className={styles.header}>
        <h2 className={styles.title}>{collection} — Continuous Options</h2>
        {result && (
          <span className={styles.meta}>
            {pointCount.toLocaleString()} points
            &nbsp;&middot;&nbsp;
            {dateRangeLabel}
          </span>
        )}
      </div>

      <div className={`${styles.controls} ${styles.controlsCapped}`}>
        <OptionStreamForm
          value={streamRef}
          onChange={setStreamRef}
          availableRoots={availableRoots}
          disabled={loading}
        />
      </div>

      <div className={styles.controls}>
        <OptionDateRangeControl
          value={dateRange}
          onChange={setDateRange}
          disabled={loading}
        />
        <button
          type="button"
          className={styles.select}
          onClick={handleResolve}
          disabled={loading || !streamRef || !streamRef.collection}
          data-testid="resolve-button"
        >
          {loading
            ? progress != null && progress < 1
              ? `Resolving... ${Math.round(progress * 100)}%`
              : 'Resolving...'
            : 'Resolve'}
        </button>
      </div>

      {error && (
        <div className={styles.error} data-testid="resolve-error">
          {error.message || 'An error occurred during resolution.'}
        </div>
      )}

      {snappedExpirations.length > 0 && (
        <div className={styles.snapNotice} data-testid="snap-notice" role="status">
          Maturity expiration snapped to nearest listed:{' '}
          {snappedExpirations.join(', ')}
          {' '}(the rule&apos;s computed expiration was not listed for this root).
        </div>
      )}

      {result && traces.length > 0 && (
        <div className={styles.chartCard}>
          <Chart
            traces={traces}
            markers={markers}
            className={styles.chartWrapper}
            downloadFilename={`${collection}-continuous-options`}
          />
        </div>
      )}

      {result && traces.length === 0 && (
        <div className={styles.status}>
          No data returned for this stream configuration.
        </div>
      )}

      {!result && !loading && !error && (
        <div className={styles.status}>
          Configure the stream above and click Resolve to materialise
          the time series.
        </div>
      )}
    </div>
  );
}

export default ContinuousOptionsChart;
