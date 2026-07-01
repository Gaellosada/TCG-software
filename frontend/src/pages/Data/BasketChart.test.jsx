// @vitest-environment jsdom
//
// Tests for BasketChart — the Data-page basket-exploration view.
//   - Renders a single composite line trace on the SHARED Chart.
//   - Exposes start/end date pickers (D3).
//   - The "Show legs" toggle (D1) is offered only for a multi-leg inline
//     basket and, when on, adds one dotted trace per leg.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor, screen, fireEvent } from '@testing-library/react';

let capturedChartProps = null;
vi.mock('../../components/Chart', () => ({
  default: vi.fn((props) => {
    capturedChartProps = props;
    return <div data-testid="chart" />;
  }),
}));

// getBasketSeries is called by the composite hook AND by each per-leg query.
// Return a deterministic series keyed off the requested basket so the
// composite and per-leg traces are distinguishable.
const mockGetBasketSeries = vi.fn();
vi.mock('../../api/data', () => ({
  getBasketSeries: (...args) => mockGetBasketSeries(...args),
}));

import BasketChart from './BasketChart';

const DATES = [20240102, 20240103, 20240104];

beforeEach(() => {
  capturedChartProps = null;
  mockGetBasketSeries.mockImplementation((basket) => {
    // One leg → its own series; full basket → the composite.
    const oneLeg = basket.legs && basket.legs.length === 1;
    return Promise.resolve({
      dates: DATES,
      values: oneLeg ? [10, 11, 12] : [100, 101, 102],
    });
  });
});

afterEach(() => cleanup());

const SPY = {
  instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' },
  weight: 0.6,
};
const QQQ = {
  instrument: { type: 'spot', collection: 'ETF', instrument_id: 'QQQ' },
  weight: 0.4,
};

describe('BasketChart', () => {
  it('renders a single composite trace by default', async () => {
    render(
      <BasketChart
        basket={{ kind: 'inline', asset_class: 'equity', legs: [SPY, QQQ] }}
        name="My Basket"
        assetClass="equity"
        legs={[SPY, QQQ]}
      />,
    );
    await waitFor(() => expect(capturedChartProps).not.toBeNull());
    expect(capturedChartProps.traces).toHaveLength(1);
    expect(capturedChartProps.traces[0].name).toBe('My Basket');
    expect(capturedChartProps.traces[0].y).toEqual([100, 101, 102]);
  });

  it('exposes start and end date pickers (D3)', async () => {
    const { container } = render(
      <BasketChart
        basket={{ kind: 'saved', basket_id: 'b1' }}
        name="Saved"
        assetClass="equity"
      />,
    );
    await waitFor(() => expect(capturedChartProps).not.toBeNull());
    const dateInputs = container.querySelectorAll('input[type="date"]');
    expect(dateInputs).toHaveLength(2);
  });

  it('per-leg toggle adds one trace per leg when enabled (D1)', async () => {
    render(
      <BasketChart
        basket={{ kind: 'inline', asset_class: 'equity', legs: [SPY, QQQ] }}
        name="My Basket"
        assetClass="equity"
        legs={[SPY, QQQ]}
      />,
    );
    await waitFor(() => expect(capturedChartProps).not.toBeNull());
    expect(capturedChartProps.traces).toHaveLength(1);

    fireEvent.click(screen.getByLabelText('Show legs'));

    // composite + one trace per leg = 3
    await waitFor(() => {
      expect(capturedChartProps.traces.length).toBe(3);
    });
    const names = capturedChartProps.traces.map((t) => t.name);
    expect(names).toContain('My Basket');
    expect(names).toContain('SPY');
    expect(names).toContain('QQQ');
  });

  it('does NOT offer the leg toggle for a saved basket without inline legs', async () => {
    render(
      <BasketChart basket={{ kind: 'saved', basket_id: 'b1' }} name="Saved" assetClass="equity" />,
    );
    await waitFor(() => expect(capturedChartProps).not.toBeNull());
    expect(screen.queryByLabelText('Show legs')).toBeNull();
  });
});
