import { describe, it, expect } from 'vitest';
import { computeRunGate } from './runGate';

// Fixtures — minimal v3 signal spec.
const SPOT_INPUT = {
  id: 'X',
  instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
};
const SECOND_SPOT_INPUT = {
  id: 'Y',
  instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'IXIC' },
};
const INST_OP_X = { kind: 'instrument', input_id: 'X', field: 'close' };
const CONST_POS = { kind: 'constant', value: 1 };
const GT_COND = { op: 'gt', lhs: INST_OP_X, rhs: CONST_POS };
const IND_OP_SMA = { kind: 'indicator', indicator_id: 'sma', input_id: 'X', output: 'default' };
const IND_COND = { op: 'gt', lhs: IND_OP_SMA, rhs: CONST_POS };

const SMA_SPEC = { id: 'sma', name: 'SMA', code: 'def compute(...): ...', params: {}, seriesMap: {} };

function emptyRules() {
  return { long_entry: [], long_exit: [], short_entry: [], short_exit: [] };
}

function buildBlock({ input_id = 'X', weight = 1, conditions = [GT_COND] } = {}) {
  return { input_id, weight, conditions };
}

describe('computeRunGate', () => {
  it('returns "Select a signal first" when the signal is null', () => {
    expect(computeRunGate(null, [])).toEqual({
      runDisabledReason: 'Select a signal first',
      missingIds: [],
    });
  });

  it('returns the no-inputs reason when inputs is empty', () => {
    const sig = { id: 's1', inputs: [], rules: emptyRules() };
    expect(computeRunGate(sig, [])).toEqual({
      runDisabledReason: 'Add at least one input at the top of the page.',
      missingIds: [],
    });
  });

  it('returns the input-not-configured reason mentioning the input id', () => {
    const sig = {
      id: 's1',
      inputs: [{ id: 'Z', instrument: { type: 'spot', collection: '', instrument_id: '' } }],
      rules: emptyRules(),
    };
    expect(computeRunGate(sig, [])).toEqual({
      runDisabledReason: 'Input "Z" needs an instrument — open the Inputs panel to pick one.',
      missingIds: [],
    });
  });

  it('returns the empty-blocks reason when all blocks are empty', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: emptyRules(),
    };
    expect(computeRunGate(sig, [])).toEqual({
      runDisabledReason: 'Add at least one block with an input + condition',
      missingIds: [],
    });
  });

  it('returns the missing-long-exit reason when only a long entry exists', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: { ...emptyRules(), long_entry: [buildBlock()] },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Long entry blocks need at least one long exit block — add an exit condition so positions can close.',
    );
  });

  it('returns the missing-short-exit reason when only a short entry exists', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: { ...emptyRules(), short_entry: [buildBlock()] },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Short entry blocks need at least one short exit block — add an exit condition so positions can close.',
    );
  });

  it('returns the block-needs-input reason when a non-empty block has no input_id', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        long_entry: [buildBlock({ input_id: '' })],
        long_exit: [buildBlock()],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Every block needs an input — pick one in the block header.',
    );
  });

  it('returns the missing-weight reason when entry weight is zero', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        long_entry: [buildBlock({ weight: 0 })],
        long_exit: [buildBlock()],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Every entry block needs a positive weight — set a weight > 0 in the block header.',
    );
  });

  it('returns the missing-indicator reason with the ids in missingIds', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        long_entry: [buildBlock({ conditions: [IND_COND] })],
        long_exit: [buildBlock()],
      },
    };
    const gate = computeRunGate(sig, []);
    expect(gate.runDisabledReason).toBe(
      'Missing indicator spec(s): sma. Open the Indicators page to create them first.',
    );
    expect(gate.missingIds).toEqual(['sma']);
  });

  it('returns {runDisabledReason: null, missingIds: []} when the signal is runnable', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT, SECOND_SPOT_INPUT],
      rules: {
        ...emptyRules(),
        long_entry: [buildBlock()],
        long_exit: [buildBlock()],
      },
    };
    expect(computeRunGate(sig, [])).toEqual({
      runDisabledReason: null,
      missingIds: [],
    });
  });

  it('passes with an indicator operand when the spec is available', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        long_entry: [buildBlock({ conditions: [IND_COND] })],
        long_exit: [buildBlock()],
      },
    };
    expect(computeRunGate(sig, [SMA_SPEC])).toEqual({
      runDisabledReason: null,
      missingIds: [],
    });
  });
});
