// Asset-type vocabulary for indicators (frontend side).
//
// Single source of truth on the FE for the three asset types an indicator
// can be declared compatible with. The matching Python module lives at
// ``tcg/core/indicators/asset_types.py`` and is kept in sync by the parity
// test ``tests/api/test_asset_type_parity.py`` — fail loud if either side
// gains or drops a value.
//
// Conventions
// -----------
//   * Lowercase string literals (``'index'``, ``'equity'``, ``'option'``)
//     are the canonical wire/storage form. Use them in JSON, defaults files,
//     and request bodies.
//   * UPPERCASE constants (``INDEX``, ``EQUITY``, ``OPTION``) are exported
//     for use in code so that no caller hard-codes a magic string. Sign 4
//     (no magic asset-type strings) — always import the constant.
//   * ``ASSET_TYPES`` is a frozen tuple of all three values, useful for
//     enumerations (e.g. validating an incoming string).
//
// Collection-classification heuristic (mirrors ``InstrumentPickerModal``
// and the ``CategoryBrowser``):
//   * ``INDEX`` collection           → 'index'
//   * ``OPT_*`` collection prefix    → 'option'
//   * ``ETF`` / ``FOREX`` / ``FUND`` → 'equity'
//   * ``FUT_*`` collection prefix    → 'equity' (continuous-future stream)
//   * anything else / null SeriesRef → null (caller decides what to do)
//
// IMPORTANT: ``inferAssetType`` is exported and unit-tested but is NOT
// called from production code paths in this wave (Wave 2a is metadata
// only). Wave 2b/3 wire it into request building / runGate. Keep it pure
// (no I/O, no side-effects) so it stays trivially testable.

/** @type {'index'} */
export const INDEX = 'index';
/** @type {'equity'} */
export const EQUITY = 'equity';
/** @type {'option'} */
export const OPTION = 'option';

/**
 * Frozen tuple of every asset-type literal known to the FE. The matching
 * Python ``ASSET_TYPES`` frozenset must contain exactly the same values —
 * the parity test enforces this.
 *
 * @type {readonly ['index', 'equity', 'option']}
 */
export const ASSET_TYPES = Object.freeze([INDEX, EQUITY, OPTION]);

/**
 * Infer the asset type of a SeriesRef-shaped value based on its
 * ``collection`` field. Returns ``null`` for unknown / unrecognised
 * inputs — never undefined, never an empty string. The null is meaningful:
 * it tells callers "we cannot classify this stream, route accordingly".
 *
 * Accepts both shapes of ``SeriesRef``:
 *   * Spot:       ``{ type: 'spot', collection, instrument_id }``
 *   * Continuous: ``{ type: 'continuous', collection, adjustment, ... }``
 *
 * Anything missing a ``collection`` string returns ``null``.
 *
 * @param {object|null|undefined} seriesRef
 * @returns {'index' | 'equity' | 'option' | null}
 */
export function inferAssetType(seriesRef) {
  if (seriesRef == null || typeof seriesRef !== 'object') return null;
  const collection = seriesRef.collection;
  if (typeof collection !== 'string' || collection.length === 0) return null;

  if (collection === 'INDEX') return INDEX;
  if (collection.startsWith('OPT_')) return OPTION;
  if (collection.startsWith('FUT_')) return EQUITY;
  if (collection === 'ETF' || collection === 'FOREX' || collection === 'FUND') {
    return EQUITY;
  }
  return null;
}
