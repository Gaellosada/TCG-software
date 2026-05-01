// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';
import DataPage from './DataPage';

// ---------------------------------------------------------------------------
// Mock CategoryBrowser — captures onSelect so tests can drive selections.
// ---------------------------------------------------------------------------
let capturedOnSelect = null;
vi.mock('./CategoryBrowser', () => ({
  default: vi.fn(({ onSelect }) => {
    capturedOnSelect = onSelect;
    return <div data-testid="category-browser" />;
  }),
}));

// ---------------------------------------------------------------------------
// Mock PriceChart and ContinuousChart to avoid real fetch / chart setup.
// ---------------------------------------------------------------------------
vi.mock('./PriceChart', () => ({
  default: vi.fn(({ collection, instrument }) => (
    <div data-testid="price-chart" data-collection={collection} data-instrument={instrument} />
  )),
}));

vi.mock('./ContinuousChart', () => ({
  default: vi.fn(({ collection }) => (
    <div data-testid="continuous-chart" data-collection={collection} />
  )),
}));

// ---------------------------------------------------------------------------
// Mock C2.2 deliverables — OptionChainTable and ContractDetailPanel.
// These may not be in the tree yet; mocking them makes the test suite
// independent of C2.2 landing time.
// ---------------------------------------------------------------------------
let capturedOnRowClick = null;
let capturedChainTableProps = null;
vi.mock('./OptionChainTable', () => ({
  default: vi.fn((props) => {
    capturedOnRowClick = props.onRowClick;
    capturedChainTableProps = props;
    return <div data-testid="option-chain-table" data-root={props.root} />;
  }),
}));

let capturedOnClose = null;
vi.mock('./ContractDetailPanel', () => ({
  default: vi.fn(({ collection, instrumentId, onClose }) => {
    capturedOnClose = onClose;
    return (
      <div
        data-testid="contract-detail-panel"
        data-collection={collection}
        data-instrument-id={instrumentId}
      />
    );
  }),
}));

// ---------------------------------------------------------------------------
// Mock useOptionExpirations — drives the Smile tab's expiration <select>.
// Tests that need a specific expiration to be selectable should re-stub
// `mockExpirations` to include that value before render.
// ---------------------------------------------------------------------------
let mockExpirations = ['2024-12-20', '2026-04-27', '2026-05-15', '2030-12-20'];
vi.mock('./useOptionExpirations', () => ({
  useOptionExpirations: vi.fn(() => ({
    expirations: mockExpirations,
    loading: false,
    error: null,
  })),
}));

// ---------------------------------------------------------------------------
// Mock ChainSnapshotPanel — captures props so tests can invoke onClose.
// We also expose a `__resolveSnapshotData` test hook so tests can drive the
// onSnapshotData callback DataPage uses to populate the cycle dropdown.
// ---------------------------------------------------------------------------
let capturedSnapshotProps = null;
vi.mock('./ChainSnapshotPanel', () => ({
  default: vi.fn((props) => {
    capturedSnapshotProps = props;
    return (
      <div
        data-testid="chain-snapshot-panel"
        data-root={props.root}
        data-date={props.date}
        data-type={props.type}
        data-expiration={props.expiration}
        data-expiration-cycle={props.expiration_cycle == null ? '' : props.expiration_cycle}
      />
    );
  }),
}));


// ---------------------------------------------------------------------------
// Lifecycle — clean up DOM between each test.
// ---------------------------------------------------------------------------
beforeEach(() => {
  capturedOnSelect = null;
  capturedOnRowClick = null;
  capturedOnClose = null;
  capturedSnapshotProps = null;
  capturedChainTableProps = null;
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderDataPage() {
  return render(<DataPage />);
}

function typeSnapshotExpiration(value = '2024-12-20') {
  // Snapshot tab gates panel rendering on a non-empty expiration input.
  const input = screen.getByLabelText('Expiration');
  act(() => {
    fireEvent.change(input, { target: { value } });
  });
}

function selectOption(collection = 'OPT_SP_500', overrides = {}) {
  act(() => {
    capturedOnSelect({
      type: 'option',
      collection,
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
      // CategoryBrowser threads the root's metadata through the emit
      // (used by DataPage to default the chain query date / window).
      // Tests that don't care still get a sensible value here so the
      // chain UI renders.
      last_trade_date: '2026-04-27',
      expiration_last: '2030-12-20',
      ...overrides,
    });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DataPage — welcome state', () => {
  it('shows welcome message when nothing is selected', () => {
    renderDataPage();
    expect(screen.getByText('Select an instrument')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
    expect(screen.queryByTestId('price-chart')).toBeNull();
    expect(screen.queryByTestId('continuous-chart')).toBeNull();
  });
});

describe('DataPage — existing dispatch (regression)', () => {
  it('renders PriceChart for non-continuous, non-option selection', () => {
    renderDataPage();
    act(() => {
      capturedOnSelect({ type: 'spot', collection: 'GOLD', symbol: 'GLD' });
    });
    expect(screen.getByTestId('price-chart')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
  });

  it('renders ContinuousChart for type=continuous', () => {
    renderDataPage();
    act(() => {
      capturedOnSelect({ type: 'continuous', collection: 'ES' });
    });
    expect(screen.getByTestId('continuous-chart')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
  });
});

describe('DataPage — option dispatch: chain-only view', () => {
  it('renders OptionChainTable when selected.type === option and instrument_id is null', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    const table = screen.getByTestId('option-chain-table');
    expect(table).toBeTruthy();
    expect(table.dataset.root).toBe('OPT_SP_500');
  });

  it('does NOT render ContractDetailPanel when no contract is selected', () => {
    renderDataPage();
    selectOption();
    expect(screen.queryByTestId('contract-detail-panel')).toBeNull();
  });

  it('does not render PriceChart or ContinuousChart for option selection', () => {
    renderDataPage();
    selectOption();
    expect(screen.queryByTestId('price-chart')).toBeNull();
    expect(screen.queryByTestId('continuous-chart')).toBeNull();
  });
});

describe('DataPage — option dispatch: contract detail split view', () => {
  it('renders ContractDetailPanel when a contract row is clicked in OptionChainTable', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    const contract = {
      collection: 'OPT_SP_500',
      instrument_id: 'SPX|2024-12-20|4500|C',
      expiry: '2024-12-20',
      strike: 4500,
      optionType: 'C',
    };

    act(() => {
      capturedOnRowClick(contract);
    });

    const panel = screen.getByTestId('contract-detail-panel');
    expect(panel).toBeTruthy();
    expect(panel.dataset.collection).toBe('OPT_SP_500');
    expect(panel.dataset.instrumentId).toBe('SPX|2024-12-20|4500|C');
  });

  it('still renders OptionChainTable alongside ContractDetailPanel', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      capturedOnRowClick({
        collection: 'OPT_SP_500',
        instrument_id: 'SPX|2024-12-20|4500|C',
      });
    });

    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.getByTestId('contract-detail-panel')).toBeTruthy();
  });

  it('scrolls the contract-detail panel into view on contract click', () => {
    // jsdom does not implement scrollIntoView; install a spy on the
    // prototype before render so the effect can call it.  The DataPage
    // already feature-tests for the function so production users on
    // browsers without scrollIntoView (none in practice) silently no-op.
    const spy = vi.fn();
    Element.prototype.scrollIntoView = spy;

    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      capturedOnRowClick({
        collection: 'OPT_SP_500',
        instrument_id: 'SPX|2024-12-20|4500|C',
      });
    });

    expect(spy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
  });
});

describe('DataPage — ContractDetailPanel close', () => {
  it('hides ContractDetailPanel when onClose is invoked', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      capturedOnRowClick({
        collection: 'OPT_SP_500',
        instrument_id: 'SPX|2024-12-20|4500|C',
      });
    });

    expect(screen.getByTestId('contract-detail-panel')).toBeTruthy();

    act(() => {
      capturedOnClose();
    });

    expect(screen.queryByTestId('contract-detail-panel')).toBeNull();
    // Chain table still visible
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
  });
});

describe('DataPage — root switch resets selectedContract', () => {
  it('clears ContractDetailPanel when user switches to a different options root', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      capturedOnRowClick({
        collection: 'OPT_SP_500',
        instrument_id: 'SPX|2024-12-20|4500|C',
      });
    });

    expect(screen.getByTestId('contract-detail-panel')).toBeTruthy();

    // Switch root
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_NASDAQ_100',
        instrument_id: null,
        expiry: null,
        strike: null,
        optionType: null,
        last_trade_date: '2026-04-27',
        expiration_last: '2030-12-20',
      });
    });

    expect(screen.queryByTestId('contract-detail-panel')).toBeNull();
    const table = screen.getByTestId('option-chain-table');
    expect(table.dataset.root).toBe('OPT_NASDAQ_100');
  });

  it('clears selectedContract when switching away from option to a non-option selection then back', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      capturedOnRowClick({
        collection: 'OPT_SP_500',
        instrument_id: 'SPX|2024-12-20|4500|C',
      });
    });

    // Switch away
    act(() => {
      capturedOnSelect({ type: 'spot', collection: 'GOLD', symbol: 'GLD' });
    });

    // Come back to same option root
    selectOption('OPT_SP_500');

    // No stale contract detail
    expect(screen.queryByTestId('contract-detail-panel')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tier-2 tab switching
// ---------------------------------------------------------------------------

describe('DataPage — Tier-2 tab strip initial state', () => {
  it('shows Chain tab as active and renders OptionChainTable by default', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    // Chain tab active (aria-selected)
    const chainTab = screen.getByRole('tab', { name: 'Contracts' });
    expect(chainTab.getAttribute('aria-selected')).toBe('true');

    // OptionChainTable present
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    // Tier-2 panel absent
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
  });

  it('shows all three tab buttons', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    expect(screen.getByRole('tab', { name: 'Contracts' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Continuous' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Smile' })).toBeTruthy();
  });

  it('Continuous tab shows the coming-soon placeholder when selected', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Continuous' }));
    });

    expect(screen.getByTestId('continuous-empty')).toBeTruthy();
    expect(screen.getByText(/coming soon/i)).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
  });
});

describe('DataPage — switching to Smile tab', () => {
  it('renders ChainSnapshotPanel (after expiration typed) and hides OptionChainTable', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    typeSnapshotExpiration();

    expect(screen.getByTestId('chain-snapshot-panel')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
  });

  it('passes root from selected.collection to ChainSnapshotPanel', () => {
    renderDataPage();
    selectOption('OPT_GOLD');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    typeSnapshotExpiration();

    const panel = screen.getByTestId('chain-snapshot-panel');
    expect(panel.dataset.root).toBe('OPT_GOLD');
  });

  it('Smile tab is aria-selected after clicking', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    expect(screen.getByRole('tab', { name: 'Smile' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: 'Contracts' }).getAttribute('aria-selected')).toBe('false');
  });
});

describe('DataPage — switching back to Chain tab', () => {
  it('restores OptionChainTable after switching away then back', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    // Switch to Smile
    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    expect(screen.queryByTestId('option-chain-table')).toBeNull();

    // Switch back to Contracts
    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Contracts' }));
    });
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
  });
});

describe('DataPage — ChainSnapshotPanel close button returns to Chain tab', () => {
  it('invokes onClose prop which returns to chain tab', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    typeSnapshotExpiration();

    expect(screen.getByTestId('chain-snapshot-panel')).toBeTruthy();

    // Invoke the onClose prop that DataPage passed to ChainSnapshotPanel
    act(() => {
      capturedSnapshotProps.onClose();
    });

    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Contracts' }).getAttribute('aria-selected')).toBe('true');
  });
});

describe('DataPage — Snapshot tab filter inputs', () => {
  it('date input passes through to ChainSnapshotPanel via props', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    typeSnapshotExpiration();  // gating

    const panel = screen.getByTestId('chain-snapshot-panel');
    const initialDate = panel.dataset.date;

    const dateInputs = screen.getAllByDisplayValue(initialDate);
    expect(dateInputs.length).toBeGreaterThan(0);

    const newDate = '2024-06-15';
    act(() => {
      fireEvent.change(dateInputs[0], { target: { value: newDate } });
    });

    expect(capturedSnapshotProps.date).toBe(newDate);
  });

  it('expiration text input updates the expiration prop on ChainSnapshotPanel', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    const expirationInput = screen.getByLabelText('Expiration');
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });

    expect(capturedSnapshotProps.expiration).toBe('2024-12-20');
  });
});

// ---------------------------------------------------------------------------
// Regression: zero-contracts and smile errors (2026-04-28)
// ---------------------------------------------------------------------------

describe('DataPage — last_trade_date defaults (zero-contracts regression)', () => {
  it('seeds OptionChainTable.initialFilters from selected.last_trade_date', () => {
    render(<DataPage />);
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_SP_500',
        instrument_id: null,
        expiry: null, strike: null, optionType: null,
        last_trade_date: '2026-04-27',
        expiration_last: '2030-12-20',
      });
    });
    expect(capturedChainTableProps).toBeTruthy();
    expect(capturedChainTableProps.initialFilters).toMatchObject({
      date: '2026-04-27',
      expirationMin: '2026-04-27',
    });
  });

  it('renders a "no data" message when last_trade_date is null', () => {
    render(<DataPage />);
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_BROKEN',
        instrument_id: null,
        expiry: null, strike: null, optionType: null,
        last_trade_date: null,
        expiration_last: null,
      });
    });
    // Loud failure: chain table must NOT render; "no data" copy must.
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
    expect(screen.getByText(/no data available/i)).toBeTruthy();
  });
});

describe('DataPage — smile gating (ApiError regression)', () => {
  function pickRoot() {
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_SP_500',
        instrument_id: null,
        expiry: null, strike: null, optionType: null,
        last_trade_date: '2026-04-27',
        expiration_last: '2030-12-20',
      });
    });
  }

  it('does NOT render ChainSnapshotPanel until expiration is non-empty', () => {
    render(<DataPage />);
    pickRoot();
    fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));

    // Empty expiration → panel must be gated; show empty-state instead.
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
    expect(screen.getByTestId('snapshot-empty')).toBeTruthy();
    expect(capturedSnapshotProps).toBeNull();
  });

  it('renders ChainSnapshotPanel once a non-empty expiration is typed', () => {
    render(<DataPage />);
    pickRoot();
    fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    const expInput = screen.getByLabelText('Expiration');
    fireEvent.change(expInput, { target: { value: '2026-05-15' } });

    expect(screen.queryByTestId('snapshot-empty')).toBeNull();
    expect(screen.getByTestId('chain-snapshot-panel')).toBeTruthy();
    expect(capturedSnapshotProps.expiration).toBe('2026-05-15');
    expect(capturedSnapshotProps.date).toBe('2026-04-27');
  });

  it('treats whitespace-only expiration as empty (still gated)', () => {
    render(<DataPage />);
    pickRoot();
    fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    const expInput = screen.getByLabelText('Expiration');
    fireEvent.change(expInput, { target: { value: '   ' } });

    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
    expect(screen.getByTestId('snapshot-empty')).toBeTruthy();
  });

});

// ---------------------------------------------------------------------------
// Cycle dropdown — smile-cycle filter (eliminates duplicate strike markers).
// ---------------------------------------------------------------------------

describe('DataPage — Smile cycle dropdown', () => {
  function pickAndOpenSmile() {
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_SP_500',
        instrument_id: null,
        expiry: null, strike: null, optionType: null,
        last_trade_date: '2026-04-27',
        expiration_last: '2030-12-20',
      });
    });
    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });
    const expInput = screen.getByLabelText('Expiration');
    act(() => {
      fireEvent.change(expInput, { target: { value: '2026-05-15' } });
    });
  }

  // Synthetic smile response carrying two distinct cycles, with "M"
  // having more points than "W" (so auto-select picks "M").
  const TWO_CYCLE_RESPONSE = {
    root: 'OPT_SP_500',
    date: '2026-04-27',
    underlying_price: { value: 5500, source: 'stored', model: null,
      inputs_used: null, missing_inputs: null, error_code: null,
      error_detail: null },
    series: [
      {
        expiration: '2026-05-15',
        points: [
          { strike: 4900, K_over_S: 0.89, expiration_cycle: 'M',
            value: { value: 0.25, source: 'stored', model: null,
              inputs_used: null, missing_inputs: null, error_code: null,
              error_detail: null } },
          { strike: 5000, K_over_S: 0.91, expiration_cycle: 'M',
            value: { value: 0.20, source: 'stored', model: null,
              inputs_used: null, missing_inputs: null, error_code: null,
              error_detail: null } },
          { strike: 5100, K_over_S: 0.93, expiration_cycle: 'M',
            value: { value: 0.17, source: 'stored', model: null,
              inputs_used: null, missing_inputs: null, error_code: null,
              error_detail: null } },
          { strike: 5000, K_over_S: 0.91, expiration_cycle: 'W',
            value: { value: 0.22, source: 'stored', model: null,
              inputs_used: null, missing_inputs: null, error_code: null,
              error_detail: null } },
        ],
      },
    ],
  };

  it('Cycle dropdown is rendered next to Type/Expiration on the Smile tab', () => {
    render(<DataPage />);
    pickAndOpenSmile();
    // Cycle <select> exists with an "All cycles" sentinel option.
    expect(screen.getByLabelText('Cycle')).toBeTruthy();
  });

  it('starts with no cycle filter — passes expiration_cycle=null on first render', () => {
    render(<DataPage />);
    pickAndOpenSmile();
    // Default state before any data has loaded: no filter (cycle === null)
    expect(capturedSnapshotProps).toBeTruthy();
    expect(capturedSnapshotProps.expiration_cycle ?? null).toBe(null);
  });

  it('auto-selects the most-populated cycle once the smile data loads', () => {
    render(<DataPage />);
    pickAndOpenSmile();
    // Simulate the panel reporting back the smile response.
    act(() => {
      capturedSnapshotProps.onSnapshotData(TWO_CYCLE_RESPONSE);
    });
    // The most-populated cycle in the fixture is 'M' (3 points vs 1).
    expect(capturedSnapshotProps.expiration_cycle).toBe('M');
  });

  it('Cycle dropdown lists the distinct cycles from the response', () => {
    render(<DataPage />);
    pickAndOpenSmile();
    act(() => {
      capturedSnapshotProps.onSnapshotData(TWO_CYCLE_RESPONSE);
    });
    const select = screen.getByLabelText('Cycle');
    const optionValues = Array.from(select.querySelectorAll('option'))
      .map((o) => o.value);
    // "" is the "All cycles" sentinel; M and W are the cycles in the
    // response.
    expect(optionValues).toEqual(expect.arrayContaining(['', 'M', 'W']));
  });

  it('selecting "All cycles" clears the filter (passes null to panel)', () => {
    render(<DataPage />);
    pickAndOpenSmile();
    act(() => {
      capturedSnapshotProps.onSnapshotData(TWO_CYCLE_RESPONSE);
    });
    expect(capturedSnapshotProps.expiration_cycle).toBe('M');

    const select = screen.getByLabelText('Cycle');
    act(() => {
      fireEvent.change(select, { target: { value: '' } });
    });
    expect(capturedSnapshotProps.expiration_cycle ?? null).toBe(null);
  });
});
