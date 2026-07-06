// @vitest-environment jsdom
// Form-level wiring of the implied-leverage readout into BOTH hold branches.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

vi.mock('../../api/options', () => ({ selectOption: vi.fn() }));
import { selectOption } from '../../api/options';
import OptionStreamForm, { buildDefaultOptionStream } from './OptionStreamForm';

afterEach(cleanup);
beforeEach(() => selectOption.mockReset());

const ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', has_greeks: true, last_trade_date: '2024-03-15' },
];

function putValue() {
  return {
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    option_type: 'P',
    // 'M' so the holdRequired one-shot cycle-default effect is a no-op (it only
    // fires when cycle is null / 'W3 Friday'), keeping onChange assertions clean.
    cycle: 'M',
    selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
    hold_between_rolls: true,
    nav_times: 1.0,
  };
}

describe('OptionStreamForm implied-leverage readout', () => {
  it('PORTFOLIO (holdRequired): shows the quantitative readout', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    render(<OptionStreamForm value={putValue()} onChange={vi.fn()} availableRoots={ROOTS} holdRequired />);
    const readout = await screen.findByTestId('lev-readout');
    expect(readout.getAttribute('data-band')).toBe('red');
    expect(readout.textContent).toContain('222×');
  });

  it('SIGNALS (showHoldControls, hold on): shows the readout AND the visible wipeout hint on fallback', async () => {
    // No premium → the readout falls back to the qualitative hint. Confirms the
    // Signals branch now surfaces the visible hint (previously absent).
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: null });
    render(<OptionStreamForm value={putValue()} onChange={vi.fn()} availableRoots={ROOTS} showHoldControls />);
    await waitFor(() => expect(screen.getByTestId('nav-hint')).toBeTruthy());
  });

  it('SIGNALS (showHoldControls, hold on): shows the quantitative readout when premium resolves', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    render(<OptionStreamForm value={putValue()} onChange={vi.fn()} availableRoots={ROOTS} showHoldControls />);
    const readout = await screen.findByTestId('lev-readout');
    expect(readout.getAttribute('data-band')).toBe('red');
  });

  it('read-only (disabled) leg still shows the readout without mutating the value', async () => {
    selectOption.mockResolvedValue({ contract: { strike: 5100 }, premium_mid: 23 });
    const onChange = vi.fn();
    render(<OptionStreamForm value={putValue()} onChange={onChange} availableRoots={ROOTS} holdRequired disabled />);
    await screen.findByTestId('lev-readout');
    // The probe is a read — it never emits a value change.
    expect(onChange).not.toHaveBeenCalled();
  });
});
