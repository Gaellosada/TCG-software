// @vitest-environment jsdom
//
// Tests for ContractDetailPanel.
//
// Mocks:
//   - ../../components/Chart : minimal stub that captures props (avoids Plotly).
//   - ./useContractSeries : drives contract data + loading/error deterministically.
//   - ../../api/options : the underlying fetcher (defensive — useContractSeries
//                         doesn't actually call it once the hook is mocked).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

// Captured Chart props per render.
const chartProps = [];

vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub({ traces, layoutOverrides, downloadFilename }) {
    chartProps.push({ traces, layoutOverrides, downloadFilename });
    return (
      <div
        data-testid="chart-stub"
        data-trace-count={Array.isArray(traces) ? traces.length : 0}
        data-download-filename={downloadFilename}
      />
    );
  }
  return { default: ChartStub };
});

const seriesState = {
  data: null,
  loading: false,
  error: null,
};

vi.mock('./useContractSeries', () => ({
  useContractSeries: () => seriesState,
}));

vi.mock('../../api/options', () => ({
  getOptionContract: vi.fn(),
  getOptionChain: vi.fn(),
}));

// Import AFTER vi.mock so the stub is wired.
import ContractDetailPanel from './ContractDetailPanel';

// ---------------------------------------------------------------------------
// Helpers — sample payload builders
// ---------------------------------------------------------------------------

function stored(value) {
  return { value, source: 'stored', model: null, inputs_used: null, missing_inputs: null, error_code: null, error_detail: null };
}
function computed(value) {
  return {
    value,
    source: 'computed',
    model: 'Black-76',
    inputs_used: { underlying_price: 5500, iv: 0.18, ttm: 0.1, r: 0 },
    missing_inputs: null,
    error_code: null,
    error_detail: null,
  };
}
function missingCR() {
  return {
    value: null,
    source: 'missing',
    model: null,
    inputs_used: null,
    missing_inputs: ['forward_vix_curve'],
    error_code: 'missing_forward_vix_curve',
    error_detail: 'Forward VIX curve unavailable.',
  };
}

function makeContract(overrides = {}) {
  return {
    collection: 'OPT_SP_500',
    // Default matches the `instrumentId` most tests pass — needed because
    // the panel now hides data whose contract_id doesn't equal the prop.
    contract_id: 'X|M',
    root_underlying: 'OPT_SP_500',
    underlying_ref: 'SPY',
    underlying_symbol: 'SPY',
    expiration: '2024-04-19',
    expiration_cycle: 'monthly',
    strike: 5000,
    type: 'C',
    contract_size: 100,
    currency: 'USD',
    provider: 'IVOLATILITY',
    strike_factor_verified: true,
    ...overrides,
  };
}

function makeRow(date, overrides = {}) {
  return {
    date,
    open: 510,
    high: 512,
    low: 509,
    close: 511,
    bid: 510.5,
    ask: 511.0,
    bid_size: 1,
    ask_size: 1,
    volume: 100,
    open_interest: 123,
    mid: 510.75,
    iv_stored: 0.18,
    delta_stored: 0.95,
    gamma_stored: 0.001,
    theta_stored: null,
    vega_stored: 2.0,
    underlying_price_stored: 5500,
    iv: stored(0.18),
    delta: stored(0.95),
    gamma: stored(0.001),
    theta: missingCR(),
    vega: stored(2.0),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Reset between tests.
// ---------------------------------------------------------------------------

beforeEach(() => {
  seriesState.data = null;
  seriesState.loading = false;
  seriesState.error = null;
  chartProps.length = 0;
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('<ContractDetailPanel> metadata sidebar', () => {
  it('populates strike, type, expiration, root, provider from contract', () => {
    seriesState.data = {
      contract: makeContract({
        contract_id: 'SPY_240419C00500000|M',
        strike: 5000,
        type: 'P',
        expiration: '2024-04-19',
      }),
      rows: [makeRow('2024-03-01'), makeRow('2024-03-15')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="SPY_240419C00500000|M"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText('5000.00')).toBeTruthy();
    expect(screen.getByText('Put')).toBeTruthy();
    expect(screen.getByText('2024-04-19')).toBeTruthy();
    expect(screen.getByText('OPT_SP_500')).toBeTruthy();
    expect(screen.getByText('IVOLATILITY')).toBeTruthy();
    expect(screen.getByText(/2024-03-01.*2024-03-15/)).toBeTruthy();
  });

  it('does not surface the strike-factor verification banner or badge', () => {
    // Verification UI was intentionally removed from the right panel; the
    // sidebar should not carry "Verified" / "Pending" labels and the
    // banner should not render even for unverified roots.
    seriesState.data = {
      contract: makeContract({
        root_underlying: 'OPT_T_NOTE_10_Y',
        strike_factor_verified: false,
      }),
      rows: [makeRow('2024-03-01')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_T_NOTE_10_Y"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText('Verified')).toBeNull();
    expect(screen.queryByText('Pending')).toBeNull();
    expect(screen.queryByText(/strike factor verification pending/i)).toBeNull();
  });
});

describe('<ContractDetailPanel> chart traces', () => {
  it('passes mid + volume traces to the Chart component by default', () => {
    seriesState.data = {
      contract: makeContract(),
      rows: [makeRow('2024-03-01'), makeRow('2024-03-02')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    expect(chartProps.length).toBeGreaterThan(0);
    const last = chartProps[chartProps.length - 1];
    expect(Array.isArray(last.traces)).toBe(true);
    // Mid + volume.
    expect(last.traces).toHaveLength(2);
    const names = last.traces.map((t) => t.name);
    expect(names).toContain('Mid');
    expect(names).toContain('Volume');
  });

  it('toggling Δ overlay adds a delta trace', () => {
    seriesState.data = {
      contract: makeContract(),
      rows: [makeRow('2024-03-01'), makeRow('2024-03-02')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    // Toggle the Δ overlay checkbox.
    const deltaToggle = screen.getByLabelText('Δ');
    fireEvent.click(deltaToggle);

    const last = chartProps[chartProps.length - 1];
    const names = last.traces.map((t) => t.name);
    expect(names).toContain('Δ');
  });

  it('computed Greek overlay uses dashed line style', () => {
    // Inject a row whose delta is COMPUTED — overlay trace should be dashed.
    seriesState.data = {
      contract: makeContract(),
      rows: [
        makeRow('2024-03-01', { delta: computed(0.5) }),
        makeRow('2024-03-02', { delta: computed(0.51) }),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText('Δ'));

    const last = chartProps[chartProps.length - 1];
    const deltaTrace = last.traces.find((t) => t.name === 'Δ');
    expect(deltaTrace).toBeTruthy();
    expect(deltaTrace.line.dash).toBe('dash');
  });

  it('all-stored Greek overlay uses solid line style', () => {
    seriesState.data = {
      contract: makeContract(),
      rows: [
        makeRow('2024-03-01', { delta: stored(0.5) }),
        makeRow('2024-03-02', { delta: stored(0.51) }),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText('Δ'));

    const last = chartProps[chartProps.length - 1];
    const deltaTrace = last.traces.find((t) => t.name === 'Δ');
    expect(deltaTrace.line.dash).toBe('solid');
  });
});

describe('<ContractDetailPanel> cycle metadata row', () => {
  it('renders Cycle row with full cycle string when expiration_cycle is populated', () => {
    seriesState.data = {
      contract: makeContract({ contract_id: 'X|M', expiration_cycle: 'W3 Friday' }),
      rows: [makeRow('2024-03-01'), makeRow('2024-03-15')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    expect(screen.getByText('Cycle')).toBeTruthy();
    expect(screen.getByText('W3 Friday')).toBeTruthy();
  });

  it('renders Cycle row as em-dash when expiration_cycle is empty', () => {
    seriesState.data = {
      contract: makeContract({ contract_id: 'X|M', expiration_cycle: '' }),
      rows: [makeRow('2024-03-01')],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    // Label still present.
    expect(screen.getByText('Cycle')).toBeTruthy();
    // Value is em-dash.
    const cycleLabel = screen.getByText('Cycle');
    const metaRow = cycleLabel.closest('[class*="metaRow"]');
    expect(metaRow).toBeTruthy();
    // The sibling value span should contain '—'.
    expect(metaRow.textContent).toContain('—');
  });
});

describe('<ContractDetailPanel> close button', () => {
  it('clicking Close calls onClose', () => {
    seriesState.data = {
      contract: makeContract(),
      rows: [makeRow('2024-03-01')],
    };
    const onClose = vi.fn();
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('<ContractDetailPanel> loading / error states', () => {
  it('shows a loading message while data is fetching', () => {
    seriesState.loading = true;
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/loading contract series/i)).toBeTruthy();
  });

  it('shows an error message on fetch failure', () => {
    seriesState.error = new Error('boom');
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/failed to load contract/i)).toBeTruthy();
    expect(screen.getByText(/boom/)).toBeTruthy();
  });

  it('hides stale data when instrumentId changed before the new fetch lands', () => {
    // Simulate the transient frame between clicking a new contract and the
    // hook's useEffect-based reset firing: data still belongs to the old
    // contract while instrumentId already points at the new one. The panel
    // must hide the body and surface the loading state so the user does
    // not see the previous chart while they're already on the new one.
    seriesState.data = {
      contract: makeContract({ contract_id: 'OLD|M' }),
      rows: [makeRow('2024-03-01')],
    };
    seriesState.loading = false;
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="NEW|M"
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/loading contract series/i)).toBeTruthy();
    expect(chartProps.length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Life-cycle markers
// ---------------------------------------------------------------------------

// Helper: rows for marker tests with underlying_price and delta data.
function makeRowWithUnderlying(date, underlyingPrice, deltaStored, overrides = {}) {
  return makeRow(date, {
    underlying_price_stored: underlyingPrice,
    delta_stored: deltaStored,
    delta: stored(deltaStored),
    ...overrides,
  });
}

// Count traces whose name matches a lifecycle marker label.
function markerTraceNames(traces) {
  const LIFECYCLE_LABELS = new Set([
    'First trade', 'Expiration', 'ATM cross', '|Δ|=0.30', '|Δ|=0.50', '|Δ|=0.70',
  ]);
  return traces.filter((t) => LIFECYCLE_LABELS.has(t.name)).map((t) => t.name);
}

describe('<ContractDetailPanel> life-cycle markers', () => {
  it('markers are off by default — no lifecycle traces in layout', () => {
    seriesState.data = {
      contract: makeContract({ expiration: '2024-04-19' }),
      rows: [
        makeRowWithUnderlying('2024-03-01', 5100, 0.30),
        makeRowWithUnderlying('2024-03-15', 5050, 0.50),
        makeRowWithUnderlying('2024-03-20', 5000, 0.70),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    const last = chartProps[chartProps.length - 1];
    const names = markerTraceNames(last.traces);
    // Default off — no lifecycle traces.
    expect(names).toHaveLength(0);
    // yaxis4 should not be in layoutOverrides.
    expect(last.layoutOverrides.yaxis4).toBeUndefined();
  });

  it('toggle on with full data → all 6 markers present', () => {
    seriesState.data = {
      contract: makeContract({ strike: 5000, expiration: '2024-04-19' }),
      rows: [
        makeRowWithUnderlying('2024-03-01', 5100, 0.30),
        makeRowWithUnderlying('2024-03-15', 5050, 0.50),
        makeRowWithUnderlying('2024-03-20', 5000, 0.70),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText('Life-cycle'));

    const last = chartProps[chartProps.length - 1];
    const names = markerTraceNames(last.traces);

    expect(names).toContain('First trade');
    expect(names).toContain('Expiration');
    expect(names).toContain('ATM cross');
    expect(names).toContain('|Δ|=0.30');
    expect(names).toContain('|Δ|=0.50');
    expect(names).toContain('|Δ|=0.70');
    // All 6 present.
    expect(names).toHaveLength(6);
    // yaxis4 (hidden overlay) is added to layout.
    expect(last.layoutOverrides.yaxis4).toBeTruthy();
  });

  it('toggle on with missing |Δ|=0.70 threshold → only 5 markers present', () => {
    // Delta never reaches 0.70 in data.
    seriesState.data = {
      contract: makeContract({ strike: 5000, expiration: '2024-04-19' }),
      rows: [
        makeRowWithUnderlying('2024-03-01', 5100, 0.30),
        makeRowWithUnderlying('2024-03-15', 5050, 0.50),
        // Max delta is 0.60 — never crosses 0.70.
        makeRowWithUnderlying('2024-03-20', 5000, 0.60),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText('Life-cycle'));

    const last = chartProps[chartProps.length - 1];
    const names = markerTraceNames(last.traces);

    expect(names).toContain('|Δ|=0.30');
    expect(names).toContain('|Δ|=0.50');
    expect(names).not.toContain('|Δ|=0.70');
    // 5 markers: firstTrade + expiration + atmCross + delta30 + delta50.
    expect(names).toHaveLength(5);
  });

  it('ATM marker only renders when underlying_price_stored exists on at least one row', () => {
    // Rows have no underlying_price_stored.
    seriesState.data = {
      contract: makeContract({ strike: 5000, expiration: '2024-04-19' }),
      rows: [
        makeRow('2024-03-01', { underlying_price_stored: null, delta_stored: 0.30 }),
        makeRow('2024-03-15', { underlying_price_stored: null, delta_stored: 0.50 }),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText('Life-cycle'));

    const last = chartProps[chartProps.length - 1];
    const names = markerTraceNames(last.traces);

    // ATM cross requires underlying_price_stored — absent → no ATM marker.
    expect(names).not.toContain('ATM cross');
    // But first trade and expiration should still be present.
    expect(names).toContain('First trade');
    expect(names).toContain('Expiration');
  });

  it('first trade marker uses rows[0].date', () => {
    seriesState.data = {
      contract: makeContract({ strike: 5000, expiration: '2024-04-19' }),
      rows: [
        makeRowWithUnderlying('2024-02-10', 5500, 0.10),
        makeRowWithUnderlying('2024-03-01', 5100, 0.35),
      ],
    };
    render(
      <ContractDetailPanel
        collection="OPT_SP_500"
        instrumentId="X|M"
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText('Life-cycle'));

    const last = chartProps[chartProps.length - 1];
    const firstTradeTrace = last.traces.find((t) => t.name === 'First trade');
    expect(firstTradeTrace).toBeTruthy();
    // The trace x array contains the date (null separators between pairs).
    expect(firstTradeTrace.x).toContain('2024-02-10');
  });
});
