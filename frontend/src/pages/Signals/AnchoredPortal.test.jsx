// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { createRef } from 'react';

import AnchoredPortal from './AnchoredPortal';

afterEach(() => { cleanup(); });

// Render AnchoredPortal against an anchor whose bottom edge we control, so we
// can assert the overlay is clamped to the space below the anchor and scrolls
// internally rather than overflowing off-screen.
function renderPortalAnchoredAt(bottom) {
  const anchorRef = createRef();
  const anchorEl = document.createElement('div');
  anchorEl.getBoundingClientRect = () => ({
    top: bottom - 20,
    bottom,
    left: 10,
    right: 110,
    width: 100,
    height: 20,
  });
  anchorRef.current = anchorEl;
  render(
    <AnchoredPortal anchorRef={anchorRef} testId="portal">
      <div style={{ height: 5000 }}>tall</div>
    </AnchoredPortal>,
  );
  return screen.getByTestId('portal');
}

describe('AnchoredPortal max-height clamp', () => {
  it('bounds the overlay to the space below an anchor near the viewport bottom', () => {
    // jsdom defaults innerHeight to 768; anchor bottom at 700 leaves 700+4 top,
    // so maxHeight = 768 - 704 - 8 = 56 → floored to the 120px minimum.
    const node = renderPortalAnchoredAt(700);
    expect(node.style.overflowY).toBe('auto');
    expect(node.style.maxHeight).toBe('120px');
  });

  it('allows a taller overlay when the anchor sits high in the viewport', () => {
    // Anchor bottom at 100 → top 104 → maxHeight = 768 - 104 - 8 = 656.
    const node = renderPortalAnchoredAt(100);
    expect(node.style.overflowY).toBe('auto');
    expect(node.style.maxHeight).toBe('656px');
  });
});
