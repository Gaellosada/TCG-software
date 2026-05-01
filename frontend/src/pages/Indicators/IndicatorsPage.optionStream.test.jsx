// @vitest-environment jsdom
//
// Wave 3 — IndicatorsPage end-to-end wiring for option-stream-shaped
// SeriesRefs.
//
// Modelled on ``IndicatorsPage.assetCompat.test.jsx`` but stresses the
// option_stream code-path that Wave 2c introduces via the defaults
// hydrator. We mock ``defaultIndicators`` with a stub registry entry
// whose ``seriesMap`` already contains an ``option_stream`` ref; this
// avoids any dependency on the Wave 2c rename / hydrator landing first.
//
// Coverage:
//   1. Pre-flight blocks the request when the option_stream ref is
//      tautological (selection.kind=by_delta + stream='delta'). Picker
//      grey-with-tooltip behaviour is covered in the runGate unit suite;
//      here we verify the page-level outcome (no fetch fired).
//   2. A healthy run dispatches a JSON request body whose
//      ``series.<label>`` is the option_stream-shaped ref (type,
//      collection, selection.kind, stream).
//   3. A 422 with ``error_code: 'TAUTOLOGICAL_OPTION_STREAM'`` from the
//      backend renders the typed error card with heading
//      "Tautological selection".
//   4. A 422 with ``error_code: 'STREAM_UNAVAILABLE_FOR_ROOT'`` renders
//      the typed error card with heading "Stream unavailable for root".
//   5. A 200 success renders the chart (no error card visible).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react';

// Stub the Plotly chart so jsdom never tries to render WebGL.
vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub() { return <div data-testid="chart-stub" />; }
  return { default: ChartStub };
});

const computeIndicatorMock = vi.fn();
const resolveDefaultIndexInstrumentMock = vi.fn();
vi.mock('../../api/indicators', () => ({
  computeIndicator: (...args) => computeIndicatorMock(...args),
  resolveDefaultIndexInstrument: (...args) => resolveDefaultIndexInstrumentMock(...args),
}));

// runIndicator resolves a default ISO date range from /api/options/roots
// when the seriesMap contains an option_stream ref. Mock the API so jsdom
// doesn't try to fetch and the resolved range is deterministic.
const getOptionRootsMock = vi.fn();
vi.mock('../../api/options', () => ({
  getOptionRoots: (...args) => getOptionRootsMock(...args),
}));

// Stub DEFAULT_INDICATORS with a single option-only registry entry whose
// seriesMap is pre-seeded with an option_stream ref. This mirrors what
// Wave 2c's hydrator will produce once it lands.
vi.mock('./defaultIndicators', () => {
  // Two-label code is needed only if we want slot conflicts. For this
  // suite a single-label option indicator is enough.
  const FAKE_CODE = "def compute(series, target_days: int = 30):\n    s = series['close']\n    return s";
  return {
    DEFAULT_INDICATORS: [
      {
        id: 'atm-contract-iv',
        name: 'ATM contract IV',
        readonly: true,
        category: 'volatility',
        compatibleAssetTypes: ['option'],
        chartShape: 'time-series',
        code: FAKE_CODE,
        params: { target_days: 30 },
        seriesMap: {},
        doc: '',
        ownPanel: true,
      },
    ],
  };
});

import IndicatorsPage from './IndicatorsPage';

// Healthy option_stream ref — by_moneyness selection, iv stream.
const HEALTHY_STREAM_REF = {
  type: 'option_stream',
  collection: 'OPT_SP_500',
  option_type: 'C',
  cycle: null,
  maturity: { kind: 'nearest_to_target', target_days: 30 },
  selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
  stream: 'iv',
};

const TAUTOLOGICAL_STREAM_REF = {
  ...HEALTHY_STREAM_REF,
  selection: { kind: 'by_delta', target: 0.25 },
  stream: 'delta',
};

function seedDefaultState(defaultId, seriesMap, params = { target_days: 30 }) {
  const payload = {
    version: 1,
    indicators: [],
    defaultState: {
      [defaultId]: { params, seriesMap },
    },
  };
  localStorage.setItem('tcg.indicators.v1', JSON.stringify(payload));
}

afterEach(() => {
  cleanup();
  computeIndicatorMock.mockReset();
  resolveDefaultIndexInstrumentMock.mockReset();
  getOptionRootsMock.mockReset();
  try { localStorage.clear(); } catch { /* ignore */ }
});

beforeEach(() => {
  // No SPX default — option-stream tests never need it.
  resolveDefaultIndexInstrumentMock.mockResolvedValue({ ok: true, data: null });
  // Roots lookup yields a deterministic last_trade_date so the FE-derived
  // [start, end] range is reproducible across runs.
  getOptionRootsMock.mockResolvedValue({
    roots: [{ collection: 'OPT_SP_500', name: 'SP_500', has_greeks: true, last_trade_date: '2024-12-20' }],
  });
});

describe('option_stream — pre-flight gate', () => {
  it('blocks the request when the option_stream ref is tautological', async () => {
    seedDefaultState('atm-contract-iv', { close: TAUTOLOGICAL_STREAM_REF });
    render(<IndicatorsPage />);

    // The Run button is disabled because of the tautological combo. We
    // surface the tooltip via the title attribute (existing pattern).
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    expect(runBtn).toHaveProperty('disabled', true);
    expect(runBtn.getAttribute('title') || '').toMatch(/tautological/i);

    // Sanity: no fetch fired.
    expect(computeIndicatorMock).not.toHaveBeenCalled();
  });
});

describe('option_stream — healthy dispatch', () => {
  it('forwards the option_stream-shaped SeriesRef in the JSON body', async () => {
    seedDefaultState('atm-contract-iv', { close: HEALTHY_STREAM_REF });
    let resolveCompute;
    computeIndicatorMock.mockImplementation(() => new Promise((resolve) => {
      resolveCompute = resolve;
    }));

    render(<IndicatorsPage />);

    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    expect(runBtn).toHaveProperty('disabled', false);
    fireEvent.click(runBtn);

    await waitFor(() => expect(computeIndicatorMock).toHaveBeenCalled());
    const [body] = computeIndicatorMock.mock.calls[0];
    expect(body.series.close).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      stream: 'iv',
    });
    expect(body.series.close.selection.kind).toBe('by_moneyness');
    expect(body.series.close.maturity.kind).toBe('nearest_to_target');
    // Asset-type metadata still flows even though the slot is option_stream-shaped.
    expect(body.asset_type).toBe('option');
    expect(body.compatible_asset_types).toEqual(['option']);
    // ISO date range derived from the mocked root's last_trade_date
    // (6-month lookback — keeps the per-date materialiser fast enough
    // on remote Mongo for v1). Asserting the contract proves the
    // option_stream resolver gets concrete dates instead of None.
    expect(body.end).toBe('2024-12-20');
    expect(body.start).toBe('2024-06-20');

    await act(async () => {
      resolveCompute({
        dates: ['2024-01-01'],
        series: [{ label: 'close', collection: 'OPT_SP_500', instrument_id: 'stream', close: [0.18] }],
        indicator: [0.18],
        diagnostics: [null],
      });
    });
  });
});

describe('option_stream — typed-error rendering', () => {
  it('renders the typed error card on TAUTOLOGICAL_OPTION_STREAM (422)', async () => {
    // The pre-flight should normally catch this. Here we use a HEALTHY
    // ref but force the backend to reject with the typed error code, so
    // we can prove the typed-error envelope routes correctly. This is
    // the Sign-7 contract: route on error_code, never on the message.
    seedDefaultState('atm-contract-iv', { close: HEALTHY_STREAM_REF });
    const errorBody = {
      error_code: 'TAUTOLOGICAL_OPTION_STREAM',
      indicator_id: 'atm-contract-iv',
      asset_type: 'option',
      accepted_asset_types: ['option'],
      detail: "selection=by_delta with stream='delta' returns the target delta by construction.",
      message: "selection=by_delta with stream='delta' returns the target delta by construction.",
    };
    computeIndicatorMock.mockRejectedValue(
      Object.assign(new Error(errorBody.message), { status: 422, body: errorBody }),
    );

    render(<IndicatorsPage />);
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);

    await waitFor(() => expect(screen.getByText('Tautological selection')).toBeTruthy());
    // The typed error card carries the backend's detail message verbatim.
    expect(screen.getByText(/by_delta/i)).toBeTruthy();
    // No chart should have rendered.
    expect(screen.queryByTestId('chart-stub')).toBeNull();
  });

  it('renders the typed error card on STREAM_UNAVAILABLE_FOR_ROOT (422)', async () => {
    seedDefaultState('atm-contract-iv', { close: HEALTHY_STREAM_REF });
    const errorBody = {
      error_code: 'STREAM_UNAVAILABLE_FOR_ROOT',
      indicator_id: 'atm-contract-iv',
      root: 'SPY',
      unavailable_streams: ['gamma', 'vega', 'theta'],
      detail: 'Greek streams (gamma, vega, theta) are not available on this option root.',
      message: 'Greek streams (gamma, vega, theta) are not available on this option root.',
    };
    computeIndicatorMock.mockRejectedValue(
      Object.assign(new Error(errorBody.message), { status: 422, body: errorBody }),
    );

    render(<IndicatorsPage />);
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);

    await waitFor(() => expect(screen.getByText('Stream unavailable for root')).toBeTruthy());
    expect(screen.getByText(/Greek streams/i)).toBeTruthy();
    expect(screen.queryByTestId('chart-stub')).toBeNull();
  });

  it('renders the chart on a 200 success — no error card', async () => {
    seedDefaultState('atm-contract-iv', { close: HEALTHY_STREAM_REF });
    computeIndicatorMock.mockResolvedValue({
      dates: ['2024-01-01', '2024-01-02'],
      series: [
        { label: 'close', collection: 'OPT_SP_500', instrument_id: 'stream', close: [0.18, 0.19] },
      ],
      indicator: [0.18, 0.19],
      diagnostics: [null, null],
    });

    render(<IndicatorsPage />);
    const runBtn = await screen.findByRole('button', { name: /run indicator/i });
    fireEvent.click(runBtn);

    await waitFor(() => expect(screen.getByTestId('chart-stub')).toBeTruthy());
    expect(screen.queryByText('Tautological selection')).toBeNull();
    expect(screen.queryByText('Stream unavailable for root')).toBeNull();
  });
});
