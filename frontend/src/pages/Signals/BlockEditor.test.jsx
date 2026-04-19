// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import BlockEditor from './BlockEditor';
import { emptyRules } from './storage';

// SeriesPicker pulls from /api/data/*; stub the network layer so its
// useEffect doesn't blow up in jsdom. We don't assert anything about it
// here — the Indicator and Constant tabs cover our CRUD flows.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => []),
  listInstruments: vi.fn(async () => ({ items: [], total: 0, skip: 0, limit: 0 })),
}));

function renderEditor(initialRules = emptyRules(), extra = {}) {
  const onRulesChange = vi.fn();
  const indicators = [
    { id: 'sma-20', name: '20-day SMA' },
    { id: 'rsi-14', name: '14-day RSI' },
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

describe('BlockEditor CRUD', () => {
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

  it('adds a block on long_entry — produces one block with one default condition', () => {
    const { onRulesChange } = renderEditor();
    fireEvent.click(screen.getByTestId('add-block-btn'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry).toHaveLength(1);
    expect(nextRules.long_entry[0].conditions).toHaveLength(1);
    expect(nextRules.long_entry[0].conditions[0].op).toBe('gt');
    // Other directions untouched.
    expect(nextRules.long_exit).toEqual([]);
    expect(nextRules.short_entry).toEqual([]);
    expect(nextRules.short_exit).toEqual([]);
  });

  it('adds a condition to an existing block', () => {
    const seeded = { ...emptyRules(),
      long_entry: [{ conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }] }] };
    const { onRulesChange } = renderEditor(seeded);
    fireEvent.click(screen.getByTestId('add-condition-0'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry[0].conditions).toHaveLength(2);
    expect(nextRules.long_entry[0].conditions[1].op).toBe('gt');
  });

  it('removes a block', () => {
    const seeded = { ...emptyRules(),
      long_entry: [
        { conditions: [{ op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } }] },
        { conditions: [{ op: 'lt', lhs: { kind: 'constant', value: 2 }, rhs: { kind: 'constant', value: 0 } }] },
      ] };
    const { onRulesChange } = renderEditor(seeded);
    fireEvent.click(screen.getByTestId('remove-block-0'));
    expect(onRulesChange).toHaveBeenCalledTimes(1);
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry).toHaveLength(1);
    // Second block survived.
    expect(nextRules.long_entry[0].conditions[0].op).toBe('lt');
  });

  it('removes a condition', () => {
    const seeded = { ...emptyRules(),
      long_entry: [
        { conditions: [
          { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
          { op: 'lt', lhs: { kind: 'constant', value: 2 }, rhs: { kind: 'constant', value: 0 } },
        ] },
      ] };
    const { onRulesChange } = renderEditor(seeded);
    fireEvent.click(screen.getByTestId('remove-condition-0-0'));
    const nextRules = onRulesChange.mock.calls[0][0];
    expect(nextRules.long_entry[0].conditions).toHaveLength(1);
    expect(nextRules.long_entry[0].conditions[0].op).toBe('lt');
  });

  it('switches directions and edits independently', () => {
    const { onRulesChange } = renderEditor();
    // Switch to short_entry and add a block.
    fireEvent.click(screen.getByTestId('direction-tab-short_entry'));
    fireEvent.click(screen.getByTestId('add-block-btn'));
    const next = onRulesChange.mock.calls[0][0];
    expect(next.short_entry).toHaveLength(1);
    expect(next.long_entry).toEqual([]);
  });

  it('changing op migrates the condition shape (binary → range)', () => {
    const seeded = { ...emptyRules(),
      long_entry: [{ conditions: [
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
    // operand (new) should be populated from old lhs — indicator ref preserved.
    expect(cond.operand).toEqual({ kind: 'indicator', indicator_id: 'sma-20', output: 'default' });
    expect(cond.min).toBeDefined();
    expect(cond.max).toBeDefined();
  });
});
