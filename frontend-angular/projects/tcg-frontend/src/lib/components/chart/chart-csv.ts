/**
 * CSV builder + downloader for Plotly traces. Pure TS port of React's
 * `utils/chartCsv.js` (with TypeScript types). Visibility rules:
 *   - `visible === false` or `'legendonly'` → excluded;
 *   - `hoverinfo === 'skip'` → excluded;
 *   - `meta.skipCsv === true` → excluded (marker overlays opt out).
 * Supports scatter/line/bar (`y`) and candlestick/ohlc (OHLC columns).
 */

export type CsvTrace = TcgCsvTrace;

export interface TcgCsvTrace {
  x?: ReadonlyArray<string | number | null>;
  y?: ReadonlyArray<unknown>;
  open?: ReadonlyArray<unknown>;
  high?: ReadonlyArray<unknown>;
  low?: ReadonlyArray<unknown>;
  close?: ReadonlyArray<unknown>;
  name?: string;
  type?: string;
  visible?: boolean | 'legendonly';
  hoverinfo?: string;
  meta?: { skipCsv?: boolean };
}

function isExportable(trace: TcgCsvTrace | null | undefined): trace is TcgCsvTrace {
  if (!trace) return false;
  if (trace.visible === false || trace.visible === 'legendonly') return false;
  if (trace.hoverinfo === 'skip') return false;
  if (trace.meta && trace.meta.skipCsv === true) return false;
  const type = trace.type;
  if (type === 'candlestick' || type === 'ohlc') {
    return Array.isArray(trace.x) && Array.isArray(trace.close);
  }
  return Array.isArray(trace.x) && Array.isArray(trace.y);
}

const INJECTION_PREFIX = /^[=+@\t\r]/;

function escapeCsv(val: unknown): string {
  if (val == null) return '';
  if (typeof val === 'number' && !Number.isFinite(val)) return '';
  let s = String(val);
  if (INJECTION_PREFIX.test(s)) s = "'" + s;
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function uniqueName(base: string, usedNames: Set<string>): string {
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

export function buildCsv(traces: ReadonlyArray<TcgCsvTrace> | null | undefined): string {
  if (!Array.isArray(traces)) return '';
  const exportable = traces.filter(isExportable);
  if (exportable.length === 0) return '';

  const usedNames = new Set<string>();
  interface SeriesCol {
    header: string;
    values: ReadonlyArray<unknown>;
  }
  interface Series {
    x: ReadonlyArray<string | number | null>;
    cols: SeriesCol[];
  }
  const series: Series[] = [];

  exportable.forEach((t, idx) => {
    const base = (t.name && String(t.name).trim()) || `series_${idx + 1}`;
    const name = uniqueName(base, usedNames);
    const x = (t.x || []) as ReadonlyArray<string | number | null>;
    if (t.type === 'candlestick' || t.type === 'ohlc') {
      series.push({
        x,
        cols: [
          { header: `${name}_open`, values: t.open || [] },
          { header: `${name}_high`, values: t.high || [] },
          { header: `${name}_low`, values: t.low || [] },
          { header: `${name}_close`, values: t.close || [] },
        ],
      });
    } else {
      series.push({ x, cols: [{ header: name, values: t.y || [] }] });
    }
  });

  const xSet = new Set<string | number | null>();
  for (const s of series) for (const x of s.x) xSet.add(x);
  const xSorted = Array.from(xSet).sort();

  const lookups = series.map((s) => {
    const m = new Map<string | number | null, number>();
    for (let i = 0; i < s.x.length; i++) m.set(s.x[i], i);
    return m;
  });

  const headers = ['date', ...series.flatMap((s) => s.cols.map((c) => c.header))];
  const rows = [headers.map(escapeCsv).join(',')];

  for (const x of xSorted) {
    const row: unknown[] = [x];
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

function sanitizeFilename(raw: unknown): string {
  const s = raw == null ? '' : String(raw).trim();
  if (!s) return 'chart';
  // eslint-disable-next-line no-control-regex
  const cleaned = s.replace(/[\\/\x00-\x1f]/g, '_').slice(0, 120);
  return cleaned || 'chart';
}

export function downloadCsv(csv: string, filename: string): void {
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
