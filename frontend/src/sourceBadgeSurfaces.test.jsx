// @vitest-environment jsdom
//
// Requirement (a): an EXISTING portfolio leg / signal input / indicator series
// renders a READ-ONLY source badge with the correct source and NO interactive
// control to change it. The source is chosen once at add time and is immutable.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

// InstrumentPickerModal (mounted by InputsPanel + Indicators ParamsPanel) loads
// collections on open — stub the network so mounting (closed) never throws.
vi.mock('./api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX']),
  listInstruments: vi.fn(async () => ({ items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 500 })),
  getAvailableCycles: vi.fn(async () => []),
}));

import HoldingsList from './pages/Portfolio/HoldingsList';
import InputsPanel from './pages/Signals/InputsPanel';
import IndicatorParamsPanel from './pages/Indicators/ParamsPanel';

afterEach(() => { cleanup(); });

const V2_SPOT = { type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v2' };

describe('read-only source badge on each surface', () => {
  it('portfolio leg: shows a v2 badge and no source <select>', () => {
    const legs = [{ id: 'leg-1', label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100, dataSource: 'v2' }];
    const { container } = render(
      <HoldingsList
        legs={legs}
        legDateRanges={{}}
        onUpdateLeg={vi.fn()}
        onRemoveLeg={vi.fn()}
        onOpenAddModal={vi.fn()}
        onOpenSignalModal={vi.fn()}
      />,
    );
    const badge = screen.getByTestId('leg-datasource-leg-1');
    expect(badge.tagName).toBe('SPAN');
    expect(badge.textContent).toBe('v2');
    expect(container.querySelector('select')).toBeNull();
  });

  it('signal input: shows a v2 badge and no source <select>', () => {
    const inputs = [{ id: 'X', instrument: V2_SPOT }];
    const { container } = render(<InputsPanel inputs={inputs} onChange={vi.fn()} />);
    // A non-empty panel opens collapsed — expand it to reveal the input rows.
    fireEvent.click(screen.getByTestId('inputs-panel-toggle'));
    const badge = screen.getByTestId('input-datasource-0');
    expect(badge.tagName).toBe('SPAN');
    expect(badge.textContent).toBe('v2');
    // No <select> for the source (the picker modal is closed).
    expect(container.querySelector('select')).toBeNull();
  });

  it('indicator series: shows a v2 badge and no source <select>', () => {
    const { container } = render(
      <IndicatorParamsPanel
        indicator={{ id: 'u1', name: 'I', code: "def compute(series):\n return series['close']", params: {}, seriesMap: { close: V2_SPOT }, readonly: false }}
        paramsSpec={[]}
        seriesLabels={['close']}
        onParamChange={vi.fn()}
        onSeriesSave={vi.fn()}
        onRun={vi.fn()}
        running={false}
        canRun={false}
        runDisabledReason={null}
        ownPanel={false}
        onOwnPanelChange={vi.fn()}
      />,
    );
    const badge = screen.getByTestId('series-datasource-close');
    expect(badge.tagName).toBe('SPAN');
    expect(badge.textContent).toBe('v2');
    expect(container.querySelector('select')).toBeNull();
  });
});
