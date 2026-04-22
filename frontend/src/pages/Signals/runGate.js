// Pure validation: returns the Run-button gate state for a given
// signal + available-indicators set. Extracted from SignalsPage.jsx —
// the 90-LOC useMemo body that decides whether the Run button is
// enabled and, when disabled, the tooltip explaining why.
//
// Returns ``{runDisabledReason, missingIds}`` where:
//   - ``runDisabledReason`` is null when the signal is runnable, or
//     a user-visible string (verbatim — any edit is a UX change).
//   - ``missingIds`` is the list of indicator_ids referenced by the
//     signal that don't have a spec in ``availableIndicators`` — used
//     to highlight the referenced operands in the block editor.
import { buildComputeRequestBody } from './requestBuilder';
import { isBlockRunnable, isInputConfigured } from './blockShape';

export function computeRunGate(selectedSignal, availableIndicators) {
  if (!selectedSignal) return { runDisabledReason: 'Select a signal first', missingIds: [] };
  const inputs = Array.isArray(selectedSignal.inputs) ? selectedSignal.inputs : [];
  if (inputs.length === 0) {
    return {
      runDisabledReason: 'Add at least one input at the top of the page.',
      missingIds: [],
    };
  }
  // Every input that's referenced by the rules must be configured;
  // conservatively require every declared input to be configured so
  // there's no dangling-instrument UX.
  for (const input of inputs) {
    if (!isInputConfigured(input)) {
      return {
        runDisabledReason: `Input "${input.id}" needs an instrument — open the Inputs panel to pick one.`,
        missingIds: [],
      };
    }
  }
  const rules = selectedSignal.rules || {};
  const blocksWithDir = Object.keys(rules).flatMap((dir) => {
    const blocks = Array.isArray(rules[dir]) ? rules[dir] : [];
    return blocks.map((b) => ({ block: b, direction: dir }));
  });
  const nonEmpty = blocksWithDir.filter(({ block: b }) => (
    (b.conditions || []).length > 0 || b.input_id
  ));
  if (nonEmpty.length === 0) {
    return {
      runDisabledReason: 'Add at least one block with an input + condition',
      missingIds: [],
    };
  }
  // Entry blocks require at least one matching exit block.
  const hasLongEntry = (rules.long_entry || []).length > 0;
  const hasLongExit = (rules.long_exit || []).length > 0;
  const hasShortEntry = (rules.short_entry || []).length > 0;
  const hasShortExit = (rules.short_exit || []).length > 0;
  if (hasLongEntry && !hasLongExit) {
    return {
      runDisabledReason: 'Long entry blocks need at least one long exit block — add an exit condition so positions can close.',
      missingIds: [],
    };
  }
  if (hasShortEntry && !hasShortExit) {
    return {
      runDisabledReason: 'Short entry blocks need at least one short exit block — add an exit condition so positions can close.',
      missingIds: [],
    };
  }
  for (const { block: b, direction } of nonEmpty) {
    if (!b.input_id) {
      return {
        runDisabledReason: 'Every block needs an input — pick one in the block header.',
        missingIds: [],
      };
    }
    if (!(b.conditions || []).length) {
      return {
        runDisabledReason: 'Every block needs at least one condition.',
        missingIds: [],
      };
    }
    const isEntry = direction === 'long_entry' || direction === 'short_entry';
    if (isEntry && (!Number.isFinite(b.weight) || b.weight <= 0)) {
      return {
        runDisabledReason: 'Every entry block needs a positive weight — '
          + 'set a weight > 0 in the block header.',
        missingIds: [],
      };
    }
    if (!isBlockRunnable(b, direction, inputs)) {
      return {
        runDisabledReason: 'Every operand must be set — pick an input, '
          + 'indicator or constant for each slot.',
        missingIds: [],
      };
    }
  }
  const { missing } = buildComputeRequestBody(selectedSignal, availableIndicators);
  if (missing.length > 0) {
    return {
      runDisabledReason: `Missing indicator spec(s): ${missing.join(', ')}. `
        + 'Open the Indicators page to create them first.',
      missingIds: missing,
    };
  }
  return { runDisabledReason: null, missingIds: [] };
}
