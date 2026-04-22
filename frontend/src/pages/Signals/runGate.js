// Pure Run-button gate for the Signals page — v4.
//
// Returns ``{runDisabledReason, missingIds}`` where:
//   - ``runDisabledReason`` is null when the signal is runnable, or
//     a user-visible string (verbatim — any edit is a UX change).
//   - ``missingIds`` is the list of indicator_ids referenced by the
//     signal that don't have a spec in ``availableIndicators``.
//
// v4 section model: ``rules = { entries, exits }``. Exits target a
// specific entry by ``target_entry_block_id``; an exit whose target has
// been deleted is not runnable.
import { buildComputeRequestBody } from './requestBuilder';
import {
  isBlockRunnable,
  isInputConfigured,
  collectEntryIds,
} from './blockShape';

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
  const entries = Array.isArray(rules.entries) ? rules.entries : [];
  const exits = Array.isArray(rules.exits) ? rules.exits : [];

  const entryIds = collectEntryIds(entries);

  // Flatten to a tagged list we can walk uniformly.
  const blocksWithSection = [
    ...entries.map((b) => ({ block: b, section: 'entries' })),
    ...exits.map((b) => ({ block: b, section: 'exits' })),
  ];
  const nonEmpty = blocksWithSection.filter(({ block: b }) => (
    (b && ((b.conditions || []).length > 0 || b.input_id))
  ));
  if (nonEmpty.length === 0) {
    return {
      runDisabledReason: 'Add at least one block with an input + condition',
      missingIds: [],
    };
  }

  // Entry blocks need at least one exit block so positions can close.
  if (entries.length > 0 && exits.length === 0) {
    return {
      runDisabledReason: 'Entry blocks need at least one exit block — add an exit so positions can close.',
      missingIds: [],
    };
  }
  // Exit blocks are only meaningful if there is an entry to target.
  if (exits.length > 0 && entries.length === 0) {
    return {
      runDisabledReason: 'Exit blocks need at least one entry block to target.',
      missingIds: [],
    };
  }

  for (const { block: b, section } of nonEmpty) {
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
    if (section === 'entries') {
      if (!Number.isFinite(b.weight) || b.weight === 0) {
        return {
          runDisabledReason: 'Every entry block needs a non-zero weight — '
            + 'set a weight between -100 and +100 (sign decides long vs short).',
          missingIds: [],
        };
      }
      if (Math.abs(b.weight) > 100) {
        return {
          runDisabledReason: 'Entry block weight must be within -100%…+100% — no leverage.',
          missingIds: [],
        };
      }
    }
    if (section === 'exits') {
      const tgt = b.target_entry_block_id;
      if (typeof tgt !== 'string' || !tgt) {
        return {
          runDisabledReason: 'Every exit block must target an entry block — pick one in the block header.',
          missingIds: [],
        };
      }
      if (!entryIds.has(tgt)) {
        return {
          runDisabledReason: 'An exit block references an entry that no longer exists — remove it or pick a new target.',
          missingIds: [],
        };
      }
    }
    if (!isBlockRunnable(b, section, inputs, entryIds)) {
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

/**
 * "Don't repeat" effective-trace filter.
 *
 * Per the backend contract, each event in a compute response carries
 * both:
 *   - ``fired_indices``:   every bar where the block's AND-condition was True;
 *   - ``latched_indices``: the *effective* subset — for entries, bars where
 *                          the block actually opened a position; for exits,
 *                          bars where the exit actually closed something on
 *                          its ``target_entry_block_id``.
 *
 * When ``dontRepeat`` is true the UI should present ``latched_indices`` as
 * the canonical trigger set (no accidental repeats on already-open
 * positions, no exits on already-closed positions). When false, the raw
 * ``fired_indices`` is presented.
 *
 * This is a pure function: returns a NEW rawTrace with each event's
 * ``fired_indices`` rewritten to the effective set (or the raw set) based
 * on the flag. Downstream marker rendering can then read ``fired_indices``
 * without knowing about the flag. The original trace is not mutated.
 *
 * @param {object} rawTrace  the backend compute response (at least
 *                           ``{events: [...]}``). Other fields are
 *                           shallow-copied through unchanged.
 * @param {object} opts
 * @param {boolean} opts.dontRepeat  when true, emit ``latched_indices`` in
 *                                   place of ``fired_indices``.
 * @returns a new object with the transformed ``events`` array. If
 *          ``rawTrace`` is null/undefined it is returned as-is.
 */
export function computeEffectiveTrace(rawTrace, { dontRepeat } = {}) {
  if (!rawTrace || typeof rawTrace !== 'object') return rawTrace;
  const events = Array.isArray(rawTrace.events) ? rawTrace.events : null;
  if (events === null) return rawTrace;
  const mapped = events.map((ev) => {
    if (!ev || typeof ev !== 'object') return ev;
    const effective = dontRepeat
      ? (Array.isArray(ev.latched_indices) ? ev.latched_indices : [])
      : (Array.isArray(ev.fired_indices) ? ev.fired_indices : []);
    return { ...ev, fired_indices: effective };
  });
  return { ...rawTrace, events: mapped };
}
