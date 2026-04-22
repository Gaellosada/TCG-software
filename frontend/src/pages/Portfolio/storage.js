// Saved-portfolio persistence — localStorage-backed. Storage key is
// 'tcg-saved-portfolios'; do not rename without a user-state migration.
//
// Each save overwrites the entry under ``name``. The in-memory shape
// persisted per entry is:
//   { legs: Array<LegShape>, weights: Record<label, number>,
//     rebalance: string, savedAt: string }
// where LegShape carries every field the hook needs to restore the
// leg — signal-specific fields (signalId, signalName, signalSpec)
// default to null for non-signal legs.

export const STORAGE_KEY = 'tcg-saved-portfolios';

function readAll() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
  catch { saved = {}; }
  return saved;
}

function writeAll(saved) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); }
  catch { /* quota exceeded or sandboxed storage — the in-memory save still succeeds */ }
}

export function savePortfolio(name, { legs, rebalance }) {
  const saved = readAll();
  const weightsDict = {};
  for (const l of legs) weightsDict[l.label] = Number(l.weight) || 0;
  saved[name] = {
    legs: legs.map((l) => ({
      label: l.label,
      type: l.type,
      collection: l.collection,
      symbol: l.symbol,
      strategy: l.strategy,
      adjustment: l.adjustment,
      cycle: l.cycle,
      rollOffset: l.rollOffset,
      weight: l.weight,
      // Signal-specific fields (null for non-signal legs).
      signalId: l.signalId || null,
      signalName: l.signalName || null,
      signalSpec: l.signalSpec || null,
    })),
    weights: weightsDict,
    rebalance,
    savedAt: new Date().toISOString(),
  };
  writeAll(saved);
}

export function loadPortfolio(name) {
  const saved = readAll();
  return saved[name] || null;
}

export function deleteSavedPortfolio(name) {
  const saved = readAll();
  delete saved[name];
  writeAll(saved);
}

export function getSavedPortfolios() {
  const saved = readAll();
  return Object.keys(saved);
}
