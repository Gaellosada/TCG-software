import { fetchApi } from './client';

export async function listCollections(assetClass = null) {
  const params = assetClass ? `?asset_class=${assetClass}` : '';
  const res = await fetchApi(`/data/collections${params}`);
  return res.collections;
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
  const res = await fetchApi(`/data/${collection}/${instrumentId}${query}`);
  return res; // { dates, open, high, low, close, volume }
}
