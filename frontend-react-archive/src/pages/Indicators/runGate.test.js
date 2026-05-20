import { describe, it, expect } from 'vitest';
import {
  areAllSlotsFilled,
  computeRunDisabledReason,
  deriveAssetTypeFromSeriesMap,
  computeAssetCompatibility,
  computeOptionStreamSanity,
  runGateForBackendError,
} from './runGate';

const SPOT_INDEX = { type: 'spot', collection: 'INDEX', instrument_id: 'IND_SP_500' };
const SPOT_OPTION = { type: 'spot', collection: 'OPT_SPX', instrument_id: 'SPXW 250101' };
const SPOT_EQUITY = { type: 'spot', collection: 'ETF', instrument_id: 'SPY' };
const CONT_FUT = { type: 'continuous', collection: 'FUT_ES', adjustment: 'none' };

const SMA_CODE = 'def compute(series, window: int = 20):\n    s = series["close"]\n    return s';

function makeInd({ code = SMA_CODE, seriesMap = {}, compatibleAssetTypes } = {}) {
  const ind = { id: 'x', name: 'X', code, params: {}, seriesMap };
  if (compatibleAssetTypes !== undefined) ind.compatibleAssetTypes = compatibleAssetTypes;
  return ind;
}

describe('areAllSlotsFilled', () => {
  it('false with no indicator', () => {
    expect(areAllSlotsFilled(null, ['close'])).toBe(false);
  });

  it('false when no labels declared (defensive)', () => {
    expect(areAllSlotsFilled(makeInd(), [])).toBe(false);
  });

  it('false when a slot is null', () => {
    expect(areAllSlotsFilled(makeInd({ seriesMap: { close: null } }), ['close'])).toBe(false);
  });

  it('true with a fully-populated spot slot', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: SPOT_INDEX } }),
      ['close'],
    )).toBe(true);
  });

  it('true with a continuous slot lacking instrument_id', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: CONT_FUT } }),
      ['close'],
    )).toBe(true);
  });

  it('false on a spot slot missing instrument_id', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: { type: 'spot', collection: 'INDEX' } } }),
      ['close'],
    )).toBe(false);
  });
});

describe('deriveAssetTypeFromSeriesMap', () => {
  it('returns null when seriesMap is empty', () => {
    expect(deriveAssetTypeFromSeriesMap({})).toEqual({ ok: true, asset_type: null });
  });

  it('returns null when seriesMap is missing entirely', () => {
    expect(deriveAssetTypeFromSeriesMap(null)).toEqual({ ok: true, asset_type: null });
    expect(deriveAssetTypeFromSeriesMap(undefined)).toEqual({ ok: true, asset_type: null });
  });

  it('returns the unique inferred type when slots agree', () => {
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX })).toEqual({ ok: true, asset_type: 'index' });
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_OPTION })).toEqual({ ok: true, asset_type: 'option' });
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_EQUITY, b: CONT_FUT })).toEqual({ ok: true, asset_type: 'equity' });
  });

  it('returns slot_conflict when slots disagree', () => {
    const out = deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX, b: SPOT_OPTION });
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('slot_conflict');
    expect(out.types.sort()).toEqual(['index', 'option']);
  });

  it('skips null slots without conflict', () => {
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX, b: null })).toEqual({ ok: true, asset_type: 'index' });
  });

  it('skips slots whose collection is unknown (returns null type)', () => {
    expect(deriveAssetTypeFromSeriesMap({
      a: SPOT_INDEX,
      b: { type: 'spot', collection: 'CRYPTO', instrument_id: 'BTC' },
    })).toEqual({ ok: true, asset_type: 'index' });
  });
});

describe('computeAssetCompatibility', () => {
  it('returns ok for a null indicator', () => {
    expect(computeAssetCompatibility(null)).toEqual({ ok: true });
  });

  it('returns ok when compatibleAssetTypes is missing (back-compat)', () => {
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION } }),
    )).toEqual({ ok: true });
  });

  it('returns ok when compatibleAssetTypes is an empty array', () => {
    // Empty array is treated as "no compat declared" — never silently
    // blocking forever from a misconfigured registry.
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION }, compatibleAssetTypes: [] }),
    )).toEqual({ ok: true });
  });

  it('returns ok when slots agree and the type is in the compat list', () => {
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_INDEX }, compatibleAssetTypes: ['index', 'equity'] }),
    )).toEqual({ ok: true });
  });

  it('rejects with incompatible_asset when slot type is not in compat list', () => {
    const out = computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION }, compatibleAssetTypes: ['index', 'equity'] }),
    );
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('incompatible_asset');
    expect(out.asset_type).toBe('option');
    expect(out.accepted_asset_types).toEqual(['index', 'equity']);
  });

  it('propagates slot_conflict from underlying derivation', () => {
    const out = computeAssetCompatibility(
      makeInd({
        seriesMap: { a: SPOT_INDEX, b: SPOT_OPTION },
        compatibleAssetTypes: ['index'],
      }),
    );
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('slot_conflict');
  });

  it('returns ok when seriesMap has no inferable type (defer to backend)', () => {
    expect(computeAssetCompatibility(
      makeInd({
        seriesMap: { close: { type: 'spot', collection: 'CRYPTO', instrument_id: 'BTC' } },
        compatibleAssetTypes: ['index'],
      }),
    )).toEqual({ ok: true });
  });
});

describe('computeRunDisabledReason', () => {
  it('asks to select an indicator first', () => {
    expect(computeRunDisabledReason(null, ['close'])).toBe('Select an indicator first');
  });

  it('asks to fill empty slot', () => {
    expect(computeRunDisabledReason(
      makeInd({ seriesMap: { close: null } }),
      ['close'],
    )).toBe('Fill series slot: close');
  });

  it('reports asset-type incompatibility once slots are filled', () => {
    const reason = computeRunDisabledReason(
      makeInd({
        seriesMap: { close: SPOT_OPTION },
        compatibleAssetTypes: ['index', 'equity'],
      }),
      ['close'],
    );
    expect(reason).toContain('Requires');
    expect(reason).toContain('index');
    expect(reason).toContain('equity');
    expect(reason).toContain('option');
  });

  it('reports slot_conflict when slots disagree', () => {
    const reason = computeRunDisabledReason(
      makeInd({
        seriesMap: { a: SPOT_INDEX, b: SPOT_OPTION },
        compatibleAssetTypes: ['index'],
        code: 'def compute(series):\n    return series["a"] + series["b"]',
      }),
      ['a', 'b'],
    );
    expect(reason).toContain('disagree');
    expect(reason).toContain('index');
    expect(reason).toContain('option');
  });

  it('reports tautological option_stream selection in the run-disabled tooltip', () => {
    const tautologicalRef = {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: 0.25 },
      stream: 'delta',
    };
    const reason = computeRunDisabledReason(
      makeInd({
        seriesMap: { close: tautologicalRef },
        compatibleAssetTypes: ['option'],
      }),
      ['close'],
    );
    expect(reason).toContain('tautological');
    expect(reason).toContain('close');
  });
});

describe('computeOptionStreamSanity', () => {
  const HEALTHY_STREAM_REF = {
    type: 'option_stream',
    collection: 'OPT_SP_500',
    option_type: 'C',
    cycle: null,
    maturity: { kind: 'nearest_to_target', target_days: 30 },
    selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
    stream: 'iv',
  };

  it('returns ok for an indicator with no option_stream slots', () => {
    expect(computeOptionStreamSanity({ seriesMap: { close: SPOT_INDEX } })).toEqual({ ok: true });
  });

  it('returns ok for a healthy option_stream slot', () => {
    expect(computeOptionStreamSanity({ seriesMap: { close: HEALTHY_STREAM_REF } })).toEqual({ ok: true });
  });

  it('flags a tautological by_delta + stream=delta combination', () => {
    const ref = {
      ...HEALTHY_STREAM_REF,
      selection: { kind: 'by_delta', target: 0.25 },
      stream: 'delta',
    };
    const out = computeOptionStreamSanity({ seriesMap: { close: ref } });
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('tautological_option_stream');
    expect(out.label).toBe('close');
    expect(out.stream).toBe('delta');
  });

  it('does NOT flag by_delta + stream=iv (only the delta-stream combo is tautological)', () => {
    const ref = {
      ...HEALTHY_STREAM_REF,
      selection: { kind: 'by_delta', target: 0.25 },
      stream: 'iv',
    };
    expect(computeOptionStreamSanity({ seriesMap: { close: ref } })).toEqual({ ok: true });
  });

  it('does NOT flag by_moneyness + stream=delta (only by_delta-by-construction is tautological)', () => {
    const ref = { ...HEALTHY_STREAM_REF, stream: 'delta' };
    expect(computeOptionStreamSanity({ seriesMap: { close: ref } })).toEqual({ ok: true });
  });

  it('returns ok when seriesMap is missing or null (defensive)', () => {
    expect(computeOptionStreamSanity({})).toEqual({ ok: true });
    expect(computeOptionStreamSanity({ seriesMap: null })).toEqual({ ok: true });
    expect(computeOptionStreamSanity(null)).toEqual({ ok: true });
  });
});

describe('runGateForBackendError', () => {
  it('returns null for an empty / null error', () => {
    expect(runGateForBackendError(null)).toBeNull();
    expect(runGateForBackendError(undefined)).toBeNull();
    expect(runGateForBackendError({})).toBeNull();
  });

  it('returns null for an error without a recognised error_code', () => {
    expect(runGateForBackendError({ error_code: 'INDICATOR_INCOMPATIBLE_ASSET' })).toBeNull();
    expect(runGateForBackendError({ error_code: 'UNRELATED_CODE' })).toBeNull();
  });

  it('produces a typed tooltip for TAUTOLOGICAL_OPTION_STREAM', () => {
    const tip = runGateForBackendError({
      error_code: 'TAUTOLOGICAL_OPTION_STREAM',
      asset_type: 'option',
      accepted_asset_types: ['option'],
      detail: 'whatever',
    });
    expect(tip).toBeTruthy();
    expect(tip).toContain('Tautological');
    expect(tip).toContain('by_delta');
  });

  it('produces a typed tooltip for STREAM_UNAVAILABLE_FOR_ROOT including the root and streams', () => {
    const tip = runGateForBackendError({
      error_code: 'STREAM_UNAVAILABLE_FOR_ROOT',
      root: 'SPX',
      unavailable_streams: ['gamma', 'vega'],
    });
    expect(tip).toBeTruthy();
    expect(tip).toContain('SPX');
    expect(tip).toContain('gamma');
    expect(tip).toContain('vega');
  });

  it('falls back to a generic root/stream label when fields are missing', () => {
    const tip = runGateForBackendError({ error_code: 'STREAM_UNAVAILABLE_FOR_ROOT' });
    expect(tip).toBeTruthy();
    expect(tip).toContain('this option root');
  });
});
