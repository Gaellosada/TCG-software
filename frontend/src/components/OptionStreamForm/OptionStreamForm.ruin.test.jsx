// @vitest-environment jsdom
// The full-NAV-premium sizing warning renders at the sizing control when a
// held long option leg is sized for guaranteed premium decay, and is absent
// otherwise (advisory only — never blocks; adds no wire field).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

vi.mock('../../api/options', () => ({ selectOption: vi.fn() }));
import { selectOption } from '../../api/options';
import OptionStreamForm, { buildDefaultOptionStream } from './OptionStreamForm';

afterEach(cleanup);
beforeEach(() => {
  selectOption.mockReset();
  // Keep the leverage readout from touching state after unmount.
  selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
});

const ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', has_greeks: true, last_trade_date: '2024-03-15' },
];

function ruinValue(overrides = {}) {
  return {
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    option_type: 'P',
    cycle: 'M',
    selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
    hold_between_rolls: true,
    nav_times: 1.0,
    ...overrides,
  };
}

describe('OptionStreamForm full-NAV premium ruin warning', () => {
  it('renders the warning for a held full-NAV premium leg (holdRequired)', () => {
    render(<OptionStreamForm value={ruinValue()} onChange={vi.fn()} availableRoots={ROOTS} holdRequired />);
    expect(screen.getByTestId('premium-ruin-warning')).toBeTruthy();
  });

  it('does NOT render the warning under futures-notional sizing', () => {
    render(
      <OptionStreamForm
        value={ruinValue({ sizing_mode: 'futures_notional' })}
        onChange={vi.fn()}
        availableRoots={ROOTS}
        holdRequired
      />,
    );
    expect(screen.queryByTestId('premium-ruin-warning')).toBeNull();
  });

  it('does NOT render the warning for a small nav_times (partial NAV)', () => {
    render(
      <OptionStreamForm
        value={ruinValue({ nav_times: 0.1 })}
        onChange={vi.fn()}
        availableRoots={ROOTS}
        holdRequired
      />,
    );
    expect(screen.queryByTestId('premium-ruin-warning')).toBeNull();
  });

  it('does NOT render the warning when hold controls are off (no sizing shown)', () => {
    render(
      <OptionStreamForm
        value={ruinValue({ hold_between_rolls: false })}
        onChange={vi.fn()}
        availableRoots={ROOTS}
        showHoldControls
      />,
    );
    expect(screen.queryByTestId('premium-ruin-warning')).toBeNull();
  });
});
