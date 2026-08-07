// Cash / financing accrual leg (feature F4, SPEC §5.7).
//
// A cash-rate leg earns a short interest rate on cash collateral: near-zero
// vol, all-positive drift. It is a REAL rate instrument read from the warehouse
// — a v2-only ``RATE`` series (e.g. RATE_US_CMT_1M, the FRED DGS1MO 1-month
// Treasury CMT rate). The leg is created through the instrument picker's "Rate"
// tab (there is no flat, made-up %/yr input — the rate comes from the data).
//
// The in-memory leg stores its source under ``cash_rate`` in the SAME snake_case
// shape the backend ``LegSpec.cash_rate`` (CashRateSpec) expects, so it
// round-trips through persistence (legsToWire / persistedDocToLegs) unchanged.
// The leg also carries ``dataSource: 'v2'`` (rates are a v2-only object); the
// compute builder stamps ``data_source: 'v2'`` on the wire leg.

/** The v2 rate collection + the default 1-month CMT rate symbol. */
export const RATE_COLLECTION = 'RATE';
export const RATE_1M_SYMBOL = 'RATE_US_CMT_1M';

/**
 * Default (fallback) rate source: the v2 1-month CMT Treasury series, read as
 * an annualized percent and 252-day compounded. Used as the display/emit
 * fallback for a cash leg that somehow carries no explicit ``cash_rate``.
 */
export const DEFAULT_CASH_RATE_SOURCE = Object.freeze({
  collection: RATE_COLLECTION,
  symbol: RATE_1M_SYMBOL,
  unit: 'percent',
  compound: true,
});

/**
 * The clean wire ``cash_rate`` object for the compute body / persistence.
 * Series-only: the rate is a real instrument reference (collection + symbol),
 * read as percent (÷100) and compounded. Falls back to the default RATE ref
 * when a leg carries no ``cash_rate`` (legacy / hand-built).
 */
export function cashRateApiSpec(leg) {
  const src = (leg && leg.cash_rate) || DEFAULT_CASH_RATE_SOURCE;
  return {
    collection: src.collection || RATE_COLLECTION,
    symbol: src.symbol || RATE_1M_SYMBOL,
    unit: src.unit === 'fraction' ? 'fraction' : 'percent',
    compound: src.compound !== false, // default true
  };
}

/** A short, human-readable summary of a cash leg's rate source. */
export function cashRateSummary(leg) {
  const src = (leg && leg.cash_rate) || DEFAULT_CASH_RATE_SOURCE;
  const ref = [src.collection, src.symbol].filter(Boolean).join('/') || '(unset)';
  return `rate ${ref}`;
}
