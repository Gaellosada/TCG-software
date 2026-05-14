// Single source of truth for sidebar nav. Adding a page = one new entry.
export const NAV_SECTIONS = [
  {
    id: 'live',
    label: 'Live',
    items: [
      { path: '/running-signals', label: 'Running Signals', icon: 'signals' },
    ],
  },
  {
    id: 'manual',
    label: 'Manual',
    items: [
      { path: '/data',       label: 'Data',       icon: 'data' },
      { path: '/indicators', label: 'Indicators', icon: 'indicators' },
      { path: '/signals',    label: 'Signals',    icon: 'signals' },
      { path: '/portfolio',  label: 'Portfolio',  icon: 'portfolio' },
    ],
  },
  {
    id: 'agents',
    label: 'Agents',
    items: [
      { path: '/mongodb-agent', label: 'MongoDB Agent', icon: 'data' },
    ],
  },
  {
    id: 'app',
    label: 'App',
    anchor: 'bottom',
    items: [
      { path: '/settings', label: 'Settings', icon: 'settings' },
      { path: '/help',     label: 'Help',     icon: 'help' },
    ],
  },
];
