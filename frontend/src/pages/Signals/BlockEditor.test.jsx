// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import BlockEditor from './BlockEditor';
import { emptyRules } from './storage';

// InstrumentPickerModal pulls from /api/data/*; stub the network layer so its
// useEffect doesn't blow up in jsdom.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX']),
  listInstruments: vi.fn(async () => ({ items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 0 })),
  getAvailableCycles: vi.fn(async () => []),
}));

const SPX_INPUT = {
  id: 'X',
  instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
};

function renderEditor(initialRules = emptyRules(), extra = {}) {
  const onRulesChange = vi.fn();
  const indicators = [
    { id: 'sma-20', name: '20-day SMA', params: {}, seriesMap: {}, code: 'def compute(series):\n    return series["price"]' },
    { id: 'rsi-14', name: '14-day RSI', params: {}, seriesMap: {}, code: 'def compute(series):\n    return series["price"]' },
  ];
  const inputs = [SPX_INPUT];
  const utils = render(
    <BlockEditor
      rules={initialRules}
      onRulesChange={onRulesChange}
      inputs={inputs}
      indicators={indicators}
      {...extra}
    />,
  );
  return { ...utils, onRulesChange };
}

describe('BlockEditor (v3 / iter-4)', () => {
  it('renders all four direction tabs', () => {
    renderEditor();
    expect(screen.getByTestId('direction-tab-long_entry')).toBeDefined();
    expect(screen.getByTestId('direction-tab-long_exit')).toBeDefined();
    expect(screen.getByTestId('direction-tab-short_entry')).toBeDefined();
    expect(screen.getByTestId('direction-tab-short_exit')).toBeDefined();
  });

  it('starts on long_entry with zero blocks and an add-block button', () => {
    renderEditor();
    expect(screen.getByTestId('add-block-btn')).toBeDefined();
    expect(screen.queryByTestId('block-0')).toBeNull();
  });

  it('adds a block with no defaults — empty input_id, weight 0, no conditions', () => {
    const { onRulesChange } = renderEditor();
    fireEvent.click(screen.getByTestId('add-block-btn'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry).toHaveLength(1);
    const b = nextRules.long_entry[0];
    expect(b.input_id).toBe('');
    expect(b.weight).toBe(0);
    expect(b.conditions).toEqual([]);
    expect(nextRules.long_exit).toEqual([]);
  });

  it('adds a condition to an existing block', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{ input_id: 'X', weight: 0.5, conditions: [] }],
    };
    const { onRulesChange } = renderEditor(seeded);
    fireEvent.click(screen.getByTestId('add-condition-0'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry[0].conditions).toHaveLength(1);
    expect(nextRules.long_entry[0].conditions[0].op).toBe('gt');
  });

  it('hides weight input on exit tabs', () => {
    const seeded = {
      ...emptyRules(),
      long_exit: [{ input_id: 'X', weight: 0, conditions: [] }],
    };
    renderEditor(seeded);
    fireEvent.click(screen.getByTestId('direction-tab-long_exit'));
    expect(screen.queryByTestId('block-weight-0')).toBeNull();
  });

  it('shows weight input on entry tabs', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{ input_id: 'X', weight: 0, conditions: [] }],
    };
    renderEditor(seeded);
    expect(screen.getByTestId('block-weight-0')).toBeDefined();
  });

  it('block header shows an input-id dropdown (no inline picker)', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{ input_id: 'X', weight: 0.5, conditions: [] }],
    };
    renderEditor(seeded);
    // v3: a <select> bound to the signal's declared inputs.
    const select = screen.getByTestId('block-input-select-0');
    expect(select).toBeDefined();
    expect(select.value).toBe('X');
  });

  it('renders empty operand slot as a + button', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    renderEditor(seeded);
    expect(screen.getAllByTestId('operand-add-btn')).toHaveLength(2);
  });

  it('clicking + opens a menu with three kinds', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    renderEditor(seeded);
    const addBtns = screen.getAllByTestId('operand-add-btn');
    act(() => { fireEvent.click(addBtns[0]); });
    expect(screen.getByTestId('operand-menu')).toBeDefined();
    expect(screen.getByTestId('operand-menu-indicator')).toBeDefined();
    expect(screen.getByTestId('operand-menu-instrument')).toBeDefined();
    expect(screen.getByTestId('operand-menu-constant')).toBeDefined();
  });

  it('picking Constant from the menu installs a constant operand', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(seeded);
    const addBtns = screen.getAllByTestId('operand-add-btn');
    act(() => { fireEvent.click(addBtns[0]); });
    act(() => { fireEvent.click(screen.getByTestId('operand-menu-constant')); });
    const next = onRulesChange.mock.calls.pop()[0];
    expect(next.long_entry[0].conditions[0].lhs).toEqual({ kind: 'constant', value: 0 });
  });

  it('picking Indicator installs a v3 default indicator operand with empty input_id and null overrides', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(seeded);
    const addBtns = screen.getAllByTestId('operand-add-btn');
    act(() => { fireEvent.click(addBtns[0]); });
    act(() => { fireEvent.click(screen.getByTestId('operand-menu-indicator')); });
    const next = onRulesChange.mock.calls.pop()[0];
    const op = next.long_entry[0].conditions[0].lhs;
    expect(op.kind).toBe('indicator');
    expect(op.input_id).toBe('');
    expect(op.params_override).toBeNull();
    expect(op.series_override).toBeNull();
  });

  it('picking Instrument installs a v3 instrument operand with empty input_id', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(seeded);
    const addBtns = screen.getAllByTestId('operand-add-btn');
    act(() => { fireEvent.click(addBtns[0]); });
    act(() => { fireEvent.click(screen.getByTestId('operand-menu-instrument')); });
    const next = onRulesChange.mock.calls.pop()[0];
    const op = next.long_entry[0].conditions[0].lhs;
    expect(op).toEqual({ kind: 'instrument', input_id: '', field: 'close' });
  });

  it('clicking × on a filled operand opens the ConfirmDialog', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        input_id: 'X', weight: 0.5,
        conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 5 }, rhs: null }],
      }],
    };
    renderEditor(seeded);
    act(() => { fireEvent.click(screen.getAllByTestId('operand-clear-btn')[0]); });
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
  });

  it('block status dot reflects runnable state (v3: resolves via inputs)', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [
        { input_id: '', weight: 0, conditions: [] },
        {
          input_id: 'X',
          weight: 0.5,
          conditions: [{
            op: 'gt',
            lhs: { kind: 'constant', value: 1 },
            rhs: { kind: 'constant', value: 0 },
          }],
        },
      ],
    };
    renderEditor(seeded);
    expect(screen.getByTestId('block-status-0').getAttribute('data-runnable')).toBe('false');
    expect(screen.getByTestId('block-status-1').getAttribute('data-runnable')).toBe('true');
  });
});
