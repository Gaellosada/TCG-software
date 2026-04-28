// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
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

  render(<CategoryBrowser selected={selected} onSelect={onSelect} />);

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

// ---------------------------------------------------------------------------
// 3. Verification-pending badge for strike_factor_verified=false
// ---------------------------------------------------------------------------

describe('Verification-pending badge', () => {
  it('shows badge only for roots with strike_factor_verified=false', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      const badges = screen.getAllByText('Verification pending');
      // Only OPT_T_NOTE_10_Y has strike_factor_verified=false
      expect(badges.length).toBe(1);
    });
  });

  it('badge carries correct title attribute for hover tooltip', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      const badge = screen.getByText('Verification pending');
      expect(badge.getAttribute('title')).toContain('Strike factor verification pending');
      expect(badge.getAttribute('title')).toContain('bond/rate option strikes');
    });
  });

  it('does NOT show badge for roots with strike_factor_verified=true', async () => {
    await renderAndWait();

    const optionsBtn = screen.getByRole('button', { name: /options/i });
    fireEvent.click(optionsBtn);

    await waitFor(() => {
      expect(screen.getByText('SP 500')).toBeDefined();
    });

    // One badge only (for T-Note, not SP500 or Gold)
    const badges = screen.queryAllByText('Verification pending');
    expect(badges.length).toBe(1);
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
