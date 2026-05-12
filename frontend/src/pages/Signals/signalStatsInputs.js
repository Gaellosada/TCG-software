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
  return { dates, equity };
}
