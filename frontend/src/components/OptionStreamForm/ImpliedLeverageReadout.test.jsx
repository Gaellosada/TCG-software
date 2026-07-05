// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the API module so no real fetch happens.
vi.mock('../../api/options', () => ({
  selectOption: vi.fn(),
}));
import { selectOption } from '../../api/options';
import ImpliedLeverageReadout from './ImpliedLeverageReadout';

afterEach(cleanup);
beforeEach(() => {
  selectOption.mockReset();
});

const ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', has_greeks: true, last_trade_date: '2024-03-15' },
];

// A short 10Δ put on OPT_SP_500.
const PUT_10D = {
  type: 'option_stream',
  collection: 'OPT_SP_500',
  option_type: 'P',
  cycle: 'M',
  maturity: { kind: 'next_third_friday', offset_months: 0 },
  selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
  stream: 'mid',
};

function renderReadout(overrides = {}) {
  const props = {
    streamValue: PUT_10D,
    navFraction: 1.0,
    availableRoots: ROOTS,
    ...overrides,
  };
  return render(<ImpliedLeverageReadout {...props} />);
}

describe('<ImpliedLeverageReadout>', () => {
  it('probes /select and renders a large RED leverage readout for a full-notional 10Δ put', async () => {
    // strike 5100, premium 23 → leverage ≈ 222×  → RED
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    renderReadout({ navFraction: 1.0 });

    const readout = await screen.findByTestId('lev-readout');
    expect(readout.textContent).toContain('222×');
    expect(readout.textContent).toContain('underlying notional');
    expect(readout.getAttribute('data-band')).toBe('red');

    // Sub-line: premium as % of strike + selection label.
    expect(screen.getByTestId('lev-subline').textContent).toMatch(/0\.45% of strike.*10Δ put/);
    // Caution: 2.0× spike wipes equity at 100% — direction-honest wording.
    const caution = screen.getByTestId('lev-caution').textContent;
    expect(caution).toContain('2.0×');
    expect(caution).toMatch(/sold\/written/i);
    expect(caution).toMatch(/bought leg risks only the premium/i);

    // Snapshot-date label: falls back to the root's last_trade_date here.
    expect(screen.getByTestId('lev-date').textContent).toContain('2024-03-15');
  });

  it('uses the referenceDate prop when supplied (else falls back to last_trade_date)', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    renderReadout({ referenceDate: '2023-12-01' });
    await screen.findByTestId('lev-readout');
    const q = selectOption.mock.calls[0][0];
    expect(q.date).toBe('2023-12-01');
    expect(q.root).toBe('OPT_SP_500');
    expect(q.type).toBe('P');
    expect(q.criterion.kind).toBe('by_delta');
    // The date label reflects the date actually probed (the referenceDate prop).
    expect(screen.getByTestId('lev-date').textContent).toContain('2023-12-01');
  });

  it('recomputes on Size% (navFraction) change WITHOUT refetching', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    const { rerender } = renderReadout({ navFraction: 1.0 });
    const readout = await screen.findByTestId('lev-readout');
    expect(readout.getAttribute('data-band')).toBe('red');
    expect(selectOption).toHaveBeenCalledTimes(1);

    // Drop to 0.5% of NAV → leverage ≈ 1.1× → green, and NO new fetch.
    rerender(
      <ImpliedLeverageReadout streamValue={PUT_10D} navFraction={0.005} availableRoots={ROOTS} />,
    );
    await waitFor(() => {
      expect(screen.getByTestId('lev-readout').getAttribute('data-band')).toBe('green');
    });
    expect(selectOption).toHaveBeenCalledTimes(1); // no refetch on Size% change
  });

  it('falls back to the qualitative hint when the probe errors', async () => {
    selectOption.mockRejectedValue(new Error('boom'));
    renderReadout();
    await waitFor(() => {
      expect(screen.getByTestId('nav-hint')).toBeTruthy();
    });
    expect(screen.queryByTestId('lev-readout')).toBeNull();
  });

  it('falls back when premium is missing (null) — no bogus number, no div-by-zero', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: null });
    renderReadout();
    await waitFor(() => {
      expect(screen.getByTestId('nav-hint')).toBeTruthy();
    });
    expect(screen.queryByTestId('lev-readout')).toBeNull();
  });

  it('does not probe when no reference date is available (no last_trade_date, no prop)', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    render(
      <ImpliedLeverageReadout
        streamValue={PUT_10D}
        navFraction={1.0}
        availableRoots={[{ collection: 'OPT_SP_500', has_greeks: true }]}
      />,
    );
    // Fallback hint shows; no fetch is issued.
    expect(screen.getByTestId('nav-hint')).toBeTruthy();
    // Give any (unexpected) debounced fetch time to fire.
    await new Promise((r) => setTimeout(r, 350));
    expect(selectOption).not.toHaveBeenCalled();
  });

  it('reports the band via onBand for input tinting', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    const onBand = vi.fn();
    renderReadout({ onBand });
    await screen.findByTestId('lev-readout');
    await waitFor(() => expect(onBand).toHaveBeenCalledWith('red'));
  });
});
