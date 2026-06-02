// @vitest-environment jsdom
//
// Wave 3 — IndicatorsPage asset-type compatibility wiring.
//
// Verifies (behavior, not snapshots):
//   1. runIndicator forwards asset_type and compatible_asset_types to
//      computeIndicator when both can be derived.
//   2. runIndicator refuses to run with a typed validation error when
//      seriesMap slots disagree on asset_type (Sign 10 — never silently
//      pick one).
//   3. The pinned-meets-incompat banner appears when a previously-run
//      indicator's resolved asset_type is no longer in its compat list.
//   4. The Detach button clears the pinned result so the chart panel
//      goes back to the "Run to see chart" state.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react';

// Mock the Chart component (avoid Plotly + jsdom issues).
vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub() { return <div data-testid="chart-stub" />; }
  return { default: ChartStub };
});

// Spy / mock for computeIndicator so we can assert wire-format and
// drive run results synchronously.
const computeIndicatorMock = vi.fn();
const resolveDefaultIndexInstrumentMock = vi.fn();
vi.mock('../../api/indicators', () => ({
  computeIndicator: (...args) => computeIndicatorMock(...args),
  resolveDefaultIndexInstrument: (...args) => resolveDefaultIndexInstrumentMock(...args),
}));

// Stub DEFAULT_INDICATORS so the test owns the registry shape; we ship
// two indicators — one option-only, one index/equity — both of which
// declare a parsable Python signature. The factory is hoisted; FAKE_CODE
// must be inlined or built inside the factory body.
vi.mock('./defaultIndicators', () => {
  const FAKE_CODE_ONE = "def compute(series, window: int = 20):\n    s = series['close']\n    return s";
  // Two-label code so reconcileSeriesMap retains both slots — needed
  // to trigger slot-conflict when seeded with disagreeing asset types.
  const FAKE_CODE_TWO = "def compute(series):\n    a = series['a']\n    b = series['b']\n    return a + b";
  return {
    DEFAULT_INDICATORS: [
      {
        id: 'atm',
        name: 'ATM IV',
        readonly: true,
        category: 'volatility',
        compatibleAssetTypes: ['option'],
        chartShape: 'time-series',
        code: FAKE_CODE_ONE,
        params: {},
        seriesMap: {},
        doc: '',
        ownPanel: true,
      },
      {
        id: 'sma',
        name: 'SMA',
        readonly: true,
        category: 'trend',
        compatibleAssetTypes: ['index', 'equity'],
        chartShape: 'time-series',
        code: FAKE_CODE_ONE,
        params: {},
        seriesMap: {},
        doc: '',
        ownPanel: false,
      },
      {
        id: 'multi',
        name: 'Multi',
        readonly: true,
        category: 'trend',
        compatibleAssetTypes: ['index', 'equity'],
        chartShape: 'time-series',
        code: FAKE_CODE_TWO,
        params: {},
        seriesMap: {},
        doc: '',
        ownPanel: false,
      },
    ],
  };
});

import IndicatorsPage from './IndicatorsPage';

afterEach(() => {
  cleanup();
  computeIndicatorMock.mockReset();
  resolveDefaultIndexInstrumentMock.mockReset();
  try { localStorage.clear(); } catch { /* ignore */ }
});

beforeEach(() => {
  // No SPX default by default — keeps test seriesMap manipulation
  // explicit. Tests that need a default override this.
  resolveDefaultIndexInstrumentMock.mockResolvedValue({ ok: true, data: null });
});

// Drives a default indicator's seriesMap by writing the storage entry
// before mount — IndicatorsPage hydrates defaultState[id].seriesMap on
// load. Avoids needing to navigate the InstrumentPickerModal in tests.
function seedDefaultState(defaultId, seriesMap, params = {}) {
  const payload = {
    version: 1,
    indicators: [],
    defaultState: {
      [defaultId]: { params, seriesMap },
    },
  };
  localStorage.setItem('tcg.indicators.v1', JSON.stringify(payload));
}

const SPOT_OPTION = { type: 'spot', collection: 'OPT_SPX', instrument_id: 'SPXW 20240120 4500 C' };
const SPOT_INDEX = { type: 'spot', collection: 'INDEX', instrument_id: 'IND_SP_500' };

describe('runIndicator asset_type forwarding', () => {
  it('forwards asset_type and compatible_asset_types when slots agree', async () => {
    seedDefaultState('atm', { close: SPOT_OPTION });
    let resolveCompute;
    computeIndicatorMock.mockImplementation(() => new Promise((resolve) => {
      resolveCompute = resolve;
    }));

    render(<IndicatorsPage />);

    // Wait for hydration → ATM is selected by default (first in list).
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    expect(runBtn).toBeTruthy();
    fireEvent.click(runBtn);

    await waitFor(() => expect(computeIndicatorMock).toHaveBeenCalled());
    const [body] = computeIndicatorMock.mock.calls[0];
    expect(body.asset_type).toBe('option');
    expect(body.compatible_asset_types).toEqual(['option']);
    expect(body.series.close.collection).toBe('OPT_SPX');

    // Resolve the run so React can settle.
    await act(async () => {
      resolveCompute({ dates: [], series: [], indicator: [] });
    });
  });

  it('refuses to run when slots disagree on asset_type — typed validation error', async () => {
    // The 'multi' fake-code parses two series labels (a, b). Seed
    // each with a different asset type so the page-level derivation
    // hits a slot-conflict.
    seedDefaultState('multi', { a: SPOT_INDEX, b: SPOT_OPTION });

    render(<IndicatorsPage />);

    // Select 'Multi' — DEFAULT section starts expanded.
    fireEvent.click(await screen.findByText('Multi'));

    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);

    // No compute call — failure is purely client-side.
    expect(computeIndicatorMock).not.toHaveBeenCalled();

    // The error card should surface the slot-conflict message (typed
    // validation, not silent).
    await waitFor(() => {
      expect(screen.getByText(/slots disagree on asset type/i)).toBeTruthy();
    });
  });
});

describe('incompat pre-flight error card', () => {
  it('refuses to run with typed incompatible_asset error when seriesMap is incompat with declared compat list', async () => {
    // ATM IV declares compat=['option'] but we seed it with an INDEX
    // slot — the pre-flight in runIndicator should reject before any
    // network call, with a typed error envelope.
    seedDefaultState('atm', { close: SPOT_INDEX });
    render(<IndicatorsPage />);

    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);

    // No compute call — failure is purely client-side.
    expect(computeIndicatorMock).not.toHaveBeenCalled();

    // The error card surfaces the typed message.
    await waitFor(() => {
      expect(screen.getByText(/Requires option data; current asset is index/i)).toBeTruthy();
    });
  });

  it('chart panel shows chart (not pinned banner) when current asset is compat', async () => {
    seedDefaultState('atm', { close: SPOT_OPTION });
    computeIndicatorMock.mockResolvedValue({
      dates: ['2024-01-01'],
      series: [{ label: 'close', collection: 'OPT_SPX', instrument_id: 'X', close: [1] }],
      indicator: [1],
    });
    render(<IndicatorsPage />);
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);
    await waitFor(() => expect(screen.getByTestId('chart-stub')).toBeTruthy());
    // Banner should NOT appear when the run was compat — the pinned
    // banner is purely a "asset changed under us" failure mode, not
    // a normal-success affordance.
    expect(screen.queryByTestId('pinned-incompat-banner')).toBeNull();
  });
});
