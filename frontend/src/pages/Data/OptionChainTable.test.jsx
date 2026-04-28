// @vitest-environment jsdom
//
// Tests for OptionChainTable.
//
// Mocks:
//   - ../../api/options : the underlying fetcher (called by useOptionsChain).
//   - ./useOptionsChain : the hook itself, so we can drive chain data deterministically.
//
// The visual rules (stored normal, computed italic + ⓒ badge, missing em-dash)
// are exercised against ComputeResultCell directly, while the rest of the
// component is exercised through render() with a controlled hook stub.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mock useOptionsChain — it owns filters + chainData and is called by the
// component. We expose mutable references so each test can wire its preferred
// behaviour before render().
// ---------------------------------------------------------------------------

const hookState = {
  filters: null,
  chainData: null,
  loading: false,
  fetchChain: vi.fn(),
  updateFilters: vi.fn(),
  abort: vi.fn(),
};

vi.mock('./useOptionsChain', () => ({
  useOptionsChain: () => hookState,
}));

vi.mock('../../api/options', () => ({
  getOptionChain: vi.fn(),
  getOptionContract: vi.fn(),
}));

// Import AFTER vi.mock so the stub is wired.
import OptionChainTable, { ComputeResultCell } from './OptionChainTable';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildFilters(overrides = {}) {
  return {
    root: 'OPT_SP_500',
    date: '2024-03-15',
    type: 'both',
    expirationMin: '2024-03-15',
    expirationMax: '2024-06-15',
    strikeMin: null,
    strikeMax: null,
    computeMissing: false,
    ...overrides,
  };
}

function makeRow({
  contract_id = 'C1',
  expiration = '2024-04-19',
  type = 'C',
  strike = 5000,
  bid = 510.5,
  ask = 511.0,
  mid = 510.75,
  open_interest = 123,
  iv = stored(0.18),
  delta = stored(0.95),
  gamma = computed(0.001),
  theta = missing('missing_forward_vix_curve', 'Forward VIX curve unavailable'),
  vega = stored(2.0),
} = {}) {
  return {
    contract_id,
    expiration,
    type,
    strike,
    K_over_S: 0.909,
    bid,
    ask,
    mid,
    open_interest,
    iv,
    delta,
    gamma,
    theta,
    vega,
  };
}

function stored(value) {
  return {
    value,
    source: 'stored',
    model: null,
    inputs_used: null,
    missing_inputs: null,
    error_code: null,
    error_detail: null,
  };
}

function computed(value) {
  return {
    value,
    source: 'computed',
    model: 'Black-76',
    inputs_used: { underlying_price: 5500, iv: 0.18, ttm: 0.1, r: 0, sign: 'c', kernel: 'BS76Kernel' },
    missing_inputs: null,
    error_code: null,
    error_detail: null,
  };
}

function missing(error_code, error_detail) {
  return {
    value: null,
    source: 'missing',
    model: null,
    inputs_used: null,
    missing_inputs: ['forward_vix_curve'],
    error_code,
    error_detail,
  };
}

// ---------------------------------------------------------------------------
// Reset hook state and mocks before each test.
// ---------------------------------------------------------------------------

beforeEach(() => {
  hookState.filters = buildFilters();
  hookState.chainData = null;
  hookState.loading = false;
  hookState.fetchChain = vi.fn();
  hookState.updateFilters = vi.fn();
  hookState.abort = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// ComputeResultCell unit tests — visual rules
// ---------------------------------------------------------------------------

describe('<ComputeResultCell> visual rules', () => {
  it('stored value renders formatted with no italic / no badge', () => {
    const { container } = render(<ComputeResultCell result={stored(0.18)} decimals={4} />);
    expect(container.textContent).toBe('0.1800');
    // No italic styling marker → no element with the computed-style class.
    expect(container.querySelector('[class*="computed"]')).toBeNull();
    // No ⓒ badge.
    expect(container.textContent).not.toContain('ⓒ');
  });

  it('computed value renders italic with ⓒ badge', () => {
    const { container } = render(
      <ComputeResultCell result={computed(0.001)} decimals={4} />,
    );
    expect(container.textContent).toContain('0.0010');
    expect(container.textContent).toContain('ⓒ');
    // Wrapping span carries the computed-styled class.
    const span = container.querySelector('span');
    expect(span.className).toMatch(/computed/i);
  });

  it('computed value tooltip mentions model and inputs', () => {
    const { container } = render(
      <ComputeResultCell result={computed(0.001)} decimals={4} />,
    );
    const span = container.querySelector('span[title]');
    expect(span).not.toBeNull();
    const title = span.getAttribute('title');
    expect(title).toContain('Black-76');
    expect(title).toContain('F = 5500');
    expect(title).toContain('IV = 0.18');
    expect(title).toContain('T = 0.1 yr');
    expect(title).toContain('r = 0');
  });

  it('missing value renders em-dash with error tooltip', () => {
    const { container } = render(
      <ComputeResultCell
        result={missing('missing_forward_vix_curve', 'Forward VIX curve unavailable')}
        decimals={4}
      />,
    );
    expect(container.textContent).toBe('—');
    const span = container.querySelector('span[title]');
    expect(span.getAttribute('title')).toBe(
      'missing_forward_vix_curve: Forward VIX curve unavailable',
    );
  });

  it('null result defensively renders em-dash', () => {
    const { container } = render(<ComputeResultCell result={null} />);
    expect(container.textContent).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// OptionChainTable behaviour
// ---------------------------------------------------------------------------

describe('<OptionChainTable> rendering rows', () => {
  it('renders one row per chain entry with Greek cells', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', strike: 5000 }),
        makeRow({ contract_id: 'C2', strike: 5100 }),
      ],
      notes: [],
    };

    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);

    // 2 data rows + 1 header row.
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(screen.getByText('5000.00')).toBeTruthy();
    expect(screen.getByText('5100.00')).toBeTruthy();
  });

  it('stored Greek renders in normal style (no italic class on the value span)', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [makeRow({ iv: stored(0.18) })],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    // The IV cell text "0.1800" should appear unwrapped in computed-italic class.
    expect(container.textContent).toContain('0.1800');
    // The container should not contain the ⓒ badge for this stored cell.
    // (It might still show the badge somewhere else if other cells are computed —
    // here only one row, only stored values for IV.)
    // Check the IV column does NOT have italic on the IV value:
    const cells = container.querySelectorAll('td');
    // Order: Expiration, Type, Strike, Bid, Ask, Mid, IV, Δ, Γ, Θ, ν, OI
    const ivCell = cells[6];
    expect(ivCell.querySelector('[class*="computed"]')).toBeNull();
  });

  it('computed Greek renders in italic with ⓒ badge', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [makeRow({ gamma: computed(0.001) })],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const cells = container.querySelectorAll('td');
    const gammaCell = cells[8]; // Γ column
    expect(gammaCell.textContent).toContain('ⓒ');
    expect(gammaCell.querySelector('[class*="computed"]')).not.toBeNull();
  });

  it('missing Greek renders em-dash with tooltip', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({
          theta: missing('missing_forward_vix_curve', 'Forward VIX curve unavailable'),
        }),
      ],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const cells = container.querySelectorAll('td');
    const thetaCell = cells[9]; // Θ column
    expect(thetaCell.textContent).toBe('—');
    const span = thetaCell.querySelector('span[title]');
    expect(span.getAttribute('title')).toBe(
      'missing_forward_vix_curve: Forward VIX curve unavailable',
    );
  });
});

describe('<OptionChainTable> compute_missing toggle', () => {
  it('clicking the compute-missing checkbox calls updateFilters({ computeMissing: true })', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [makeRow()],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);

    const checkbox = screen.getByLabelText(/compute missing greeks/i);
    fireEvent.click(checkbox);

    expect(hookState.updateFilters).toHaveBeenCalledWith({ computeMissing: true });
  });
});

describe('<OptionChainTable> row click', () => {
  it('clicking a row calls onRowClick with the expected shape', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({
          contract_id: 'SPY_240419C00500000|M',
          expiration: '2024-04-19',
          strike: 5000,
          type: 'C',
        }),
      ],
      notes: [],
    };
    const onRowClick = vi.fn();

    render(<OptionChainTable root="OPT_SP_500" onRowClick={onRowClick} />);

    // Find the data row (skip the header row).
    const rows = screen.getAllByRole('row');
    fireEvent.click(rows[1]);

    expect(onRowClick).toHaveBeenCalledWith({
      collection: 'OPT_SP_500',
      instrument_id: 'SPY_240419C00500000|M',
      expiry: '2024-04-19',
      strike: 5000,
      optionType: 'C',
    });
  });
});

describe('<OptionChainTable> verification-pending banner', () => {
  it('appears for OPT_T_NOTE_10_Y root', () => {
    hookState.filters = buildFilters({ root: 'OPT_T_NOTE_10_Y' });
    hookState.chainData = {
      root: 'OPT_T_NOTE_10_Y',
      date: '2024-03-15',
      underlying_price: stored(110),
      rows: [makeRow()],
      notes: [],
    };
    render(<OptionChainTable root="OPT_T_NOTE_10_Y" onRowClick={() => {}} />);
    expect(
      screen.getByText(/strike factor verification pending/i),
    ).toBeTruthy();
  });

  it('does not appear for OPT_SP_500 root', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [makeRow()],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    expect(screen.queryByText(/strike factor verification pending/i)).toBeNull();
  });
});

describe('<OptionChainTable> error / empty states', () => {
  it('renders an error message when chainData.error is present', () => {
    hookState.chainData = { error: new Error('boom') };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    expect(screen.getByText(/failed to load chain/i)).toBeTruthy();
    expect(screen.getByText(/boom/)).toBeTruthy();
  });

  it('renders empty-state when chain has no rows', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    expect(screen.getByText(/no contracts match/i)).toBeTruthy();
  });
});
