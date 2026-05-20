import { aggregateRealizedPnl } from './resultsPlotTraces';

function unixMsToYYYYMMDD(timestamps) {
  if (!Array.isArray(timestamps)) return null;
  const out = new Array(timestamps.length);
  for (let i = 0; i < timestamps.length; i++) {
    const ms = timestamps[i];
    if (!Number.isFinite(ms)) return null;
    const d = new Date(ms);
    const y = d.getUTCFullYear();
    const m = d.getUTCMonth() + 1;
    const day = d.getUTCDate();
    out[i] = y * 10000 + m * 100 + day;
  }
  return out;
}

export function buildSignalStatsInputs(result, capital) {
  if (!result || !Array.isArray(result.timestamps)) return null;
  const dates = unixMsToYYYYMMDD(result.timestamps);
  if (!dates || dates.length < 2) return null;
  const pnlRaw = aggregateRealizedPnl(result.realized_pnl, result.timestamps.length);
  if (!pnlRaw) return null;
  const cap = Number.isFinite(capital) ? capital : 1;
  const equity = pnlRaw.map((v) => cap + v * cap);
  // Backend rejects non-finite or non-positive equity. A losing signal
  // can drive equity to zero or below — short-circuit here so the
  // Statistics panel is never mounted in that pathological case, instead
  // of letting the user see a backend validation error inside the panel.
  if (equity.some((v) => !Number.isFinite(v) || v <= 0)) return null;
  return { dates, equity };
}
