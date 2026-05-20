/**
 * Single source of truth for the sidebar nav. Mirrors React's
 * `navConfig.js` 1:1 — adding a page = one new entry in the matching
 * section. Paths must match the route paths in `tcg-routes.ts`.
 */

export interface TcgNavItem {
  path: string;
  label: string;
  icon: string;
}

export interface TcgNavSection {
  id: string;
  label: string;
  /** When 'bottom', the section sticks to the bottom of the sidebar. */
  anchor?: 'bottom';
  items: ReadonlyArray<TcgNavItem>;
}

export const TCG_NAV_SECTIONS: ReadonlyArray<TcgNavSection> = [
  {
    id: 'live',
    label: 'Live',
    items: [{ path: '/running-signals', label: 'Running Signals', icon: 'signals' }],
  },
  {
    id: 'manual',
    label: 'Manual',
    items: [
      { path: '/data', label: 'Data', icon: 'data' },
      { path: '/indicators', label: 'Indicators', icon: 'indicators' },
      { path: '/signals', label: 'Signals', icon: 'signals' },
      { path: '/portfolio', label: 'Portfolio', icon: 'portfolio' },
    ],
  },
  {
    id: 'agents',
    label: 'Agents',
    items: [{ path: '/mongodb-agent', label: 'MongoDB Agent', icon: 'data' }],
  },
  {
    id: 'app',
    label: 'App',
    anchor: 'bottom',
    items: [
      { path: '/settings', label: 'Settings', icon: 'settings' },
      { path: '/help', label: 'Help', icon: 'help' },
      { path: '/tickets', label: 'Tickets', icon: 'ticket' },
    ],
  },
];
