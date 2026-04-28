import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { useContractSeries } from './useContractSeries';
import { TRACE_COLORS, createVerticalLineTrace, hiddenOverlayAxis } from '../../utils/chartTheme';
import styles from './ContractDetailPanel.module.css';

const AMERICAN_EXERCISE_ROOTS = new Set(['OPT_T_NOTE_10_Y', 'OPT_T_BOND']);

const GREEK_DEFS = [
  { key: 'iv', label: 'IV', color: TRACE_COLORS[1] },
  { key: 'delta', label: 'Δ', color: TRACE_COLORS[2] },
  { key: 'gamma', label: 'Γ', color: TRACE_COLORS[3] },
  { key: 'theta', label: 'Θ', color: TRACE_COLORS[4] },
  { key: 'vega', label: 'ν', color: TRACE_COLORS[5] },
];

// ---------------------------------------------------------------------------
// Life-cycle marker definitions
// ---------------------------------------------------------------------------

// Each marker: { key, label, color, dash }
// Colors chosen to be visually distinct from TRACE_COLORS used by Greeks.
const LIFECYCLE_MARKERS = [
  { key: 'firstTrade',  label: 'First trade',   color: '#94a3b8', dash: 'dot' },
  { key: 'expiration',  label: 'Expiration',     color: '#ef4444', dash: 'dash' },
  { key: 'atmCross',    label: 'ATM cross',      color: '#f59e0b', dash: 'dot' },
  { key: 'delta30',     label: '|Δ|=0.30',       color: '#10b981', dash: 'dot' },
  { key: 'delta50',     label: '|Δ|=0.50',       color: '#6366f1', dash: 'dot' },
  { key: 'delta70',     label: '|Δ|=0.70',       color: '#ec4899', dash: 'dot' },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function daysBetween(isoA, isoB) {
  if (!isoA || !isoB) return null;
  const a = new Date(`${isoA}T00:00:00`);
  const b = new Date(`${isoB}T00:00:00`);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return null;
  return Math.round((a.getTime() - b.getTime()) / 86400000);
}

/**
 * Per-row Greek extractor.
 *
 * For each ContractRowWithGreeks (Decision D — both *_stored raw fields AND
 * ComputeResult-wrapped fields are present), pull a numeric value if either
 * the stored scalar is non-null OR the ComputeResult resolved (stored/
 * computed). Returns null when the value is missing — Plotly will render
 * a gap rather than a fake-zero.
 *
 * The trace shape choice (chart trace shape for Greek overlays):
 * we render two traces per Greek when stored & computed both exist on
 * different rows is unnecessary — instead we render a SINGLE trace per
 * Greek where each point comes from `row[key]` (the ComputeResult wrapper).
 * Line style = solid when EVERY visible point is `source==='stored'`,
 * dashed when ANY point is `source==='computed'`. This keeps the chart
 * legible (one trace per Greek) while honouring the spec's
 * "stored solid / computed dashed" rule at the trace level.
 */
function extractGreekSeries(rows, key) {
  const xs = [];
  const ys = [];
  let anyComputed = false;
  let anyValue = false;
  for (const row of rows) {
    const cr = row && row[key];
    if (!cr) {
      xs.push(row && row.date);
      ys.push(null);
      continue;
    }
    if (cr.source === 'computed') anyComputed = true;
    const v =
      cr.source === 'missing' || cr.value === null || cr.value === undefined
        ? null
        : Number(cr.value);
    if (v !== null) anyValue = true;
    xs.push(row.date);
    ys.push(v);
  }
  return { xs, ys, anyComputed, anyValue };
}

/**
 * Computes life-cycle event dates from contract metadata + rows.
 *
 * Returns an object keyed by LIFECYCLE_MARKERS[*].key → ISO date string or null.
 * Null means the marker cannot be computed (data absent or threshold not reached).
 *
 * Delta source priority: `row.delta_stored` (scalar) → `row.delta.value` (ComputeResult).
 * If neither is available, delta thresholds are skipped for that row.
 */
function computeLifecycleDates(contract, rows) {
  const result = {
    firstTrade: null,
    expiration: null,
    atmCross: null,
    delta30: null,
    delta50: null,
    delta70: null,
  };

  if (!rows || rows.length === 0) return result;

  // 1. First trade date.
  result.firstTrade = rows[0].date || null;

  // 2. Expiration — from contract metadata (always known if contract loaded).
  if (contract && contract.expiration) {
    result.expiration = contract.expiration;
  }

  // 3. ATM cross — the row whose |K − S| is minimised.
  //    Requires underlying_price_stored on at least one row.
  const strike = contract ? contract.strike : null;
  if (strike != null) {
    let bestDist = Infinity;
    let bestDate = null;
    for (const row of rows) {
      const s = row.underlying_price_stored;
      if (s == null) continue;
      const dist = Math.abs(strike - s);
      if (dist < bestDist) {
        bestDist = dist;
        bestDate = row.date;
      }
    }
    result.atmCross = bestDate;
  }

  // 4–6. |Δ| threshold crossings — first date where |delta| >= threshold.
  const thresholds = [
    { key: 'delta30', level: 0.30 },
    { key: 'delta50', level: 0.50 },
    { key: 'delta70', level: 0.70 },
  ];

  for (const row of rows) {
    // Extract delta magnitude from stored scalar or ComputeResult.
    let absDelta = null;
    if (row.delta_stored != null) {
      absDelta = Math.abs(Number(row.delta_stored));
    } else if (row.delta && row.delta.source !== 'missing' && row.delta.value != null) {
      absDelta = Math.abs(Number(row.delta.value));
    }
    if (absDelta == null) continue;

    for (const { key, level } of thresholds) {
      if (result[key] === null && absDelta >= level) {
        result[key] = row.date;
      }
    }

    // Short-circuit once all thresholds are found.
    if (result.delta30 !== null && result.delta50 !== null && result.delta70 !== null) break;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export default function ContractDetailPanel({ collection, instrumentId, onClose }) {
  const [computeMissing, setComputeMissing] = useState(false);
  const [overlayState, setOverlayState] = useState({
    iv: false,
    delta: false,
    gamma: false,
    theta: false,
    vega: false,
  });
  // Life-cycle markers: off by default to avoid clutter (spec default).
  const [showMarkers, setShowMarkers] = useState(false);

  const { data, loading, error } = useContractSeries(collection, instrumentId, {
    computeMissing,
  });

  // useAsync resets state inside useEffect, which runs AFTER render — so the
  // first render after `instrumentId` changes still carries the old data and
  // would briefly paint the previous contract's chart. Detect that mismatch
  // here and treat the panel as loading until the new fetch lands. Without
  // this guard, switching contracts shows the old chart for one frame, which
  // is confusing when the user has already moved on.
  const dataIsStale =
    data && data.contract && data.contract.contract_id !== instrumentId;

  const contract = !dataIsStale && data && data.contract ? data.contract : null;
  const rows = !dataIsStale && data && Array.isArray(data.rows) ? data.rows : [];
  const showLoading = loading || dataIsStale;

  const { traces, layoutOverrides } = useMemo(() => {
    if (!rows || rows.length === 0) {
      return { traces: [], layoutOverrides: {} };
    }

    const dates = rows.map((r) => r.date);
    const traceList = [];

    // Mid price — primary y-axis.
    traceList.push({
      x: dates,
      y: rows.map((r) => (r.mid == null ? null : Number(r.mid))),
      type: 'scatter',
      mode: 'lines',
      name: 'Mid',
      line: { color: TRACE_COLORS[0], width: 1.5 },
      hovertemplate: '%{x}<br>Mid: %{y:,.4f}<extra></extra>',
    });

    // Volume — secondary y-axis (bars).
    traceList.push({
      x: dates,
      y: rows.map((r) => (r.volume == null ? null : Number(r.volume))),
      type: 'bar',
      name: 'Volume',
      yaxis: 'y2',
      marker: { color: 'rgba(14, 165, 233, 0.3)' },
      hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
    });

    // Greek overlays — tertiary y-axis (yaxis3) so they don't squash mid scale.
    let hasGreekOverlay = false;
    for (const g of GREEK_DEFS) {
      if (!overlayState[g.key]) continue;
      const { xs, ys, anyComputed, anyValue } = extractGreekSeries(rows, g.key);
      if (!anyValue) continue;
      hasGreekOverlay = true;
      traceList.push({
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'lines',
        name: g.label,
        yaxis: 'y3',
        line: {
          color: g.color,
          width: 1,
          // Stored Greeks → solid; computed Greeks → dashed (spec §6).
          dash: anyComputed ? 'dash' : 'solid',
        },
        connectgaps: false,
        hovertemplate: `%{x}<br>${g.label}: %{y:.4f}<extra></extra>`,
      });
    }

    // Life-cycle markers — each is a vertical line trace on a hidden y-axis (y4).
    // Uses the codebase's createVerticalLineTrace + hiddenOverlayAxis pattern.
    let hasMarkers = false;
    if (showMarkers) {
      const markerDates = computeLifecycleDates(contract, rows);
      for (const m of LIFECYCLE_MARKERS) {
        const d = markerDates[m.key];
        if (!d) continue; // threshold not reached or data absent — skip gracefully.
        hasMarkers = true;
        traceList.push(
          createVerticalLineTrace([d], {
            name: m.label,
            color: m.color,
            dash: m.dash,
            yaxisKey: 'y4',
          }),
        );
      }
    }

    const lo = {
      xaxis: { anchor: 'y2' },
      yaxis: {
        title: { text: 'Mid', font: { size: 11 } },
        domain: [0.28, 1.0],
      },
      yaxis2: {
        domain: [0, 0.2],
        zeroline: false,
        showgrid: true,
        title: { text: 'Volume', font: { size: 11 } },
        anchor: 'x',
      },
      ...(hasGreekOverlay
        ? {
            yaxis3: {
              overlaying: 'y',
              side: 'right',
              showgrid: false,
              title: { text: 'Greek', font: { size: 11 } },
            },
          }
        : {}),
      ...(hasMarkers
        ? {
            yaxis4: hiddenOverlayAxis(),
          }
        : {}),
    };

    return { traces: traceList, layoutOverrides: lo };
  }, [rows, overlayState, showMarkers, contract]);

  const dte = contract ? daysBetween(contract.expiration, todayISO()) : null;

  const showAmericanNote =
    contract && AMERICAN_EXERCISE_ROOTS.has(contract.root_underlying || collection);

  const dataRange =
    rows.length > 0
      ? `${rows[0].date} → ${rows[rows.length - 1].date}`
      : '—';

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{instrumentId}</h2>
        <button type="button" className={styles.closeButton} onClick={onClose}>
          Close
        </button>
      </div>

      <div className={styles.controls}>
        <label
          className={styles.toggle}
          title="Compute missing Greeks via Black-76 (Decision C: transient)"
        >
          <input
            type="checkbox"
            checked={computeMissing}
            onChange={(e) => setComputeMissing(e.target.checked)}
          />
          Compute missing
        </label>
        {GREEK_DEFS.map((g) => (
          <label key={g.key} className={styles.toggle}>
            <input
              type="checkbox"
              checked={!!overlayState[g.key]}
              onChange={(e) =>
                setOverlayState((prev) => ({ ...prev, [g.key]: e.target.checked }))
              }
            />
            {g.label}
          </label>
        ))}
        <label
          className={`${styles.toggle} ${styles.markerToggle}`}
          title="Show life-cycle events: first trade, expiration, ATM cross, |Δ| thresholds"
        >
          <input
            type="checkbox"
            checked={showMarkers}
            onChange={(e) => setShowMarkers(e.target.checked)}
          />
          Life-cycle
        </label>
      </div>

      {showLoading && <div className={styles.loading}>Loading contract series…</div>}
      {error && (
        <div className={styles.error}>
          Failed to load contract: {error.message || String(error)}
        </div>
      )}

      {!showLoading && !error && data && (
        <div className={styles.body}>
          <div className={styles.chartCol}>
            <div className={styles.chartCard}>
              {rows.length > 0 ? (
                <Chart
                  traces={traces}
                  layoutOverrides={layoutOverrides}
                  className={styles.chartWrapper}
                  downloadFilename={`${collection}-${instrumentId}`}
                />
              ) : (
                <div className={styles.empty}>No rows available.</div>
              )}
            </div>
          </div>

          <aside className={styles.sidebar}>
            <h3 className={styles.sidebarTitle}>Contract</h3>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Strike</span>
              <span className={styles.metaValue}>
                {contract && contract.strike != null
                  ? Number(contract.strike).toFixed(2)
                  : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Type</span>
              <span className={styles.metaValue}>
                {contract && contract.type === 'C'
                  ? 'Call'
                  : contract && contract.type === 'P'
                  ? 'Put'
                  : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Expiration</span>
              <span className={styles.metaValue}>
                {contract ? contract.expiration : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>DTE</span>
              <span className={styles.metaValue}>{dte == null ? '—' : `${dte} d`}</span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Root</span>
              <span className={styles.metaValue}>
                {contract ? contract.root_underlying : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Cycle</span>
              <span className={styles.metaValue}>
                {contract && contract.expiration_cycle
                  ? contract.expiration_cycle
                  : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Provider</span>
              <span className={styles.metaValue}>
                {contract ? contract.provider : '—'}
              </span>
            </div>
            <div className={styles.metaRow}>
              <span className={styles.metaKey}>Data range</span>
              <span className={styles.metaValue}>{dataRange}</span>
            </div>
            {showAmericanNote && (
              <div className={styles.metaRow}>
                <span className={styles.metaKey}>Exercise</span>
                <span className={styles.metaValue}>
                  American (note: pricing kernel is European)
                </span>
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
