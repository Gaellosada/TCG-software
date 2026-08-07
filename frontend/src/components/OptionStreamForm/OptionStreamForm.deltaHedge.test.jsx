// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import OptionStreamForm, { buildDefaultOptionStream } from './OptionStreamForm';
import { instrumentToLegConfig, legToInitialConfig } from '../../pages/Portfolio/legConfig';

afterEach(cleanup);

const ROOTS = [
  { collection: 'OPT_VIX', root_label: 'VIX', has_greeks: true },
];

function renderForm(overrides = {}) {
  const onChange = overrides.onChange || vi.fn();
  const value = overrides.value !== undefined
    ? overrides.value
    : buildDefaultOptionStream({ availableRoots: ROOTS });
  const props = {
    value,
    onChange,
    availableRoots: ROOTS,
    holdRequired: true,
    ...overrides,
  };
  return { onChange, value, ...render(<OptionStreamForm {...props} />) };
}

describe('<OptionStreamForm> delta-hedge overlay (F2)', () => {
  it('does NOT render the hedge controls unless showDeltaHedge is set', () => {
    renderForm({ showDeltaHedge: false });
    expect(screen.queryByTestId('delta-hedge-enabled')).toBeNull();
  });

  it('renders the enable checkbox when showDeltaHedge is set (portfolio leg)', () => {
    renderForm({ showDeltaHedge: true });
    expect(screen.getByTestId('delta-hedge-enabled')).toBeTruthy();
    // factor + threshold hidden until enabled.
    expect(screen.queryByTestId('delta-hedge-factor')).toBeNull();
  });

  it('emits a delta_hedge object with the SPEC defaults when enabled', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange });
    fireEvent.click(screen.getByTestId('delta-hedge-enabled'));
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls.at(-1)[0];
    expect(emitted.delta_hedge).toMatchObject({
      enabled: true,
      factor: 1 / 3,
      hedge_collection: 'FUT_VIX',
      gate_collection: 'INDEX',
      gate_symbol: 'IND_VVIX',
      gate_threshold: 150,
      gate_op: 'gt',
    });
  });

  it('clears delta_hedge when the checkbox is unticked', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
    };
    renderForm({ showDeltaHedge: true, onChange, value });
    fireEvent.click(screen.getByTestId('delta-hedge-enabled'));
    const emitted = onChange.mock.calls.at(-1)[0];
    expect(emitted.delta_hedge).toBeUndefined();
  });

  it('lets the user edit the factor and gate threshold', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
    };
    renderForm({ showDeltaHedge: true, onChange, value });
    fireEvent.change(screen.getByTestId('delta-hedge-factor'), { target: { value: '0.5' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.factor).toBe(0.5);
    fireEvent.change(screen.getByTestId('delta-hedge-threshold'), { target: { value: '120' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.gate_threshold).toBe(120);
  });
});

describe('<OptionStreamForm> delta-hedge ADVANCED knobs (modular hedge)', () => {
  const hedged = () => ({
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
  });

  it('renders the three advanced inputs with defaults once the hedge is enabled', () => {
    renderForm({ showDeltaHedge: true, value: hedged() });
    const interval = screen.getByTestId('delta-hedge-interval');
    const qtyCap = screen.getByTestId('delta-hedge-qty-cap');
    const pause = screen.getByTestId('delta-hedge-pause-on-roll');
    expect(interval).toBeTruthy();
    expect(qtyCap).toBeTruthy();
    expect(pause).toBeTruthy();
    // Defaults shown even though the wire object omits the keys.
    expect(Number(interval.value)).toBe(1);
    expect(Number(qtyCap.value)).toBe(10);
    expect(pause.checked).toBe(true);
  });

  it('does NOT render the advanced inputs while the hedge is disabled', () => {
    renderForm({ showDeltaHedge: true, value: buildDefaultOptionStream({ availableRoots: ROOTS }) });
    expect(screen.queryByTestId('delta-hedge-interval')).toBeNull();
    expect(screen.queryByTestId('delta-hedge-qty-cap')).toBeNull();
    expect(screen.queryByTestId('delta-hedge-pause-on-roll')).toBeNull();
  });

  it('threads the three fields onto delta_hedge with the exact wire keys', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: hedged() });
    fireEvent.change(screen.getByTestId('delta-hedge-interval'), { target: { value: '5' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.rebalance_interval_days).toBe(5);
    fireEvent.change(screen.getByTestId('delta-hedge-qty-cap'), { target: { value: '3.5' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.qty_cap_mult).toBe(3.5);
    fireEvent.click(screen.getByTestId('delta-hedge-pause-on-roll'));
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.pause_on_roll).toBe(false);
  });

  it('coerces an invalid interval to 1 (int ≥ 1) and shows a hint', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: hedged() });
    fireEvent.change(screen.getByTestId('delta-hedge-interval'), { target: { value: '0' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.rebalance_interval_days).toBe(1);
    expect(screen.getByTestId('delta-hedge-interval-hint')).toBeTruthy();
  });

  it('coerces a non-positive qty cap to the default 10 and shows a hint', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: hedged() });
    fireEvent.change(screen.getByTestId('delta-hedge-qty-cap'), { target: { value: '-2' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.qty_cap_mult).toBe(10);
    expect(screen.getByTestId('delta-hedge-qty-cap-hint')).toBeTruthy();
  });

  it('enabling the hedge with defaults OMITS all three advanced keys (byte-identical)', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: buildDefaultOptionStream({ availableRoots: ROOTS }) });
    fireEvent.click(screen.getByTestId('delta-hedge-enabled'));
    const dh = onChange.mock.calls.at(-1)[0].delta_hedge;
    expect(dh.enabled).toBe(true);
    expect('rebalance_interval_days' in dh).toBe(false);
    expect('qty_cap_mult' in dh).toBe(false);
    expect('pause_on_roll' in dh).toBe(false);
  });
});

describe('<OptionStreamForm> delta-hedge INSTRUMENT chooser (P1 modular hedge)', () => {
  const hedged = () => ({
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
  });

  it('defaults the chooser to VX1 and omits hedge_instrument on enable (byte-identical)', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: buildDefaultOptionStream({ availableRoots: ROOTS }) });
    fireEvent.click(screen.getByTestId('delta-hedge-enabled'));
    const dh = onChange.mock.calls.at(-1)[0].delta_hedge;
    expect('hedge_instrument' in dh).toBe(false);
    // Re-render with the enabled hedge; the mode select reads VX1.
    renderForm({ showDeltaHedge: true, value: hedged() });
    expect(screen.getByTestId('delta-hedge-instrument-mode').value).toBe('vx1');
    // No collection field while VX1.
    expect(screen.queryByTestId('delta-hedge-instrument-collection')).toBeNull();
  });

  it('emits a continuous-future hedge_instrument when the mode switches to continuous', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: hedged() });
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-mode'), { target: { value: 'continuous' } });
    const emitted = onChange.mock.calls.at(-1)[0].delta_hedge.hedge_instrument;
    expect(emitted).toMatchObject({ type: 'continuous', adjustment: 'difference', strategy: 'front_month' });
  });

  it('threads collection + roll strategy onto a continuous hedge_instrument', () => {
    const onChange = vi.fn();
    const value = {
      ...hedged(),
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150, hedge_instrument: { type: 'continuous', collection: 'FUT_VIX', adjustment: 'difference', strategy: 'front_month' } },
    };
    renderForm({ showDeltaHedge: true, onChange, value });
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-collection'), { target: { value: 'FUT_ES' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.hedge_instrument.collection).toBe('FUT_ES');
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-strategy'), { target: { value: 'end_of_month' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.hedge_instrument.strategy).toBe('end_of_month');
  });

  it('emits a spot hedge_instrument and threads collection + instrument id', () => {
    const onChange = vi.fn();
    renderForm({ showDeltaHedge: true, onChange, value: hedged() });
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-mode'), { target: { value: 'spot' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.hedge_instrument).toMatchObject({ type: 'spot' });
    // Re-render carrying the spot instrument so the id field is present.
    const value = {
      ...hedged(),
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150, hedge_instrument: { type: 'spot', collection: '', instrument_id: '' } },
    };
    renderForm({ showDeltaHedge: true, onChange, value });
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-id'), { target: { value: 'SPX' } });
    expect(onChange.mock.calls.at(-1)[0].delta_hedge.hedge_instrument.instrument_id).toBe('SPX');
  });

  it('clears hedge_instrument when the mode switches back to VX1', () => {
    const onChange = vi.fn();
    const value = {
      ...hedged(),
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150, hedge_instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
    };
    renderForm({ showDeltaHedge: true, onChange, value });
    fireEvent.change(screen.getByTestId('delta-hedge-instrument-mode'), { target: { value: 'vx1' } });
    const dh = onChange.mock.calls.at(-1)[0].delta_hedge;
    expect('hedge_instrument' in dh).toBe(false);
  });
});

describe('legConfig delta-hedge round-trip', () => {
  it('forwards an enabled delta_hedge onto the leg config', () => {
    const instrument = {
      type: 'option_stream',
      collection: 'OPT_VIX',
      option_type: 'C',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: 0.5, tolerance: 0.4 },
      stream: 'close',
      roll_offset: { value: 2, unit: 'days' },
      nav_times: 0.3,
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
    };
    const leg = instrumentToLegConfig(instrument);
    expect(leg.delta_hedge).toEqual({ enabled: true, factor: 1 / 3, gate_threshold: 150 });
  });

  it('omits delta_hedge when the overlay is disabled/absent (byte-identical)', () => {
    const instrument = {
      type: 'option_stream',
      collection: 'OPT_VIX',
      option_type: 'C',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: 0.5, tolerance: 0.4 },
      stream: 'close',
      roll_offset: { value: 2, unit: 'days' },
      nav_times: 0.3,
    };
    const leg = instrumentToLegConfig(instrument);
    expect('delta_hedge' in leg).toBe(false);
  });

  it('restores delta_hedge onto the modal seed when editing a hedged leg', () => {
    const leg = {
      type: 'option_stream',
      collection: 'OPT_VIX',
      option_type: 'C',
      cycle: 'M',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: 0.5, tolerance: 0.4 },
      stream: 'close',
      roll_offset: { value: 2, unit: 'days' },
      nav_times: 0.3,
      hold_between_rolls: true,
      delta_hedge: { enabled: true, factor: 1 / 3, gate_threshold: 150 },
    };
    const seed = legToInitialConfig(leg);
    expect(seed.delta_hedge).toEqual({ enabled: true, factor: 1 / 3, gate_threshold: 150 });
  });
});
