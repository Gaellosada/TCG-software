// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the API module so no real fetch happens.
vi.mock('../../api/options', () => ({
  selectOption: vi.fn(),
}));
import { selectOption } from '../../api/options';
import ImpliedLeverageReadout from './ImpliedLeverageReadout';

afterEach(() => {
  cleanup();
  // Restore real timers in case a fake-timer test left them installed (the
  // debounce-dependent tests above rely on real timers).
  vi.useRealTimers();
});
beforeEach(() => {
  selectOption.mockReset();
});

// A promise whose resolution we control, to drive the in-flight/stale race.
function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

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

  it('discards a stale in-flight probe when a newer query supersedes it (AbortController race)', async () => {
    const dA = deferred();
    const dB = deferred();
    selectOption.mockReturnValueOnce(dA.promise).mockReturnValueOnce(dB.promise);

    const putSP = { ...PUT_10D, collection: 'OPT_SP_500' };
    const putNQ = { ...PUT_10D, collection: 'OPT_NASDAQ_100' };
    const roots = [
      { collection: 'OPT_SP_500', has_greeks: true, last_trade_date: '2024-03-15' },
      { collection: 'OPT_NASDAQ_100', has_greeks: true, last_trade_date: '2024-03-15' },
    ];

    const { rerender } = render(
      <ImpliedLeverageReadout
        streamValue={putSP}
        navFraction={1.0}
        availableRoots={roots}
        referenceDate="2024-03-15"
      />,
    );
    // Probe #1 fires (deferred → still in flight).
    await waitFor(() => expect(selectOption).toHaveBeenCalledTimes(1));

    // Supersede with a different root → probe #2. Its debounce callback aborts
    // probe #1's controller.
    rerender(
      <ImpliedLeverageReadout
        streamValue={putNQ}
        navFraction={1.0}
        availableRoots={roots}
        referenceDate="2024-03-15"
      />,
    );
    await waitFor(() => expect(selectOption).toHaveBeenCalledTimes(2));

    // Resolve the STALE probe #1 FIRST (5100/23 → 222× red). It must be
    // discarded (cancelled + aborted), never landing in the readout.
    dA.resolve({ contract: { strike: 5100 }, premium_mid: 23 });
    // Then resolve the fresh probe #2 (4000/400 → 10× amber).
    dB.resolve({ contract: { strike: 4000 }, premium_mid: 400 });

    await waitFor(() =>
      expect(screen.getByTestId('lev-readout').getAttribute('data-band')).toBe('amber'),
    );
    const readout = screen.getByTestId('lev-readout');
    expect(readout.textContent).toContain('10×');
    expect(readout.textContent).not.toContain('222×');
  });

  it('normalizeDate: a Date object probes the LOCAL calendar date (no tz ±1-day shift)', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    // Local midnight on 2023-12-01. toISOString() in a positive-UTC zone would
    // roll to 2023-11-30; the component must use local components → 2023-12-01.
    renderReadout({ referenceDate: new Date(2023, 11, 1) });
    await screen.findByTestId('lev-readout');
    expect(selectOption.mock.calls[0][0].date).toBe('2023-12-01');
    expect(screen.getByTestId('lev-date').textContent).toContain('2023-12-01');
  });

  it('does not probe for a fixed-date maturity with no date (buildSelectQuery → null)', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    renderReadout({
      streamValue: { ...PUT_10D, maturity: { kind: 'fixed' } },
      referenceDate: '2024-03-15',
    });
    expect(screen.getByTestId('nav-hint')).toBeTruthy();
    await new Promise((r) => setTimeout(r, 350));
    expect(selectOption).not.toHaveBeenCalled();
  });

  it('does not probe when the selection is missing (buildSelectQuery → null)', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    const { selection, ...noSelection } = PUT_10D;
    expect(selection).toBeTruthy();
    renderReadout({ streamValue: noSelection, referenceDate: '2024-03-15' });
    expect(screen.getByTestId('nav-hint')).toBeTruthy();
    await new Promise((r) => setTimeout(r, 350));
    expect(selectOption).not.toHaveBeenCalled();
  });

  it('coalesces rapid contract changes into ONE debounced fetch', async () => {
    vi.useFakeTimers();
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    const roots = [
      { collection: 'OPT_SP_500', has_greeks: true, last_trade_date: '2024-03-15' },
      { collection: 'OPT_NASDAQ_100', has_greeks: true, last_trade_date: '2024-03-15' },
      { collection: 'OPT_VIX', has_greeks: true, last_trade_date: '2024-03-15' },
    ];
    const mk = (collection) => (
      <ImpliedLeverageReadout
        streamValue={{ ...PUT_10D, collection }}
        navFraction={1.0}
        availableRoots={roots}
        referenceDate="2024-03-15"
      />
    );
    const { rerender } = render(mk('OPT_SP_500'));
    // Three quick queryKey changes, each within the 300ms debounce window.
    await vi.advanceTimersByTimeAsync(100);
    rerender(mk('OPT_NASDAQ_100'));
    await vi.advanceTimersByTimeAsync(100);
    rerender(mk('OPT_VIX'));
    // Now let the debounce elapse: only the LAST change should fire.
    await vi.advanceTimersByTimeAsync(300);
    expect(selectOption).toHaveBeenCalledTimes(1);
    expect(selectOption.mock.calls[0][0].root).toBe('OPT_VIX');
  });
});
