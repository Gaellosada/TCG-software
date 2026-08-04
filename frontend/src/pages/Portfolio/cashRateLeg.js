// Cash / financing accrual leg (feature F4, SPEC §5.7).
//
// A cash-rate leg earns a short interest rate on cash collateral: near-zero
// vol, all-positive drift. It is NOT chosen through the instrument picker (it
// references no market instrument on the FLAT source) — it is a
// configuration-only leg with a pluggable RATE SOURCE:
//
//   - flat:   a constant annual rate (%/yr). Zero data dependency; the default
//             and, as of build time, the ONLY operative source — the dwh has no
//             USD short-rate series (probe found only VIX-family indices), so a
//             ``series`` source awaits Gael loading the instrument (P-DATA-2).
//   - series: read an annualized-rate instrument (collection, symbol) from the
//             warehouse; ``rate_pct`` is the fallback before the series begins.
//
// The in-memory leg stores its source under ``cash_rate`` in the SAME snake_case
// shape the backend ``LegSpec.cash_rate`` (CashRateSpec) expects, so it
// round-trips through persistence (legsToWire / persistedDocToLegs) unchanged.

/** Default rate source: flat 1 %/yr (legacy §5.7 behaviour). */
export const DEFAULT_CASH_RATE_SOURCE = Object.freeze({
  kind: 'flat',
  rate_pct: 1.0,
  unit: 'percent',
  compound: true,
});

/** A fresh cash-rate leg with a default label, +100 weight and flat 1 %/yr. */
export function makeCashRateLeg() {
  return {
    type: 'cash_rate',
    label: 'Cash (USD 1M rate)',
    weight: 100,
    cash_rate: { ...DEFAULT_CASH_RATE_SOURCE },
  };
}

/**
 * The clean wire ``cash_rate`` object for the compute body / persistence.
 * Emits ONLY the fields the active source needs so a flat leg stays minimal and
 * a series leg carries its instrument reference. Falls back to the flat default
 * when a leg has no ``cash_rate`` (legacy / hand-built).
 */
export function cashRateApiSpec(leg) {
  const src = (leg && leg.cash_rate) || DEFAULT_CASH_RATE_SOURCE;
  const kind = src.kind === 'series' ? 'series' : 'flat';
  // Empty string (a blanked editor) or non-finite -> fall back to 1.0. Guard the
  // empty case explicitly since Number('') === 0 (a finite, wrong value).
  const rawRate = src.rate_pct;
  const ratePct =
    rawRate === '' || rawRate === null || rawRate === undefined || !Number.isFinite(Number(rawRate))
      ? 1.0
      : Number(rawRate);
  const compound = src.compound !== false; // default true
  if (kind === 'series') {
    return {
      kind: 'series',
      collection: src.collection || null,
      symbol: src.symbol || null,
      unit: src.unit === 'fraction' ? 'fraction' : 'percent',
      rate_pct: ratePct,
      compound,
    };
  }
  return { kind: 'flat', rate_pct: ratePct, compound };
}

/** A short, human-readable summary of a cash leg's rate source. */
export function cashRateSummary(leg) {
  const src = (leg && leg.cash_rate) || DEFAULT_CASH_RATE_SOURCE;
  if (src.kind === 'series') {
    const ref = [src.collection, src.symbol].filter(Boolean).join('/') || '(unset)';
    return `series ${ref}`;
  }
  const pct = Number.isFinite(Number(src.rate_pct)) ? Number(src.rate_pct) : 1.0;
  return `flat ${pct.toFixed(2)}%/yr`;
}
