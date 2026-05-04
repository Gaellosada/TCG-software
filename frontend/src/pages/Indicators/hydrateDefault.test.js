// Unit coverage for ``hydrateDefault`` and ``applyDefaultSeries``.
//
// The two functions are thin but carry the Wave 2c routing rule:
// per-label registry-declared defaultSeries take precedence over the
// ambient resolved-index instrument. The IndicatorsPage smoke tests
// only cover the whole-page hydrate path, so the per-label fallback
// ordering is asserted directly here.

import { describe, it, expect } from 'vitest';
import { hydrateDefault, applyDefaultSeries } from './hydrateDefault';

// A throw-away registry-shaped def factory.
function makeDef(overrides = {}) {
  return {
    id: 'mock',
    name: 'Mock',
    readonly: true,
    category: 'volatility',
    chartShape: 'time-series',
    code: "def compute(series):\n    return series['close']",
    params: {},
    seriesMap: {},
    ownPanel: false,
    ...overrides,
  };
}

describe('hydrateDefault', () => {
  it('propagates registry defaultSeries onto the hydrated indicator', () => {
    const optionRef = {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'next_third_friday', offset_months: 0 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
    };
    const def = makeDef({
      code: "def compute(series):\n    return series['atm_iv']",
      defaultSeries: { atm_iv: optionRef },
    });
    const hydrated = hydrateDefault(def, undefined);
    expect(hydrated.defaultSeries).toEqual({ atm_iv: optionRef });
    // Hydrated seriesMap is populated with null per parsed label —
    // the actual auto-fill happens later in ``applyDefaultSeries``.
    expect(hydrated.seriesMap).toEqual({ atm_iv: null });
  });

  it('omits defaultSeries when the registry entry has none', () => {
    const def = makeDef();
    const hydrated = hydrateDefault(def, undefined);
    expect(hydrated.defaultSeries).toBeUndefined();
  });
});

describe('applyDefaultSeries — Wave 2c routing', () => {
  const optionRef = {
    type: 'option_stream',
    collection: 'OPT_SP_500',
    option_type: 'C',
    cycle: null,
    maturity: { kind: 'next_third_friday', offset_months: 0 },
    selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
    stream: 'iv',
  };
  const indexDefault = { collection: 'IDX', instrument_id: 'SPX' };

  it('prefers per-label registry defaultSeries over the index resolver', () => {
    const ind = {
      seriesMap: { atm_iv: null },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, indexDefault);
    // The atm_iv slot got the registry-declared option_stream ref
    // — NOT a SpotInstrumentRef built from indexDefault.
    expect(next.seriesMap.atm_iv).toEqual(optionRef);
  });

  it('falls back to the index resolver when no registry default for a label', () => {
    const ind = {
      seriesMap: { close: null },
      // No defaultSeries field.
    };
    const next = applyDefaultSeries(ind, indexDefault);
    expect(next.seriesMap.close).toEqual({
      type: 'spot',
      collection: 'IDX',
      instrument_id: 'SPX',
    });
  });

  it('mixes per-label and ambient routes within one indicator', () => {
    const ind = {
      seriesMap: { atm_iv: null, close: null },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, indexDefault);
    expect(next.seriesMap.atm_iv).toEqual(optionRef);
    expect(next.seriesMap.close).toEqual({
      type: 'spot',
      collection: 'IDX',
      instrument_id: 'SPX',
    });
  });

  it('leaves user-picked slots alone on non-readonly indicators', () => {
    const userPick = { type: 'spot', collection: 'IDX', instrument_id: 'NDX' };
    const ind = {
      readonly: false,
      seriesMap: { atm_iv: userPick },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, indexDefault);
    expect(next.seriesMap.atm_iv).toBe(userPick);
  });

  it('overwrites stale saved slots on readonly indicators when defaultSeries exists', () => {
    // When the developer changes a default (e.g., cycle: 'M' → 'W3 Friday'),
    // the registry default must win over any stale saved state.
    const staleRef = {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: 'M', // OLD default
      maturity: { kind: 'next_third_friday', offset_months: 0 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
    };
    const ind = {
      readonly: true,
      seriesMap: { atm_iv: staleRef },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, indexDefault);
    // Registry default wins — stale 'M' cycle replaced with the registry ref.
    expect(next.seriesMap.atm_iv).toEqual(optionRef);
  });

  it('returns the same object when no fill source is available', () => {
    const ind = { seriesMap: { close: null } };
    // No defaultSeries on indicator; no indexDefault passed.
    const next = applyDefaultSeries(ind, null);
    expect(next).toBe(ind);
  });

  it('still fills option-only labels when index resolver is unavailable', () => {
    // Wave 2c key behaviour: the index-resolver failing must NOT block
    // option-native indicators from auto-binding to their declared
    // defaults. (Note: the current page-level effect short-circuits if
    // indexDefault is null, so this is asserted at the unit boundary;
    // upgrading the page effect is Wave 3 territory.)
    const ind = {
      seriesMap: { atm_iv: null },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, null);
    expect(next.seriesMap.atm_iv).toEqual(optionRef);
  });

  it('does not match a label from defaultSeries to a different label', () => {
    // Routing is keyed by exact label match — no glob, no fallthrough.
    const ind = {
      seriesMap: { other_label: null },
      defaultSeries: { atm_iv: optionRef },
    };
    const next = applyDefaultSeries(ind, indexDefault);
    // ``other_label`` falls through to the index-resolver default.
    expect(next.seriesMap.other_label).toEqual({
      type: 'spot',
      collection: 'IDX',
      instrument_id: 'SPX',
    });
  });
});
