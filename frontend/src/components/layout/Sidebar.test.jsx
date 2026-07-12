// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Sidebar from './Sidebar';
import { NAV_SECTIONS } from './navConfig';

afterEach(() => {
  cleanup();
});

function renderSidebar({ collapsed = false, initialPath = '/data' } = {}) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Sidebar collapsed={collapsed} onToggle={() => {}} />
    </MemoryRouter>
  );
}

describe('navConfig', () => {
  it('exports NAV_SECTIONS as a non-empty array', () => {
    expect(Array.isArray(NAV_SECTIONS)).toBe(true);
    expect(NAV_SECTIONS.length).toBeGreaterThan(0);
  });

  it('every section has id, label, items[] with required item shape', () => {
    for (const section of NAV_SECTIONS) {
      expect(typeof section.id).toBe('string');
      expect(section.id.length).toBeGreaterThan(0);
      expect(typeof section.label).toBe('string');
      expect(Array.isArray(section.items)).toBe(true);
      for (const item of section.items) {
        expect(typeof item.path).toBe('string');
        expect(item.path.startsWith('/')).toBe(true);
        expect(typeof item.label).toBe('string');
        expect(typeof item.icon).toBe('string');
      }
    }
  });

  it('section order is Live, Manual, Agents, App', () => {
    const ids = NAV_SECTIONS.map((s) => s.id);
    expect(ids).toEqual(['live', 'manual', 'agents', 'app']);
  });

  it('App section is marked anchor=bottom', () => {
    const app = NAV_SECTIONS.find((s) => s.id === 'app');
    expect(app.anchor).toBe('bottom');
  });

  it('Manual section preserves order Data, Indicators, Signals, Portfolio, Composed', () => {
    const manual = NAV_SECTIONS.find((s) => s.id === 'manual');
    expect(manual.items.map((i) => i.path)).toEqual([
      '/data',
      '/indicators',
      '/signals',
      '/portfolio',
      '/composed-portfolios',
    ]);
  });
});

describe('<Sidebar>', () => {
  it('renders without crashing', () => {
    renderSidebar();
    // Logo TCG text is present when expanded.
    expect(screen.getByText('TCG')).toBeTruthy();
  });

  it('renders one link per navConfig item', () => {
    const { container } = renderSidebar();
    const expectedCount = NAV_SECTIONS.reduce(
      (n, s) => n + s.items.length,
      0
    );
    const links = container.querySelectorAll('a');
    expect(links.length).toBe(expectedCount);
  });

  it('renders the section labels when expanded', () => {
    renderSidebar({ collapsed: false });
    expect(screen.getByText('Live')).toBeTruthy();
    expect(screen.getByText('Manual')).toBeTruthy();
    expect(screen.getByText('Agents')).toBeTruthy();
    expect(screen.getByText('App')).toBeTruthy();
  });

  it('hides section labels when collapsed', () => {
    renderSidebar({ collapsed: true });
    expect(screen.queryByText('Live')).toBeNull();
    expect(screen.queryByText('Manual')).toBeNull();
    expect(screen.queryByText('Agents')).toBeNull();
    expect(screen.queryByText('App')).toBeNull();
  });

  it('renders sections in NAV_SECTIONS order in the DOM', () => {
    const { container } = renderSidebar();
    const sectionEls = container.querySelectorAll('[data-section-id]');
    const ids = Array.from(sectionEls).map((el) =>
      el.getAttribute('data-section-id')
    );
    expect(ids).toEqual(NAV_SECTIONS.map((s) => s.id));
  });

  it('applies active state to NavLink matching the current route', () => {
    renderSidebar({ initialPath: '/portfolio' });
    const link = screen.getByRole('link', { name: /portfolio/i });
    // CSS module class names contain "active" substring when the class is applied.
    expect(link.className).toMatch(/active/);
  });

  it('does NOT apply active state to non-matching routes', () => {
    renderSidebar({ initialPath: '/portfolio' });
    const link = screen.getByRole('link', { name: /^data$/i });
    expect(link.className).not.toMatch(/\bactive\b/);
  });
});
