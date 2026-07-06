// @vitest-environment jsdom
//
// Help page regression tests.
//
// Locks in the section contract: page mounts, every section renders, the
// per-block reset binding mention (new from PR #39) is present, and the sticky
// nav exposes the expected anchors in order. The Options + Tickets sections
// were added when those features shipped.

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import HelpPage from './HelpPage';

afterEach(() => {
  cleanup();
});

describe('HelpPage', () => {
  it('mounts without runtime error', () => {
    expect(() => render(<HelpPage />)).not.toThrow();
  });

  it('renders every section heading', () => {
    render(<HelpPage />);
    // sectionHeading h2s — one per section
    expect(screen.getByRole('heading', { level: 2, name: 'Overview' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Data' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Options' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Portfolio' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Indicators' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Signals' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Tickets' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Settings' })).toBeTruthy();
  });

  it('renders every expected section anchor id', () => {
    const { container } = render(<HelpPage />);
    const expected = [
      'help-overview',
      'help-data',
      'help-options',
      'help-portfolio',
      'help-indicators',
      'help-signals',
      'help-tickets',
      'help-settings',
    ];
    for (const id of expected) {
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
  });

  it('sticky nav has one anchor button per section, in order', () => {
    render(<HelpPage />);
    const nav = screen.getByRole('navigation');
    const buttons = nav.querySelectorAll('button');
    expect(buttons.length).toBe(8);
    const labels = Array.from(buttons).map((b) => b.textContent);
    expect(labels).toEqual([
      'Overview',
      'Data',
      'Options',
      'Portfolio',
      'Indicators',
      'Signals',
      'Tickets',
      'Settings',
    ]);
  });

  it('documents the Tickets section, including permanent deletion', () => {
    render(<HelpPage />);
    // The Tickets section explains that deletion is permanent (distinct from
    // the soft-archive used by signals/indicators).
    expect(screen.getByText(/permanent/i)).toBeTruthy();
  });

  it('mentions the per-block reset binding (new from PR #39)', () => {
    render(<HelpPage />);
    // <details><summary> renders the title text directly
    expect(screen.getByText(/per-block reset binding/i)).toBeTruthy();
  });

  it('documents how option backtests are priced', () => {
    render(<HelpPage />);
    expect(screen.getByText(/how option backtests are priced/i)).toBeTruthy();
    expect(screen.getByText(/nav_times/)).toBeTruthy();
  });

  it('documents the implied-leverage readout on the option Size % field', () => {
    render(<HelpPage />);
    // The dedicated subsection exists…
    expect(screen.getByText(/Implied leverage on the Size %/i)).toBeTruthy();
    // …states the formula in the verifiable "Size% ÷ premium-%-of-strike" form…
    expect(screen.getByText(/Size % ÷ \(premium as % of strike\)/i)).toBeTruthy();
    // …and states the exact colour-band thresholds (from LEVERAGE_BANDS).
    expect(screen.getByText(/green below 2×/i)).toBeTruthy();
    expect(screen.getByText(/amber 2–10×/i)).toBeTruthy();
    expect(screen.getByText(/red above 10×/i)).toBeTruthy();
    // …and the short-leg wipeout multiple formula.
    expect(screen.getByText(/1 \+ 1 ÷ \(Size % ÷ 100\)/i)).toBeTruthy();
  });

  it('documents block composition (AND / THEN groups)', () => {
    render(<HelpPage />);
    expect(screen.getByText(/AND \/ THEN groups/i)).toBeTruthy();
  });

  it('documents fire mode (pulse vs. sustained) in its own section with an example', () => {
    render(<HelpPage />);
    expect(screen.getByText(/Fire mode: pulse vs\. sustained/i)).toBeTruthy();
    expect(screen.getAllByText(/Pulse/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Sustained/).length).toBeGreaterThan(0);
    // The concrete worked example (3 taps within 30 bars) must be present.
    expect(screen.getByText(/3 taps within 30 bars/i)).toBeTruthy();
  });
});
