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
