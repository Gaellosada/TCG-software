// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import BlockEditor from './BlockEditor';
import { emptyRules } from './storage';

// SeriesPicker pulls from /api/data/*; stub the network layer so its
// useEffect doesn't blow up in jsdom.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => []),
  listInstruments: vi.fn(async () => ({ items: [], total: 0, skip: 0, limit: 0 })),
}));

function renderEditor(initialRules = emptyRules(), extra = {}) {
  const onRulesChange = vi.fn();
  const indicators = [
    { id: 'sma-20', name: '20-day SMA', params: {}, seriesMap: {}, code: 'def compute(series):\n    return series["price"]' },
    { id: 'rsi-14', name: '14-day RSI', params: {}, seriesMap: {}, code: 'def compute(series):\n    return series["price"]' },
  ];
  const utils = render(
    <BlockEditor
      rules={initialRules}
      onRulesChange={onRulesChange}
      indicators={indicators}
      {...extra}
    />,
  );
  return { ...utils, onRulesChange };
}

describe('BlockEditor (iter-3)', () => {
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

  it('adds a block with no defaults — empty instrument, weight 0, no conditions', () => {
    const { onRulesChange } = renderEditor();
    fireEvent.click(screen.getByTestId('add-block-btn'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry).toHaveLength(1);
    const b = nextRules.long_entry[0];
    expect(b.instrument).toBeNull();
    expect(b.weight).toBe(0);
    expect(b.conditions).toEqual([]);
    expect(nextRules.long_exit).toEqual([]);
  });

  it('adds a condition to an existing block', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{ instrument: null, weight: 0, conditions: [] }],
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
      long_exit: [{ instrument: null, weight: 0, conditions: [] }],
    };
    renderEditor(seeded);
    fireEvent.click(screen.getByTestId('direction-tab-long_exit'));
    // Weight input should NOT render for exit blocks.
    expect(screen.queryByTestId('block-weight-0')).toBeNull();
  });

  it('shows weight input on entry tabs', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{ instrument: null, weight: 0, conditions: [] }],
    };
    renderEditor(seeded);
    expect(screen.getByTestId('block-weight-0')).toBeDefined();
  });

  it('renders empty operand slot as a + button', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    renderEditor(seeded);
    // Each condition has two empty operand slots => two + buttons
    expect(screen.getAllByTestId('operand-add-btn')).toHaveLength(2);
  });

  it('clicking + opens a menu with three kinds', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
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
        instrument: null, weight: 0,
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

  it('picking Indicator installs a default indicator operand with null overrides', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
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
    expect(op.params_override).toBeNull();
    expect(op.series_override).toBeNull();
  });

  it('clicking × on a filled operand opens the ConfirmDialog', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
        conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 5 }, rhs: null }],
      }],
    };
    renderEditor(seeded);
    act(() => { fireEvent.click(screen.getAllByTestId('operand-clear-btn')[0]); });
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
  });

  it('confirming clear resets the operand to null', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
        conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 5 }, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(seeded);
    act(() => { fireEvent.click(screen.getAllByTestId('operand-clear-btn')[0]); });
    act(() => { fireEvent.click(screen.getByTestId('confirm-dialog-confirm')); });
    const next = onRulesChange.mock.calls.pop()[0];
    expect(next.long_entry[0].conditions[0].lhs).toBeNull();
  });

  it('block delete shows a confirmation dialog; confirming removes the block', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [
        { instrument: null, weight: 0, conditions: [] },
        { instrument: null, weight: 0, conditions: [] },
      ],
    };
    const { onRulesChange } = renderEditor(seeded);
    act(() => { fireEvent.click(screen.getByTestId('remove-block-0')); });
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    act(() => { fireEvent.click(screen.getByTestId('confirm-dialog-confirm')); });
    const next = onRulesChange.mock.calls.pop()[0];
    expect(next.long_entry).toHaveLength(1);
  });

  it('condition delete uses a confirmation dialog', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [{
        instrument: null, weight: 0,
        conditions: [{ op: 'gt', lhs: null, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(seeded);
    act(() => { fireEvent.click(screen.getByTestId('remove-condition-0-0')); });
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    act(() => { fireEvent.click(screen.getByTestId('confirm-dialog-confirm')); });
    const next = onRulesChange.mock.calls.pop()[0];
    expect(next.long_entry[0].conditions).toHaveLength(0);
  });

  it('changing op migrates the condition shape (binary → range)', () => {
    const seeded = { ...emptyRules(),
      long_entry: [{ instrument: null, weight: 0, conditions: [
        { op: 'gt',
          lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
          rhs: { kind: 'constant', value: 0 } },
      ] }] };
    const { onRulesChange } = renderEditor(seeded);
    const opSelect = screen.getByTestId('op-select-0-0');
    act(() => {
      fireEvent.change(opSelect, { target: { value: 'in_range' } });
    });
    const next = onRulesChange.mock.calls[0][0];
    const cond = next.long_entry[0].conditions[0];
    expect(cond.op).toBe('in_range');
    expect(cond.operand).toEqual({ kind: 'indicator', indicator_id: 'sma-20', output: 'default' });
    expect(cond.min).toBeDefined();
    expect(cond.max).toBeDefined();
  });

  it('block status dot reflects runnable state', () => {
    const seeded = {
      ...emptyRules(),
      long_entry: [
        { instrument: null, weight: 0, conditions: [] },
        {
          instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
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
