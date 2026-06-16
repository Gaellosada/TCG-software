// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { renderWithClient, makeTestClient } from '../../test/queryWrapper';
import CategoryBrowser from './CategoryBrowser';

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Module mocks — must be declared before imports to ensure hoisting
// ---------------------------------------------------------------------------

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
}));

vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(),
}));

import { listCollections, listInstruments } from '../../api/data';
import { getOptionRoots } from '../../api/options';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MOCK_ROOTS = [
  {
    collection: 'OPT_SP_500',
    name: 'SP 500',
    has_greeks: true,
    providers: ['IVOLATILITY'],
    expiration_first: '2020-01-01',
    expiration_last: '2025-12-31',
    last_trade_date: '2024-12-31',
    doc_count_estimated: 50000,
    strike_factor_verified: true,
  },
  {
    collection: 'OPT_GOLD',
    name: 'Gold',
    has_greeks: true,
    providers: ['IVOLATILITY'],
    expiration_first: '2020-01-01',
    expiration_last: '2025-12-31',
    last_trade_date: '2024-12-31',
    doc_count_estimated: 10000,
    strike_factor_verified: true,
  },
  {
    collection: 'OPT_T_NOTE_10_Y',
    name: 'T-Note 10Y',
    has_greeks: false,
    providers: ['IVOLATILITY'],
    expiration_first: '2020-01-01',
    expiration_last: '2025-12-31',
    last_trade_date: '2024-12-31',
    doc_count_estimated: 5000,
    strike_factor_verified: false,
  },
];

function defaultDataMocks() {
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_SP_500']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [] });
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function renderAndWait(props = {}) {
  const onSelect = props.onSelect ?? vi.fn();
  const selected = props.selected ?? null;

  renderWithClient(<CategoryBrowser selected={selected} onSelect={onSelect} />);

  // Wait for the loading state to disappear.
  await waitFor(() => {
    expect(screen.queryByText('Loading instruments...')).toBeNull();
  });

  return { onSelect };
}

// ---------------------------------------------------------------------------
// beforeEach
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.resetAllMocks();
  defaultDataMocks();
});

// ---------------------------------------------------------------------------
// 1. Options category renders when CategoryBrowser mounts
// ---------------------------------------------------------------------------

describe('Options category header', () => {
  it('is visible after loading completes', async () => {
    await renderAndWait();

    // The "OPTIONS" heading (uppercase via CSS, but text content is "Options")
    const optionsBtn = screen.getByRole('button', { name: /options/i });
    expect(optionsBtn).toBeDefined();
  });

  it('is collapsed by default', async () => {
    await renderAndWait();

    // Roots are NOT in DOM before expansion
    expect(screen.queryByText('SP 500')).toBeNull();
    expect(screen.queryByText('Gold')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Expanding Options fetches roots and renders each
// ---------------------------------------------------------------------------

describe('Options expansion', () => {
  it('fetches roots and renders all three when expanded', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('SP 500')).toBeDefined();
      expect(screen.getByText('Gold')).toBeDefined();
      expect(screen.getByText('T-Note 10Y')).toBeDefined();
    });

    expect(getOptionRoots).toHaveBeenCalledOnce();
  });

  it('renders Greeks badge for roots with has_greeks=true', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      // Two roots have has_greeks=true → two "Greeks" badges
      const badges = screen.getAllByText('Greeks');
      expect(badges.length).toBe(2);
    });
  });
});

describe('Greeks badge — stored_greeks_ratio variants', () => {
  beforeEach(() => {
    defaultDataMocks();
  });

  async function renderWithRoots(roots) {
    vi.mocked(getOptionRoots).mockResolvedValue({ roots });
    renderWithClient(<CategoryBrowser selected={null} onSelect={vi.fn()} />);
    await waitFor(() => {
      expect(screen.queryByText('Loading instruments...')).toBeNull();
    });
    fireEvent.click(screen.getByRole('button', { name: /options/i }));
  }

  function root(overrides = {}) {
    return {
      collection: 'OPT_X',
      name: 'X',
      has_greeks: true,
      providers: ['IVOLATILITY'],
      expiration_first: '2020-01-01',
      expiration_last: '2025-12-31',
      last_trade_date: '2024-12-31',
      doc_count_estimated: 1,
      strike_factor_verified: true,
      stored_greeks_ratio: 1.0,
      has_computed_greeks: true,
      ...overrides,
    };
  }

  it('solid green "Greeks" when stored_greeks_ratio >= 0.9 (e.g. OPT_SP_500 at 99.7%)', async () => {
    await renderWithRoots([
      root({ collection: 'OPT_SP_500', name: 'SP 500', stored_greeks_ratio: 0.997 }),
    ]);
    const badge = await screen.findByText('Greeks');
    expect(badge.className).toMatch(/greeksBadge_/);
    expect(badge.className).not.toMatch(/greeksBadgePartial|greeksBadgeComputed/);
  });

  it('split "Greeks" when 0.1 <= ratio < 0.9 (e.g. OPT_BTC at 37%)', async () => {
    await renderWithRoots([
      root({ collection: 'OPT_BTC', name: 'BTC', stored_greeks_ratio: 0.37 }),
    ]);
    const badge = await screen.findByText('Greeks');
    expect(badge.className).toMatch(/greeksBadgePartial/);
  });

  it('split "Greeks" for OPT_JPYUSD at 30%', async () => {
    await renderWithRoots([
      root({ collection: 'OPT_JPYUSD', name: 'JPY USD', stored_greeks_ratio: 0.30 }),
    ]);
    expect((await screen.findByText('Greeks')).className).toMatch(/greeksBadgePartial/);
  });

  it('gray "Comp. Greeks" when ratio < 0.1 but has_computed_greeks=true (e.g. OPT_VIX)', async () => {
    await renderWithRoots([
      root({
        collection: 'OPT_VIX',
        name: 'VIX',
        stored_greeks_ratio: 0.0,
        has_computed_greeks: true,
      }),
    ]);
    const badge = await screen.findByText('Comp. Greeks');
    expect(badge.className).toMatch(/greeksBadgeComputed/);
    // The plain "Greeks" badge is NOT rendered for VIX.
    expect(screen.queryByText(/^Greeks$/)).toBeNull();
  });

  it('no badge when ratio < 0.1 and has_computed_greeks=false (e.g. OPT_ETH)', async () => {
    await renderWithRoots([
      root({
        collection: 'OPT_ETH',
        name: 'ETH',
        stored_greeks_ratio: 0.0,
        has_computed_greeks: false,
        has_greeks: false,
      }),
    ]);
    // Root rendered, no greek-related badge.
    expect(await screen.findByText('ETH')).toBeDefined();
    expect(screen.queryByText(/Greeks/)).toBeNull();
  });

  it('legacy fallback: has_greeks=true with no ratio fields → solid Greeks badge', async () => {
    // Older API responses (or tests) without the Phase 3 fields still work.
    await renderWithRoots([
      {
        collection: 'OPT_LEGACY',
        name: 'Legacy',
        has_greeks: true,
        providers: ['IVOLATILITY'],
        expiration_first: '2020-01-01',
        expiration_last: '2025-12-31',
        last_trade_date: '2024-12-31',
        doc_count_estimated: 1,
        strike_factor_verified: true,
        // no stored_greeks_ratio, no has_computed_greeks
      },
    ]);
    const badge = await screen.findByText('Greeks');
    expect(badge.className).toMatch(/greeksBadge_/);
    expect(badge.className).not.toMatch(/greeksBadgePartial|greeksBadgeComputed/);
  });
});

// ---------------------------------------------------------------------------
// 3. Verification-pending badge — intentionally suppressed
// ---------------------------------------------------------------------------

describe('Verification-pending badge', () => {
  it('is never rendered, even when a root has strike_factor_verified=false', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      // OPT_T_NOTE_10_Y in MOCK_ROOTS has strike_factor_verified=false but
      // we no longer surface the warning badge.
      expect(screen.getByText('T-Note 10Y')).toBeDefined();
    });

    expect(screen.queryAllByText('Verification pending').length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 4. Click root → onSelect called with correct shape
// ---------------------------------------------------------------------------

describe('Root click emits selection', () => {
  it('emits { type: "option", collection, instrument_id: null, expiry: null, strike: null, optionType: null }', async () => {
    const { onSelect } = await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('SP 500')).toBeDefined();
    });

    // Click the OPT_SP_500 root button
    const sp500Btn = screen.getByText('SP 500').closest('button');
    fireEvent.click(sp500Btn);

    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith({
      type: 'option',
      collection: 'OPT_SP_500',
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
      last_trade_date: '2024-12-31',
      expiration_last: '2025-12-31',
    });
  });

  it('emits correct collection for Gold root', async () => {
    const { onSelect } = await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('Gold')).toBeDefined();
    });

    const goldBtn = screen.getByText('Gold').closest('button');
    fireEvent.click(goldBtn);

    expect(onSelect).toHaveBeenCalledWith({
      type: 'option',
      collection: 'OPT_GOLD',
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
      last_trade_date: '2024-12-31',
      expiration_last: '2025-12-31',
    });
  });

  it('marks root as active when selection matches', async () => {
    const selected = {
      type: 'option',
      collection: 'OPT_SP_500',
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
    };

    const { onSelect } = await renderAndWait({ selected });

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('SP 500')).toBeDefined();
    });

    // The active root button should carry the active CSS module class.
    const sp500Btn = screen.getByText('SP 500').closest('button');
    // CSS modules mangle class names in test; check the element has some class
    // that includes the word "Active" in its generated name.
    const classNames = sp500Btn.className;
    expect(classNames).toMatch(/Active/);
  });
});

// ---------------------------------------------------------------------------
// 5. Empty roots — graceful placeholder
// ---------------------------------------------------------------------------

describe('Empty options roots', () => {
  it('shows placeholder when getOptionRoots returns empty array', async () => {
    vi.mocked(getOptionRoots).mockResolvedValueOnce({ roots: [] });

    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('No options roots available')).toBeDefined();
    });
  });
});

// ---------------------------------------------------------------------------
// 6. SWR: no spinner on re-navigation (the primary nav-spinner this layer kills)
// ---------------------------------------------------------------------------

describe('SWR — instant render on re-navigation', () => {
  it('second mount (navigate back) shows NO "Loading instruments..." flash', async () => {
    // One shared client == the app-wide cache surviving across route changes.
    // staleTime mirrors production (collections are slow-changing) so a
    // re-navigation within the window is served purely from cache.
    const client = makeTestClient();
    client.setDefaultOptions({
      queries: { retry: false, gcTime: Infinity, staleTime: 5 * 60 * 1000 },
    });

    // First visit to /data: cold cache → loads once.
    const first = renderWithClient(
      <CategoryBrowser selected={null} onSelect={vi.fn()} />,
      { client },
    );
    expect(first.getByText('Loading instruments...')).toBeDefined();
    await waitFor(() =>
      expect(first.queryByText('Loading instruments...')).toBeNull(),
    );
    first.unmount(); // navigate away

    const callsAfterFirst = vi.mocked(listCollections).mock.calls.length;

    // Navigate back: warm cache → the very first render must already show the
    // category tree, never the loading placeholder.
    const second = renderWithClient(
      <CategoryBrowser selected={null} onSelect={vi.fn()} />,
      { client },
    );
    expect(second.queryByText('Loading instruments...')).toBeNull();
    // The Options header (always present once categories render) is there.
    expect(second.getByRole('button', { name: /options/i })).toBeDefined();
    // No immediate refetch within the staleTime window (served from cache).
    expect(vi.mocked(listCollections).mock.calls.length).toBe(callsAfterFirst);
  });
});
