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
  getOptionExpirations: vi.fn().mockResolvedValue({
    root: 'OPT_SP_500',
    expirations: ['2024-04-19', '2024-05-17', '2024-06-21'],
  }),
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
  expiration_cycle = '',
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
    expiration_cycle,
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

    // 2 data rows + 2 grouped-header rows (Calls/Puts on top, fields below).
    expect(screen.getAllByRole('row')).toHaveLength(4);
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
    const cells = container.querySelectorAll('td');
    // Canonical chain: Exp, [call: Bid Ask Mid IV Δ Γ Θ ν OI], C, Strike, P,
    // [put: Bid Ask Mid IV Δ Γ Θ ν OI]. Call IV → cells[4].
    const ivCell = cells[4];
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
    const gammaCell = cells[6]; // call Γ column
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
    const thetaCell = cells[7]; // call Θ column
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

    // Skip the 2 grouped-header rows. Clicking the row directly (not a
    // specific cell) defaults to the call side via the side-aware handler.
    const rows = screen.getAllByRole('row');
    fireEvent.click(rows[2]);

    expect(onRowClick).toHaveBeenCalledWith({
      collection: 'OPT_SP_500',
      instrument_id: 'SPY_240419C00500000|M',
      expiry: '2024-04-19',
      strike: 5000,
      optionType: 'C',
    });
  });
});

// ---------------------------------------------------------------------------
// Persistent selection highlight — when a contract is open in the detail
// panel, its (call|put) row stays in the hover-tinted state so the user
// can see at a glance which row the panel below corresponds to.
// ---------------------------------------------------------------------------

describe('<OptionChainTable> persistent selection', () => {
  it('flags the selected row + side via data-selected-side', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C-CALL', expiration: '2024-04-19', strike: 5000, type: 'C' }),
        makeRow({ contract_id: 'C-PUT', expiration: '2024-04-19', strike: 5000, type: 'P' }),
      ],
      notes: [],
    };
    const selectedContract = {
      collection: 'OPT_SP_500',
      instrument_id: 'C-PUT',
    };
    const { container } = render(
      <OptionChainTable
        root="OPT_SP_500"
        onRowClick={() => {}}
        selectedContract={selectedContract}
      />,
    );
    const selected = container.querySelectorAll('tr[data-selected-side]');
    expect(selected.length).toBe(1);
    expect(selected[0].getAttribute('data-selected-side')).toBe('put');
  });

  it('renders no data-selected-side when no contract is selected', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C-CALL', expiration: '2024-04-19', strike: 5000, type: 'C' }),
      ],
      notes: [],
    };
    const { container } = render(
      <OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />,
    );
    expect(container.querySelectorAll('tr[data-selected-side]').length).toBe(0);
  });

  it('ignores selectedContract from a different collection (cross-root safety)', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C-CALL', expiration: '2024-04-19', strike: 5000, type: 'C' }),
      ],
      notes: [],
    };
    const selectedContract = {
      collection: 'OPT_NDX',  // different root
      instrument_id: 'C-CALL',
    };
    const { container } = render(
      <OptionChainTable
        root="OPT_SP_500"
        onRowClick={() => {}}
        selectedContract={selectedContract}
      />,
    );
    expect(container.querySelectorAll('tr[data-selected-side]').length).toBe(0);
  });
});

describe('<OptionChainTable> verification-pending banner', () => {
  it('is intentionally suppressed even on unverified roots', () => {
    // The strike-factor banner was removed from the chain table; even
    // for the historically-unverified roots (OPT_T_NOTE_10_Y, etc.) we
    // should NOT surface the warning.
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
      screen.queryByText(/strike factor verification pending/i),
    ).toBeNull();
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

// ---------------------------------------------------------------------------
// Cycle chip — conditional rendering
// ---------------------------------------------------------------------------

describe('<OptionChainTable> cycle chip', () => {
  it('single-cycle chain: chip is still rendered (always-on policy)', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'Monthly' }),
        makeRow({ contract_id: 'C2', expiration: '2024-05-17', expiration_cycle: 'Monthly' }),
      ],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const chips = container.querySelectorAll('[data-testid="cycle-chip"]');
    expect(chips.length).toBe(2);
    chips.forEach((chip) => {
      expect(chip.getAttribute('title')).toBe('Monthly');
      expect(chip.textContent).toBe('M');
    });
  });

  it('multi-cycle chain: chip rendered with 1-letter abbreviation and full title', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'Monthly' }),
        makeRow({ contract_id: 'C2', expiration: '2024-04-26', expiration_cycle: 'W3 Friday' }),
      ],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const chips = container.querySelectorAll('[data-testid="cycle-chip"]');
    // One merged row per distinct expiration (2 here, different expirations).
    expect(chips.length).toBeGreaterThanOrEqual(1);
    // First row expiration_cycle = 'Monthly' → abbreviation 'M'.
    const monthlyChip = [...chips].find((c) => c.getAttribute('title') === 'Monthly');
    expect(monthlyChip).toBeTruthy();
    expect(monthlyChip.textContent).toBe('M');
    // Second row expiration_cycle = 'W3 Friday' → abbreviation 'W'.
    const weeklyChip = [...chips].find((c) => c.getAttribute('title') === 'W3 Friday');
    expect(weeklyChip).toBeTruthy();
    expect(weeklyChip.textContent).toBe('W');
  });

  it('multi-cycle chain: row with empty expiration_cycle gets no chip', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'Monthly' }),
        makeRow({ contract_id: 'C2', expiration: '2024-04-26', expiration_cycle: 'W3 Friday' }),
        makeRow({ contract_id: 'C3', expiration: '2024-05-03', expiration_cycle: '' }),
      ],
      notes: [],
    };
    const { container } = render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const chips = container.querySelectorAll('[data-testid="cycle-chip"]');
    // Only 2 chips — the empty-cycle row gets none.
    expect(chips.length).toBe(2);
    // No chip with empty title.
    const emptyTitleChip = [...chips].find((c) => !c.getAttribute('title'));
    expect(emptyTitleChip).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Cycle dropdown — opt-in filter on the chain filter row
// ---------------------------------------------------------------------------

describe('<OptionChainTable> cycle dropdown', () => {
  it('renders the dropdown with "All cycles" sentinel + one option per distinct cycle', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'M' }),
        makeRow({ contract_id: 'C2', expiration: '2024-04-26', expiration_cycle: 'W' }),
        makeRow({ contract_id: 'C3', expiration: '2024-05-17', expiration_cycle: 'M' }),
      ],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);

    const select = screen.getByLabelText(/^cycle$/i);
    const optionValues = [...select.querySelectorAll('option')].map((o) => o.value);
    // "" sentinel = All cycles, then deduped + sorted distinct cycles.
    expect(optionValues).toEqual(['', 'M', 'W']);
    // Default selection is the "All cycles" sentinel — chain table is opt-in,
    // unlike the smile dropdown which auto-picks the most-populated cycle.
    expect(select.value).toBe('');
  });

  it('changing the dropdown calls updateFilters({ expirationCycle })', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'M' }),
        makeRow({ contract_id: 'C2', expiration: '2024-04-26', expiration_cycle: 'W' }),
      ],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);

    const select = screen.getByLabelText(/^cycle$/i);
    fireEvent.change(select, { target: { value: 'W' } });
    expect(hookState.updateFilters).toHaveBeenCalledWith({ expirationCycle: 'W' });
  });

  it('selecting "All cycles" sends null (clears the filter, no empty-string leak)', () => {
    hookState.filters = buildFilters({ expirationCycle: 'M' });
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'M' }),
        makeRow({ contract_id: 'C2', expiration: '2024-04-26', expiration_cycle: 'W' }),
      ],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);

    const select = screen.getByLabelText(/^cycle$/i);
    fireEvent.change(select, { target: { value: '' } });
    expect(hookState.updateFilters).toHaveBeenCalledWith({ expirationCycle: null });
  });

  it('single-cycle chain: dropdown still renders (with one cycle option)', () => {
    hookState.chainData = {
      root: 'OPT_SP_500',
      date: '2024-03-15',
      underlying_price: stored(5500),
      rows: [
        makeRow({ contract_id: 'C1', expiration: '2024-04-19', expiration_cycle: 'M' }),
      ],
      notes: [],
    };
    render(<OptionChainTable root="OPT_SP_500" onRowClick={() => {}} />);
    const select = screen.getByLabelText(/^cycle$/i);
    const optionValues = [...select.querySelectorAll('option')].map((o) => o.value);
    expect(optionValues).toEqual(['', 'M']);
  });
});
