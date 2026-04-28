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
vi.mock('./OptionChainTable', () => ({
  default: vi.fn(({ root, onRowClick }) => {
    capturedOnRowClick = onRowClick;
    return <div data-testid="option-chain-table" data-root={root} />;
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
// Mock C3.1 / C3.2 deliverables — ChainSnapshotPanel and
// MultiExpirationSmilePanel. Capture onClose so tests can invoke it.
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
      />
    );
  }),
}));

let capturedMultiProps = null;
vi.mock('./MultiExpirationSmilePanel', () => ({
  default: vi.fn((props) => {
    capturedMultiProps = props;
    return (
      <div
        data-testid="multi-expiration-smile-panel"
        data-root={props.root}
        data-date={props.date}
        data-type={props.type}
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
  capturedMultiProps = null;
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

function selectOption(collection = 'OPT_SP_500') {
  act(() => {
    capturedOnSelect({
      type: 'option',
      collection,
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
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
    const chainTab = screen.getByRole('tab', { name: 'Chain' });
    expect(chainTab.getAttribute('aria-selected')).toBe('true');

    // OptionChainTable present
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    // Tier-2 panels absent
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
    expect(screen.queryByTestId('multi-expiration-smile-panel')).toBeNull();
  });

  it('shows all three tab buttons', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    expect(screen.getByRole('tab', { name: 'Chain' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Smile' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Multi-smile' })).toBeTruthy();
  });
});

describe('DataPage — switching to Smile tab', () => {
  it('renders ChainSnapshotPanel and hides OptionChainTable', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    expect(screen.getByTestId('chain-snapshot-panel')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
    expect(screen.queryByTestId('multi-expiration-smile-panel')).toBeNull();
  });

  it('passes root from selected.collection to ChainSnapshotPanel', () => {
    renderDataPage();
    selectOption('OPT_GOLD');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

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
    expect(screen.getByRole('tab', { name: 'Chain' }).getAttribute('aria-selected')).toBe('false');
  });
});

describe('DataPage — switching to Multi-smile tab', () => {
  it('renders MultiExpirationSmilePanel and hides OptionChainTable', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    expect(screen.getByTestId('multi-expiration-smile-panel')).toBeTruthy();
    expect(screen.queryByTestId('option-chain-table')).toBeNull();
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
  });

  it('passes root from selected.collection to MultiExpirationSmilePanel', () => {
    renderDataPage();
    selectOption('OPT_NASDAQ_100');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    const panel = screen.getByTestId('multi-expiration-smile-panel');
    expect(panel.dataset.root).toBe('OPT_NASDAQ_100');
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

    // Switch back to Chain
    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Chain' }));
    });
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
  });

  it('restores OptionChainTable after switching from Multi-smile back to Chain', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });
    expect(screen.queryByTestId('option-chain-table')).toBeNull();

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Chain' }));
    });
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.queryByTestId('multi-expiration-smile-panel')).toBeNull();
  });
});

describe('DataPage — ChainSnapshotPanel close button returns to Chain tab', () => {
  it('invokes onClose prop which returns to chain tab', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    expect(screen.getByTestId('chain-snapshot-panel')).toBeTruthy();

    // Invoke the onClose prop that DataPage passed to ChainSnapshotPanel
    act(() => {
      capturedSnapshotProps.onClose();
    });

    expect(screen.queryByTestId('chain-snapshot-panel')).toBeNull();
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Chain' }).getAttribute('aria-selected')).toBe('true');
  });
});

describe('DataPage — Snapshot tab filter inputs', () => {
  it('date input passes through to ChainSnapshotPanel via props', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    const panel = screen.getByTestId('chain-snapshot-panel');
    const initialDate = panel.dataset.date;

    // Find the date input in the filter strip and change it
    const dateInputs = screen.getAllByDisplayValue(initialDate);
    // The filter strip date input and the panel prop should share the same date
    expect(dateInputs.length).toBeGreaterThan(0);

    const newDate = '2024-06-15';
    act(() => {
      fireEvent.change(dateInputs[0], { target: { value: newDate } });
    });

    // After state update, ChainSnapshotPanel should receive new date prop
    expect(capturedSnapshotProps.date).toBe(newDate);
  });

  it('expiration text input updates the expiration prop on ChainSnapshotPanel', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Smile' }));
    });

    const expirationInput = screen.getByPlaceholderText('e.g. 2024-12-20');
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });

    expect(capturedSnapshotProps.expiration).toBe('2024-12-20');
  });
});

describe('DataPage — Multi-smile expiration management', () => {
  it('adds an expiration when Add button is clicked', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    const expirationInput = screen.getByPlaceholderText('e.g. 2024-12-20');
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    });

    expect(capturedMultiProps.expirations).toContain('2024-12-20');
  });

  it('does not add duplicate expirations', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    const expirationInput = screen.getByPlaceholderText('e.g. 2024-12-20');

    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    });
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    });

    expect(capturedMultiProps.expirations.filter((e) => e === '2024-12-20').length).toBe(1);
  });

  it('removes an expiration when × button is clicked', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    const expirationInput = screen.getByPlaceholderText('e.g. 2024-12-20');
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    });

    // Remove via × button
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove 2024-12-20' }));
    });

    expect(capturedMultiProps.expirations).not.toContain('2024-12-20');
  });

  it('resets expirations when switching to a new options root', () => {
    renderDataPage();
    selectOption('OPT_SP_500');

    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: 'Multi-smile' }));
    });

    const expirationInput = screen.getByPlaceholderText('e.g. 2024-12-20');
    act(() => {
      fireEvent.change(expirationInput, { target: { value: '2024-12-20' } });
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    });

    // Switch root
    act(() => {
      capturedOnSelect({
        type: 'option',
        collection: 'OPT_GOLD',
        instrument_id: null,
        expiry: null,
        strike: null,
        optionType: null,
      });
    });

    // Should be back on chain tab (root reset)
    expect(screen.getByRole('tab', { name: 'Chain' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('option-chain-table')).toBeTruthy();
  });
});
