import { describe, it, expect } from 'vitest';
import { computeRunGate, computeEffectiveTrace } from './runGate';

// Fixtures — minimal v4 signal spec.
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
  return { entries: [], exits: [] };
}

function entryBlock({ id = 'e1', input_id = 'X', weight = 50, conditions = [GT_COND] } = {}) {
  return { id, input_id, weight, conditions };
}

function exitBlock({
  id = 'x1', input_id = 'X', weight = 0, target_entry_block_id = 'e1', conditions = [GT_COND],
} = {}) {
  return { id, input_id, weight, target_entry_block_id, conditions };
}

describe('computeRunGate (v4)', () => {
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

  it('returns the missing-exit reason when only an entry exists', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: { ...emptyRules(), entries: [entryBlock()] },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Entry blocks need at least one exit block — add an exit so positions can close.',
    );
  });

  it('returns the missing-entry reason when only an exit exists', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: { ...emptyRules(), exits: [exitBlock()] },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Exit blocks need at least one entry block to target.',
    );
  });

  it('returns the block-needs-input reason when a non-empty block has no input_id', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ input_id: '' })],
        exits: [exitBlock({ target_entry_block_id: 'e1' })],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Every block needs an input — pick one in the block header.',
    );
  });

  it('rejects entry weight of zero with the signed-weight reason', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ weight: 0 })],
        exits: [exitBlock()],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Every entry block needs a non-zero weight — '
        + 'set a weight between -100 and +100 (sign decides long vs short).',
    );
  });

  it('rejects entry weight magnitude above 100 (no leverage)', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ weight: 125 })],
        exits: [exitBlock()],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Entry block weight must be within -100%…+100% — no leverage.',
    );
  });

  it('rejects an exit that has no target_entry_block_id', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock()],
        exits: [exitBlock({ target_entry_block_id: '' })],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'Every exit block must target an entry block — pick one in the block header.',
    );
  });

  it('rejects an exit whose target does not match any entry', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ id: 'e1' })],
        exits: [exitBlock({ target_entry_block_id: 'orphan' })],
      },
    };
    expect(computeRunGate(sig, []).runDisabledReason).toBe(
      'An exit block references an entry that no longer exists — remove it or pick a new target.',
    );
  });

  it('returns the missing-indicator reason with the ids in missingIds', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ conditions: [IND_COND] })],
        exits: [exitBlock()],
      },
    };
    const gate = computeRunGate(sig, []);
    expect(gate.runDisabledReason).toBe(
      'Missing indicator spec(s): sma. Open the Indicators page to create them first.',
    );
    expect(gate.missingIds).toEqual(['sma']);
  });

  it('returns {runDisabledReason: null, missingIds: []} when the signal is runnable (long entry)', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT, SECOND_SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ weight: 40 })],
        exits: [exitBlock()],
      },
    };
    expect(computeRunGate(sig, [])).toEqual({
      runDisabledReason: null,
      missingIds: [],
    });
  });

  it('returns {runDisabledReason: null, missingIds: []} for a short entry (negative weight)', () => {
    const sig = {
      id: 's1',
      inputs: [SPOT_INPUT],
      rules: {
        ...emptyRules(),
        entries: [entryBlock({ weight: -40 })],
        exits: [exitBlock()],
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
        entries: [entryBlock({ conditions: [IND_COND] })],
        exits: [exitBlock()],
      },
    };
    expect(computeRunGate(sig, [SMA_SPEC])).toEqual({
      runDisabledReason: null,
      missingIds: [],
    });
  });
});

describe('computeEffectiveTrace (v4 dont_repeat filter)', () => {
  it('is exported as a function', () => {
    expect(typeof computeEffectiveTrace).toBe('function');
  });

  it('returns nullish rawTrace unchanged', () => {
    expect(computeEffectiveTrace(null, { dontRepeat: true })).toBeNull();
    expect(computeEffectiveTrace(undefined, { dontRepeat: true })).toBeUndefined();
  });

  it('returns rawTrace unchanged when it has no events array', () => {
    const raw = { timestamps: [1, 2, 3] }; // no events field
    expect(computeEffectiveTrace(raw, { dontRepeat: true })).toBe(raw);
  });

  it('dontRepeat=true rewrites fired_indices to latched_indices per event', () => {
    const raw = {
      timestamps: [1, 2, 3, 4],
      events: [
        {
          input_id: 'X', block_id: 'b1', kind: 'entry',
          fired_indices: [0, 1, 2, 3],
          latched_indices: [0, 2],
          active_indices: [0, 1, 2],
        },
        {
          input_id: 'X', block_id: 'x1', kind: 'exit',
          target_entry_block_id: 'b1',
          fired_indices: [1, 3],
          latched_indices: [3],
        },
      ],
    };
    const out = computeEffectiveTrace(raw, { dontRepeat: true });
    expect(out).not.toBe(raw); // new object
    expect(out.events).toHaveLength(2);
    expect(out.events[0].fired_indices).toEqual([0, 2]);
    expect(out.events[1].fired_indices).toEqual([3]);
    // Other event fields preserved.
    expect(out.events[0].latched_indices).toEqual([0, 2]);
    expect(out.events[0].active_indices).toEqual([0, 1, 2]);
    expect(out.events[1].target_entry_block_id).toBe('b1');
    // Top-level passthrough.
    expect(out.timestamps).toBe(raw.timestamps);
  });

  it('dontRepeat=false returns fired_indices per event (raw)', () => {
    const raw = {
      events: [
        { input_id: 'X', kind: 'entry', fired_indices: [0, 1, 2, 3], latched_indices: [0, 2] },
      ],
    };
    const out = computeEffectiveTrace(raw, { dontRepeat: false });
    expect(out.events[0].fired_indices).toEqual([0, 1, 2, 3]);
  });

  it('event with empty latched_indices renders as zero markers when dontRepeat=true', () => {
    const raw = {
      events: [
        { input_id: 'X', kind: 'entry', fired_indices: [0, 1, 2], latched_indices: [] },
      ],
    };
    const out = computeEffectiveTrace(raw, { dontRepeat: true });
    expect(out.events[0].fired_indices).toEqual([]);
  });

  it('exit event: latched_indices is already the "actually closed a position" set (regression)', () => {
    // Backend contract: on an exit block, latched_indices is exactly
    // the bars where the exit closed something. This is a pure
    // passthrough for us — we trust the backend and don't recompute.
    const raw = {
      events: [
        {
          input_id: 'X', kind: 'exit', block_id: 'x1', target_entry_block_id: 'e1',
          fired_indices: [1, 2, 3, 4],   // exit condition fired on these bars
          latched_indices: [2, 4],       // only bars 2 and 4 actually closed a position
        },
      ],
    };
    const out = computeEffectiveTrace(raw, { dontRepeat: true });
    expect(out.events[0].fired_indices).toEqual([2, 4]);
  });

  it('does not mutate the input rawTrace or its events', () => {
    const raw = {
      events: [{ kind: 'entry', fired_indices: [0, 1, 2], latched_indices: [1] }],
    };
    computeEffectiveTrace(raw, { dontRepeat: true });
    expect(raw.events[0].fired_indices).toEqual([0, 1, 2]);
    expect(raw.events[0].latched_indices).toEqual([1]);
  });

  it('missing latched/fired arrays default to [] (no crashes)', () => {
    const raw = { events: [{ kind: 'entry' }] };
    const onTrue = computeEffectiveTrace(raw, { dontRepeat: true });
    const onFalse = computeEffectiveTrace(raw, { dontRepeat: false });
    expect(onTrue.events[0].fired_indices).toEqual([]);
    expect(onFalse.events[0].fired_indices).toEqual([]);
  });

  it('defaults to "raw" (dontRepeat=false) when opts are omitted', () => {
    const raw = {
      events: [{ kind: 'entry', fired_indices: [0, 1, 2], latched_indices: [1] }],
    };
    const out = computeEffectiveTrace(raw);
    expect(out.events[0].fired_indices).toEqual([0, 1, 2]);
  });
});
