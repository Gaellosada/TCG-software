import { fetchApi } from './client';

export async function listCollections(assetClass = null) {
  const params = assetClass ? `?asset_class=${assetClass}` : '';
  const res = await fetchApi(`/data/collections${params}`);
  return res.collections || [];
}

export async function listInstruments(collection, { skip = 0, limit = 50 } = {}) {
  const res = await fetchApi(`/data/${collection}?skip=${skip}&limit=${limit}`);
  return res; // { items, total, skip, limit }
}

export async function getInstrumentPrices(collection, instrumentId, { start, end, provider } = {}) {
  const params = new URLSearchParams();
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  if (provider) params.set('provider', provider);
  const query = params.toString() ? `?${params}` : '';
  const res = await fetchApi(`/data/${encodeURIComponent(collection)}/${encodeURIComponent(instrumentId)}${query}`);
  return res; // { dates, open, high, low, close, volume }
}

export async function getContinuousSeries(collection, { strategy = 'front_month', adjustment = 'none', cycle, rollOffset, start, end } = {}) {
  const params = new URLSearchParams();
  params.set('strategy', strategy);
  params.set('adjustment', adjustment);
  if (cycle) params.set('cycle', cycle);
  if (rollOffset > 0) params.set('roll_offset', String(rollOffset));
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const res = await fetchApi(`/data/continuous/${collection}?${params}`);
  return res;
}

export async function getAvailableCycles(collection) {
  const res = await fetchApi(`/data/continuous/${collection}/cycles`);
  return res.cycles || [];
}
