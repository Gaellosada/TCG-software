// @vitest-environment jsdom
//
// The read-only source badge: shows a leg/input/series' source (v1 or v2) and
// exposes NO control to mutate it — the source is chosen once, at add time, and
// is immutable thereafter (delete + re-add to change it).

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import SourceBadge from './SourceBadge';

describe('SourceBadge', () => {
  it('renders v2 for a v2 source', () => {
    render(<SourceBadge source="v2" testId="b" />);
    const el = screen.getByTestId('b');
    expect(el.textContent).toBe('v2');
    expect(el.getAttribute('data-source')).toBe('v2');
  });

  it('renders v1 for a v1 source AND for an absent/bogus source (never hidden)', () => {
    render(<SourceBadge source={undefined} testId="a" />);
    render(<SourceBadge source="v9" testId="c" />);
    expect(screen.getByTestId('a').textContent).toBe('v1');
    expect(screen.getByTestId('c').textContent).toBe('v1');
  });

  it('is display-only — it is a <span>, not an interactive control', () => {
    render(<SourceBadge source="v2" testId="b" />);
    const el = screen.getByTestId('b');
    expect(el.tagName).toBe('SPAN');
    // No form control / button inside it.
    expect(el.querySelector('select, button, input')).toBeNull();
  });
});
