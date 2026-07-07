// Unit tests for the Portfolio leg <-> InstrumentPickerModal config translation.
//
// Portfolio legs are FLATTENED / field-RENAMED relative to the modal's own
// discriminated union (the shape it emits via onSelect and consumes via
// initialConfig):
//   - spot:      leg {type:'instrument', symbol}   <->  modal {type:'spot', instrument_id}
//   - continuous: camelCase `rollOffset` (both sides)
//   - option:     snake_case `roll_offset` {value,unit}; NO `adjustment` field
//
// `instrumentToLegConfig` is the forward (modal -> leg config) direction shared
// by the add and edit flows; `legToInitialConfig` is the inverse (leg -> modal
// initialConfig) that pre-fills the picker when editing an existing leg. The
// round-trip must be an identity for the config fields, or an edit that changes
// nothing would silently rewrite the leg.

import { describe, it, expect } from 'vitest';
import { instrumentToLegConfig, legToInitialConfig } from './legConfig';

describe('instrumentToLegConfig (modal onSelect union -> leg config)', () => {
  it('maps a continuous union to a continuous leg config (camel rollOffset, keeps adjustment)', () => {
    const config = instrumentToLegConfig({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 3,
    });
    expect(config).toEqual({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 3,
    });
  });

  it('maps an option_stream union to an option leg config (snake roll_offset, forces hold, NO adjustment)', () => {
    const config = instrumentToLegConfig({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05 },
      stream: 'mid',
      roll_offset: { value: 2, unit: 'days' },
      // a stray adjustment must be dropped (options carry no back-adjustment)
      adjustment: 'ratio',
      hold_between_rolls: true,
      nav_times: 0.5,
    });
    expect(config).toEqual({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05 },
      stream: 'mid',
      roll_offset: { value: 2, unit: 'days' },
      hold_between_rolls: true,
      nav_times: 0.5,
    });
    expect('adjustment' in config).toBe(false);
  });

  it('forces hold_between_rolls on and defaults nav_times to 1.0 for a portfolio option leg', () => {
    const config = instrumentToLegConfig({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'fixed', date: '2025-06-20' },
      selection: { kind: 'by_moneyness', target: 1.0 },
      stream: 'mid',
      roll_offset: { value: 0, unit: 'days' },
      // hold/nav omitted by the form -> the portfolio leg must still be held
    });
    expect(config.hold_between_rolls).toBe(true);
    expect(config.nav_times).toBe(1.0);
  });

  it('maps a spot union to an instrument leg config (type instrument, symbol from instrument_id)', () => {
    const config = instrumentToLegConfig({
      type: 'spot',
      collection: 'equity_etf',
      instrument_id: 'SPY',
    });
    expect(config).toEqual({
      type: 'instrument',
      collection: 'equity_etf',
      symbol: 'SPY',
    });
  });
});

describe('legToInitialConfig (leg -> modal initialConfig)', () => {
  it('returns null for a null leg', () => {
    expect(legToInitialConfig(null)).toBeNull();
  });

  it('returns null for a signal leg (no modal seed path)', () => {
    expect(legToInitialConfig({ id: 1, type: 'signal', label: 'S' })).toBeNull();
  });

  it('inverts a continuous leg to the continuous union (camel rollOffset)', () => {
    const initial = legToInitialConfig({
      id: 7,
      label: 'FUT_ES',
      weight: 100,
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 3,
      // stale option fields that must not leak into a continuous union
      option_type: null,
      roll_offset: null,
    });
    expect(initial).toEqual({
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 3,
      strategy: 'front_month',
    });
  });

  it('inverts an option leg to the option_stream union (snake roll_offset, hold/nav preserved, NO adjustment)', () => {
    const initial = legToInitialConfig({
      id: 9,
      label: 'OPT_SP_500 P mid',
      weight: 100,
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1 },
      stream: 'mid',
      roll_offset: { value: 2, unit: 'days' },
      hold_between_rolls: true,
      nav_times: 0.5,
      // stale continuous fields must not leak
      strategy: null,
      adjustment: null,
      rollOffset: 0,
    });
    expect(initial).toEqual({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1 },
      stream: 'mid',
      roll_offset: { value: 2, unit: 'days' },
      hold_between_rolls: true,
      nav_times: 0.5,
    });
    expect('adjustment' in initial).toBe(false);
  });

  it('inverts a spot/instrument leg to the spot union (instrument_id from symbol)', () => {
    const initial = legToInitialConfig({
      id: 3,
      label: 'SPY',
      weight: 50,
      type: 'instrument',
      collection: 'equity_etf',
      symbol: 'SPY',
    });
    expect(initial).toEqual({
      type: 'spot',
      collection: 'equity_etf',
      instrument_id: 'SPY',
    });
  });
});

describe('round-trip identity (a no-op edit must not rewrite the leg config)', () => {
  const continuousUnion = {
    type: 'continuous',
    collection: 'FUT_ES',
    strategy: 'calendar',
    adjustment: 'difference',
    cycle: 'Z',
    rollOffset: 5,
  };
  const optionUnion = {
    type: 'option_stream',
    collection: 'OPT_SP_500',
    option_type: 'C',
    cycle: 'M',
    maturity: { kind: 'nearest_to_target', target_days: 45 },
    selection: { kind: 'by_moneyness', target: 0.95, tolerance: 0.02 },
    stream: 'mid',
    roll_offset: { value: 3, unit: 'days' },
    hold_between_rolls: true,
    nav_times: 0.25,
  };
  const spotUnion = { type: 'spot', collection: 'equity_etf', instrument_id: 'QQQ' };

  it.each([
    ['continuous', continuousUnion],
    ['option', optionUnion],
    ['spot', spotUnion],
  ])('modal -> leg -> modal is identity for %s', (_name, union) => {
    const legConfig = instrumentToLegConfig(union);
    // Simulate a stored leg (id/label/weight are identity/derived, not config).
    const leg = { id: 1, label: 'x', weight: 100, ...legConfig };
    expect(legToInitialConfig(leg)).toEqual(union);
  });

  it.each([
    ['continuous', continuousUnion],
    ['option', optionUnion],
  ])('leg -> modal -> leg is identity for the %s config fields', (_name, union) => {
    const legConfig = instrumentToLegConfig(union);
    const leg = { id: 2, label: 'y', weight: 42, ...legConfig };
    // Re-derive config after a round-trip through the modal shape.
    const roundTripped = instrumentToLegConfig(legToInitialConfig(leg));
    expect(roundTripped).toEqual(legConfig);
  });
});
