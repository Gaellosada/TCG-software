// @vitest-environment jsdom
//
// Help page regression tests.
//
// Locks in the post-rewrite contract: page mounts, the six kept sections render,
// the per-block reset binding mention (new from PR #39) is present, and the
// sticky nav exposes the six expected anchors.

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

  it('renders the six kept section headings', () => {
    render(<HelpPage />);
    // sectionHeading h2s — one per kept section
    expect(screen.getByRole('heading', { level: 2, name: 'Overview' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Data' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Portfolio' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Indicators' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Signals' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Settings' })).toBeTruthy();
  });

  it('renders the six expected section anchor ids', () => {
    const { container } = render(<HelpPage />);
    const expected = [
      'help-overview',
      'help-data',
      'help-portfolio',
      'help-indicators',
      'help-signals',
      'help-settings',
    ];
    for (const id of expected) {
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
  });

  it('sticky nav has exactly six anchor buttons matching the section ids', () => {
    render(<HelpPage />);
    const nav = screen.getByRole('navigation');
    const buttons = nav.querySelectorAll('button');
    expect(buttons.length).toBe(6);
    const labels = Array.from(buttons).map((b) => b.textContent);
    expect(labels).toEqual([
      'Overview',
      'Data',
      'Portfolio',
      'Indicators',
      'Signals',
      'Settings',
    ]);
  });

  it('mentions the per-block reset binding (new from PR #39)', () => {
    render(<HelpPage />);
    // <details><summary> renders the title text directly
    expect(screen.getByText(/per-block reset binding/i)).toBeTruthy();
  });
});
