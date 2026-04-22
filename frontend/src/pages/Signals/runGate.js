// Pure Run-button gate for the Signals page — v4.
//
// Returns ``{runDisabledReason, missingIds}`` where:
//   - ``runDisabledReason`` is null when the signal is runnable, or
//     a user-visible string (verbatim — any edit is a UX change).
//   - ``missingIds`` is the list of indicator_ids referenced by the
//     signal that don't have a spec in ``availableIndicators``.
//
// v4 section model: ``rules = { entries, exits }``. Exits target a
// specific entry by ``target_entry_block_name``; an exit whose target has
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

  // Duplicate entry names invalidate the run: ambiguous exit targets.
  const seenNames = new Set();
  for (const e of entries) {
    const n = e && typeof e.name === 'string' ? e.name : '';
    if (n && seenNames.has(n)) {
      return {
        runDisabledReason: `duplicate-entry-names: "${n}" — two entry blocks share the same name. Rename one so exits can target them unambiguously.`,
        missingIds: [],
      };
    }
    if (n) seenNames.add(n);
  }

  // Flatten to a tagged list we can walk uniformly.
  const blocksWithSection = [
    ...entries.map((b) => ({ block: b, section: 'entries' })),
    ...exits.map((b) => ({ block: b, section: 'exits' })),
  ];
  // A block is "non-empty" if the user has interacted with it at all.
  // For entries that means any condition or picked input; for exits it
  // means any condition or picked target (exits no longer carry input_id).
  const nonEmpty = blocksWithSection.filter(({ block: b, section }) => {
    if (!b) return false;
    const hasCond = (b.conditions || []).length > 0;
    if (section === 'entries') return hasCond || !!b.input_id;
    return hasCond || !!b.target_entry_block_name;
  });
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
    if (!(b.conditions || []).length) {
      return {
        runDisabledReason: 'Every block needs at least one condition.',
        missingIds: [],
      };
    }
    if (section === 'entries') {
      if (!b.input_id) {
        return {
          runDisabledReason: 'Every entry block needs an input — pick one in the block header.',
          missingIds: [],
        };
      }
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
      const tgt = b.target_entry_block_name;
      if (typeof tgt !== 'string' || !tgt) {
        return {
          runDisabledReason: 'Every exit block must target an entry block — pick one in the block header.',
          missingIds: [],
        };
      }
      const matchingEntries = entries.filter((e) => e && e.name === tgt);
      if (matchingEntries.length === 0) {
        return {
          runDisabledReason: `exit-target-not-found: "${tgt}" — this exit references an entry name that doesn't exist. Pick a valid target.`,
          missingIds: [],
        };
      }
      // matchingEntries.length > 1 is already caught by the duplicate-name check above
    }
    // For exits we pass the entry blocks themselves so isBlockRunnable
    // can additionally verify the resolved target entry has a configured
    // input (exits inherit their input from the target entry).
    const entryArg = section === 'exits' ? entries : entryIds;
    if (!isBlockRunnable(b, section, inputs, entryArg)) {
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
 *                          its ``target_entry_block_name``.
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
