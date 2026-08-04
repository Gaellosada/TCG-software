// @vitest-environment jsdom
//
// Phase-2 stateful primitives — frontend coverage:
//   * conditionOps: hysteresis shape/slots/default/migrate + consecutive labels;
//   * requestBuilder: hysteresis enter/exit operand serialization + the
//     consecutive_days passthrough;
//   * BlockEditor: reachability + readable labels for both primitives.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });

import {
  ALL_OPS,
  HYSTERESIS_OPS,
  HYSTERESIS_DIRECTION_LABELS,
  OP_LABELS,
  conditionShape,
  operandSlots,
  defaultCondition,
  migrateCondition,
} from './conditionOps';
import { isConditionComplete } from './blockShape';
import { normaliseSpecForRequest } from './requestBuilder';
import BlockEditor from './BlockEditor';
import { emptyRules } from './storage';

// --------------------------------------------------------------------------- //
// conditionOps — hysteresis
// --------------------------------------------------------------------------- //

describe('conditionOps — hysteresis primitive', () => {
  it('lists hysteresis as an authorable op with a readable label', () => {
    expect(ALL_OPS).toContain('hysteresis');
    expect(HYSTERESIS_OPS).toEqual(['hysteresis']);
    expect(OP_LABELS.hysteresis).toBe('completes episode');
  });

  it('maps to its own shape + operand slots', () => {
    expect(conditionShape('hysteresis')).toBe('hysteresis');
    expect(operandSlots('hysteresis')).toEqual(['operand', 'enter', 'exit']);
  });

  it('default seeds null operands + up direction (no fabricated operands)', () => {
    expect(defaultCondition('hysteresis')).toEqual({
      op: 'hysteresis', operand: null, enter: null, exit: null, direction: 'up',
    });
  });

  it('direction labels read naturally per direction', () => {
    expect(HYSTERESIS_DIRECTION_LABELS.up).toBe('then descends to');
    expect(HYSTERESIS_DIRECTION_LABELS.down).toBe('then rises to');
  });

  it('migrate binary -> hysteresis carries the moving series into operand', () => {
    const current = { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: null };
    const next = migrateCondition(current, 'hysteresis');
    expect(next.op).toBe('hysteresis');
    expect(next.operand).toEqual(current.lhs);
    expect(next.enter).toBeNull();
    expect(next.exit).toBeNull();
    expect(next.direction).toBe('up');
  });

  it('isConditionComplete requires operand + enter + exit resolved', () => {
    const byId = { X: { id: 'X' } };
    const partial = { op: 'hysteresis', operand: { kind: 'instrument', input_id: 'X', field: 'close' }, enter: null, exit: null, direction: 'up' };
    expect(isConditionComplete(partial, byId)).toBe(false);
    const complete = {
      op: 'hysteresis',
      operand: { kind: 'instrument', input_id: 'X', field: 'close' },
      enter: { kind: 'constant', value: 95 },
      exit: { kind: 'constant', value: 75 },
      direction: 'up',
    };
    expect(isConditionComplete(complete, byId)).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// requestBuilder — serialization
// --------------------------------------------------------------------------- //

describe('requestBuilder — Phase-2 fields on the wire', () => {
  it('normalises hysteresis enter/exit operands and passes direction through', () => {
    const signal = {
      id: 's', name: 's',
      inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'I', instrument_id: 'X' } }],
      rules: {
        entries: [{
          id: 'e1', name: 'ep', input_id: 'X', weight: 100, enabled: true, conditions: [{
            op: 'hysteresis',
            operand: { kind: 'instrument', input_id: 'X', field: 'close' },
            enter: { kind: 'indicator', indicator_id: 'dstat95', input_id: 'X', output: 'default' },
            exit: { kind: 'constant', value: 75 },
            direction: 'up',
          }],
        }],
        exits: [], resets: [],
      },
    };
    const out = normaliseSpecForRequest(signal);
    const cond = out.rules.entries[0].conditions[0];
    expect(cond.op).toBe('hysteresis');
    expect(cond.direction).toBe('up');
    // indicator operand gets the deterministic override keys added:
    expect(cond.enter.params_override).toBeNull();
    expect(cond.enter.series_override).toBeNull();
    expect(cond.exit).toEqual({ kind: 'constant', value: 75 });
  });

  it('passes consecutive_days through verbatim on a comparator', () => {
    const signal = {
      id: 's', name: 's',
      inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'I', instrument_id: 'X' } }],
      rules: {
        entries: [{
          id: 'e1', name: 'c', input_id: 'X', weight: 100, enabled: true, conditions: [{
            op: 'lt',
            lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
            rhs: { kind: 'constant', value: 3.5 },
            consecutive_days: 2,
          }],
        }],
        exits: [], resets: [],
      },
    };
    const out = normaliseSpecForRequest(signal);
    expect(out.rules.entries[0].conditions[0].consecutive_days).toBe(2);
  });
});

// --------------------------------------------------------------------------- //
// BlockEditor — reachability + labels
// --------------------------------------------------------------------------- //

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX']),
  listInstruments: vi.fn(async () => ({ items: [{ symbol: 'SPX' }], total: 1, skip: 0, limit: 0 })),
  getAvailableCycles: vi.fn(async () => []),
}));

const SPX_INPUT = { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } };

function renderEditor(rules) {
  const onRulesChange = vi.fn();
  return {
    onRulesChange,
    ...render(
      <BlockEditor
        rules={rules}
        onRulesChange={onRulesChange}
        inputs={[SPX_INPUT]}
        indicators={[]}
      />,
    ),
  };
}

describe('BlockEditor — Phase-2 primitives render', () => {
  it('renders a hysteresis condition with the direction select + "hits" phrase', () => {
    const rules = {
      ...emptyRules(),
      entries: [{
        id: 'e1', name: '', input_id: 'X', weight: 50, enabled: true, description: '',
        conditions: [defaultCondition('hysteresis')],
      }],
    };
    renderEditor(rules);
    // the readable connective phrase + direction options
    expect(screen.getByText('hits')).toBeDefined();
    const dir = screen.getByTestId('hysteresis-direction-0-0');
    expect(dir).toBeDefined();
    expect(dir.value).toBe('up');
    expect(screen.getByText('then descends to')).toBeDefined();
    expect(screen.getByText('then rises to')).toBeDefined();
  });

  it('switching the direction select updates the condition to "down"', () => {
    const rules = {
      ...emptyRules(),
      entries: [{
        id: 'e1', name: '', input_id: 'X', weight: 50, enabled: true, description: '',
        conditions: [defaultCondition('hysteresis')],
      }],
    };
    const { onRulesChange } = renderEditor(rules);
    fireEvent.change(screen.getByTestId('hysteresis-direction-0-0'), { target: { value: 'down' } });
    const next = onRulesChange.mock.calls[0][0];
    expect(next.entries[0].conditions[0].direction).toBe('down');
  });

  it('a plain comparator shows the +days reveal and sets consecutive_days on expand', () => {
    const rules = {
      ...emptyRules(),
      entries: [{
        id: 'e1', name: '', input_id: 'X', weight: 50, enabled: true, description: '',
        conditions: [{ op: 'lt', lhs: null, rhs: null }],
      }],
    };
    const { onRulesChange } = renderEditor(rules);
    // collapsed reveal present (N === 1 default)
    fireEvent.click(screen.getByTestId('consecutive-expand-0-0'));
    // now the numeric input renders; type 2
    const input = screen.getByTestId('consecutive-days-0-0');
    fireEvent.change(input, { target: { value: '2' } });
    const next = onRulesChange.mock.calls[0][0];
    expect(next.entries[0].conditions[0].consecutive_days).toBe(2);
  });

  it('N=2 comparator renders the expanded "consecutive days" label directly', () => {
    const rules = {
      ...emptyRules(),
      entries: [{
        id: 'e1', name: '', input_id: 'X', weight: 50, enabled: true, description: '',
        conditions: [{ op: 'lt', lhs: null, rhs: null, consecutive_days: 2 }],
      }],
    };
    renderEditor(rules);
    expect(screen.getByTestId('consecutive-controls-0-0')).toBeDefined();
    expect(screen.getByText('consecutive days')).toBeDefined();
    expect(screen.getByTestId('consecutive-days-0-0').value).toBe('2');
  });
});
