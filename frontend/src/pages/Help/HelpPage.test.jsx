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

  it('documents block composition (AND / THEN and fire modes)', () => {
    render(<HelpPage />);
    expect(screen.getByText(/AND \/ THEN and fire modes/i)).toBeTruthy();
    expect(screen.getByText(/Pulse/)).toBeTruthy();
    expect(screen.getByText(/Sustained/)).toBeTruthy();
  });
});
