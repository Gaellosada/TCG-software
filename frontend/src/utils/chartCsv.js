/**
 * Build a CSV of the currently-visible traces on a Plotly chart and trigger a download.
 *
 * Visibility is read from the live Plotly trace objects (graphDiv.data):
 *   - `visible === false` or `'legendonly'` → excluded
 *   - `hoverinfo === 'skip'` → excluded (decorative overlays like rebalance markers)
 *
 * Supports scatter/line/bar (single `y`) and candlestick/ohlc (4 OHLC columns).
 * The x-axis of all included traces is unioned and sorted to form the row index.
 */

function isExportable(trace) {
  if (!trace) return false;
  if (trace.visible === false || trace.visible === 'legendonly') return false;
  if (trace.hoverinfo === 'skip') return false;
  const type = trace.type;
  if (type === 'candlestick' || type === 'ohlc') {
    return Array.isArray(trace.x) && Array.isArray(trace.close);
  }
  return Array.isArray(trace.x) && Array.isArray(trace.y);
}

// Guard against CSV formula injection when the value would be interpreted as
// a formula by Excel/Sheets. We skip leading `-` because negative numbers are
// valid data in this app (prices, returns) and should not be coerced to text.
const INJECTION_PREFIX = /^[=+@\t\r]/;

function escapeCsv(val) {
  if (val == null) return '';
  // Non-finite numbers (NaN, Infinity) export as empty so downstream
  // parsers don't have to strip the literal string "NaN".
  if (typeof val === 'number' && !Number.isFinite(val)) return '';
  let s = String(val);
  if (INJECTION_PREFIX.test(s)) s = "'" + s;
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function uniqueName(base, usedNames) {
  if (!usedNames.has(base)) {
    usedNames.add(base);
    return base;
  }
  let n = 2;
  while (usedNames.has(`${base}_${n}`)) n++;
  const name = `${base}_${n}`;
  usedNames.add(name);
  return name;
}

/**
 * Build a CSV string from an array of Plotly traces.
 * Returns an empty string if no traces are exportable.
 */
export function buildCsv(traces) {
  if (!Array.isArray(traces)) return '';
  const exportable = traces.filter(isExportable);
  if (exportable.length === 0) return '';

  const usedNames = new Set();
  const series = [];

  exportable.forEach((t, idx) => {
    const base = (t.name && String(t.name).trim()) || `series_${idx + 1}`;
    const name = uniqueName(base, usedNames);
    if (t.type === 'candlestick' || t.type === 'ohlc') {
      series.push({
        x: t.x,
        cols: [
          { header: `${name}_open`, values: t.open || [] },
          { header: `${name}_high`, values: t.high || [] },
          { header: `${name}_low`, values: t.low || [] },
          { header: `${name}_close`, values: t.close || [] },
        ],
      });
    } else {
      series.push({
        x: t.x,
        cols: [{ header: name, values: t.y }],
      });
    }
  });

  // Union x values across series, sorted (lexicographic works for ISO dates).
  const xSet = new Set();
  for (const s of series) for (const x of s.x) xSet.add(x);
  const xSorted = Array.from(xSet).sort();

  // Per-series index: x → position
  const lookups = series.map((s) => {
    const m = new Map();
    for (let i = 0; i < s.x.length; i++) m.set(s.x[i], i);
    return m;
  });

  const headers = ['date', ...series.flatMap((s) => s.cols.map((c) => c.header))];
  const rows = [headers.map(escapeCsv).join(',')];

  for (const x of xSorted) {
    const row = [x];
    for (let si = 0; si < series.length; si++) {
      const pos = lookups[si].get(x);
      for (const col of series[si].cols) {
        row.push(pos != null ? col.values[pos] : '');
      }
    }
    rows.push(row.map(escapeCsv).join(','));
  }

  return rows.join('\n') + '\n';
}

function sanitizeFilename(raw) {
  const s = raw == null ? '' : String(raw).trim();
  if (!s) return 'chart';
  // Strip control chars and path separators; browsers mostly normalize these
  // but we future-proof against surprises and clamp length.
  const cleaned = s.replace(/[\\/\x00-\x1f]/g, '_').slice(0, 120);
  return cleaned || 'chart';
}

/**
 * Trigger a browser download of the given CSV string.
 */
export function downloadCsv(csv, filename) {
  const safe = sanitizeFilename(filename);
  const final = safe.toLowerCase().endsWith('.csv') ? safe : `${safe}.csv`;
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = final;
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    a.remove();
    URL.revokeObjectURL(url);
  }
}
