import { useState, useRef, useEffect } from 'react';
import ConfirmDialog from '../../components/ConfirmDialog';
import { isInputConfigured, coerceResetCount } from './blockShape';
import styles from './Signals.module.css';

/**
 * Per-block controls (v6): for entries, input dropdown (references one
 * of the signal's declared inputs), signed weight input with % suffix
 * and direction badge; for exits, a VERTICAL LIST of target-entry name
 * pickers (one exit may close several entries — cross-input allowed) is
 * shown in the same position as the input dropdown, with a "+ Add block"
 * button to append rows and a per-row remove (×) control. Delete-block
 * button is gated by ConfirmDialog.
 *
 * Props:
 *   block       {Object}   { id, [input_id, weight on entries,
 *                            target_entry_block_names (string[]) on exits] }
 *   section     {string}   'entries' | 'exits' | 'resets'
 *   inputs      {Array}    the signal's declared inputs
 *   entryBlocks {Array}    the signal's entry blocks (used by exits to list targets)
 *   resetBlocks {Array}    the signal's reset blocks (used by entries+exits to bind a require-reset gate)
 *   onChange    {Function} (nextBlock) => void
 *   onDelete    {Function} () => void
 *   blockIndex  {number}   1-based index shown in the label
 *   status      {string}   'ok' | 'warn' (optional)
 *   blockIdx    {number}   0-based index for data-testid (optional)
 */
function BlockHeader({ block, section, inputs, entryBlocks, resetBlocks, onChange, onDelete, blockIndex, status, blockIdx, enabled, onToggleEnabled, readOnly = false }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [editing, setEditing] = useState(false);
  // Local draft for the weight input so the user can type freely before blur
  const [weightDraft, setWeightDraft] = useState(null);
  // Local draft for the reset-count input (same draft-on-blur pattern as weight)
  const [countDraft, setCountDraft] = useState(null);
  const nameRef = useRef(null);

  const isEntry = section === 'entries';
  const isReset = section === 'resets';

  const list = Array.isArray(inputs) ? inputs : [];
  const selectedId = typeof block.input_id === 'string' ? block.input_id : '';
  const resolved = list.find((i) => i && i.id === selectedId) || null;
  const resolvedConfigured = resolved ? isInputConfigured(resolved) : false;

  const displayName = block.name || `Block ${blockIndex}`;

  // Committed weight (number, clamped by storage / parent) used for badge
  const committedWeight = Number.isFinite(block.weight) ? block.weight : 0;

  // Committed reset count — integer ≥ 1; default 1 when absent/invalid so
  // a freshly-bound block (no stored count yet) shows the single-fire value.
  // Uses the ONE shared coercion (storage/wire/UI byte-identical).
  const committedCount = coerceResetCount(block.requires_reset_count);

  useEffect(() => {
    if (editing && nameRef.current) {
      nameRef.current.focus();
      nameRef.current.select();
    }
  }, [editing]);

  // Sync weightDraft when block.weight changes from outside (e.g. load)
  useEffect(() => {
    setWeightDraft(null);
  }, [block.weight]);

  // Sync countDraft when the committed count changes from outside (load).
  useEffect(() => {
    setCountDraft(null);
  }, [block.requires_reset_count]);

  function commitName() {
    if (!nameRef.current) return;
    const trimmed = nameRef.current.value.trim();
    setEditing(false);
    if (!trimmed || trimmed === `Block ${blockIndex}`) {
      onChange({ ...block, name: '' });
    } else {
      onChange({ ...block, name: trimmed });
    }
  }

  function setInputId(id) {
    onChange({ ...block, input_id: id });
  }

  /**
   * Clamp weight to [-100, +100] on blur or on explicit commit.
   * Intermediate string (e.g. "-") is left in draft while editing.
   */
  function commitWeight(raw) {
    setWeightDraft(null);
    const n = raw === '' || raw === '-' ? 0 : parseFloat(raw);
    if (!Number.isFinite(n)) {
      onChange({ ...block, weight: 0 });
      return;
    }
    const clamped = Math.max(-100, Math.min(100, n));
    onChange({ ...block, weight: clamped });
  }

  /**
   * Commit the reset-count on blur / Enter. Delegates to the ONE shared
   * coercion so the committed value is byte-identical to what storage and
   * the wire produce: integer ≥ 1; empty / non-numeric / sub-1 → 1
   * (single-fire default). ``Number('')`` is 0 → <1 → 1, so the empty case
   * needs no special handling here.
   */
  function commitCount(raw) {
    setCountDraft(null);
    onChange({ ...block, requires_reset_count: coerceResetCount(raw) });
  }

  const showUnconfiguredWarning = resolved && !resolvedConfigured;
  const showUnknownWarning = !!selectedId && !resolved;

  // Determine badge
  function badgeProps() {
    if (committedWeight > 0) {
      return { className: styles.badgeLong, text: 'long', ariaLabel: 'direction: long' };
    }
    if (committedWeight < 0) {
      return { className: styles.badgeShort, text: 'short', ariaLabel: 'direction: short' };
    }
    return { className: styles.badgeNeutral, text: '—', ariaLabel: 'direction: neutral' };
  }

  const badge = isEntry ? badgeProps() : null;

  // Display value in the weight input: use the draft string if mid-edit,
  // otherwise the committed numeric value
  const weightDisplayValue = weightDraft !== null
    ? weightDraft
    : committedWeight;

  // Same draft-or-committed display rule for the reset-count input.
  const countDisplayValue = countDraft !== null
    ? countDraft
    : committedCount;

  return (
    <div className={styles.blockHeaderRow} data-testid={`block-header-${blockIndex - 1}`}>
      {status && (
        <span
          className={`${styles.blockStatusDot} ${status === 'ok' ? styles.blockStatusDotOk : styles.blockStatusDotWarn}`}
          title={status === 'ok' ? 'Block ready' : 'Block not yet runnable (pick input + at least one complete condition)'}
          data-testid={`block-status-${blockIdx}`}
          data-runnable={status === 'ok' ? 'true' : 'false'}
          aria-hidden="true"
        />
      )}
      {editing ? (
        <input
          ref={nameRef}
          type="text"
          className={styles.blockNameInput}
          defaultValue={displayName}
          onBlur={commitName}
          onKeyDown={(e) => { if (e.key === 'Enter') commitName(); if (e.key === 'Escape') setEditing(false); }}
          maxLength={32}
          data-testid={`block-name-input-${blockIndex - 1}`}
        />
      ) : (
        <>
          <span className={styles.blockLabel}>{displayName}</span>
          {/* Read-only: the rename pencil is hidden (the name stays visible as
              text) so the user can't enter inline-edit mode. */}
          {!readOnly && (
            <button
              type="button"
              className={styles.blockEditNameBtn}
              onClick={() => setEditing(true)}
              title="Rename block"
              aria-label={`Rename block ${blockIndex}`}
              data-testid={`block-edit-name-${blockIndex - 1}`}
            >
              ✎
            </button>
          )}
        </>
      )}

      {typeof onToggleEnabled === 'function' && (
        <input
          type="checkbox"
          className={styles.blockEnableToggle}
          checked={enabled !== false}
          onChange={onToggleEnabled}
          aria-label="Enable block"
          data-testid={`block-enable-${blockIdx}`}
          disabled={readOnly}
        />
      )}

      {!isReset && (
      <span className={styles.blockDirectionLabel}>
        {isEntry ? 'entry on' : 'exit on'}
      </span>
      )}
      {isReset ? null : isEntry ? (
        <div className={styles.blockInstrumentCell}>
          <select
            className={styles.blockInputSelect}
            value={selectedId}
            onChange={(e) => setInputId(e.target.value)}
            aria-label={`Input for block ${blockIndex}`}
            data-testid={`block-input-select-${blockIndex - 1}`}
            disabled={readOnly}
          >
            <option value="">…</option>
            {list.map((input) => {
              const ok = isInputConfigured(input);
              return (
                <option key={input.id} value={input.id}>
                  {input.id}{!ok ? ' (needs instrument)' : ''}
                </option>
              );
            })}
          </select>
          {showUnconfiguredWarning && (
            <span className={styles.blockInputWarn} title="This input has no instrument yet">!</span>
          )}
          {showUnknownWarning && (
            <span className={styles.blockInputWarn} title={`Unknown input id "${selectedId}"`}>?</span>
          )}
        </div>
      ) : (
        <ExitTargetPicker
          block={block}
          entryBlocks={entryBlocks}
          onChange={onChange}
          blockIndex={blockIndex}
          readOnly={readOnly}
        />
      )}

      {!isReset && (
        <>
          <label className={styles.blockDirectionLabel} htmlFor={`require-reset-${blockIndex}`}>
            require reset
          </label>
          <div className={styles.blockInstrumentCell}>
            <select
              id={`require-reset-${blockIndex}`}
              className={styles.blockInputSelect}
              value={block.requires_reset_block_id || ''}
              onChange={(e) => onChange({
                ...block,
                requires_reset_block_id: e.target.value || null,
              })}
              aria-label={`Require reset for block ${blockIndex}`}
              data-testid={`require-reset-select-${blockIndex - 1}`}
              disabled={readOnly}
            >
              <option value="">None</option>
              {(Array.isArray(resetBlocks) ? resetBlocks : []).map((r, i) => (
                <option key={r.id || i} value={r.id}>
                  {(r.name && r.name.trim()) || `Reset ${i + 1}`}
                </option>
              ))}
            </select>
          </div>
          {block.requires_reset_block_id && (
            <div className={styles.blockWeightCell}>
              {/* Single trailing "×" suffix (reads like "3×"), mirroring the
                  weight input's single "%" suffix — no separate "×" label. */}
              <div className={styles.weightInputWrap}>
                <input
                  id={`reset-count-${blockIndex}`}
                  type="number"
                  step="1"
                  min="1"
                  className={styles.weightInput}
                  value={countDisplayValue}
                  onChange={(e) => setCountDraft(e.target.value)}
                  onBlur={(e) => commitCount(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') commitCount(e.target.value); }}
                  aria-label={`Reset count for block ${blockIndex}`}
                  title="Number of times the bound reset must fire before this block re-arms"
                  data-testid={`reset-count-input-${blockIndex - 1}`}
                  readOnly={readOnly}
                />
                <span className={styles.weightSuffix} aria-hidden="true">×</span>
              </div>
            </div>
          )}
        </>
      )}

      {isEntry && (
        <div className={styles.blockWeightCell}>
          <label className={styles.conditionInlineLabel} htmlFor={`weight-${blockIndex}`}>weight</label>
          <div className={styles.weightInputWrap}>
            <input
              id={`weight-${blockIndex}`}
              type="number"
              step="1"
              min="-100"
              max="100"
              className={styles.weightInput}
              value={weightDisplayValue}
              onChange={(e) => setWeightDraft(e.target.value)}
              onBlur={(e) => commitWeight(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') commitWeight(e.target.value); }}
              aria-label={`Weight for block ${blockIndex}`}
              data-testid={`block-weight-${blockIndex - 1}`}
              readOnly={readOnly}
            />
            <span className={styles.weightSuffix} aria-hidden="true">%</span>
          </div>
          {badge && (
            <span
              className={badge.className}
              aria-label={badge.ariaLabel}
              data-testid={`block-badge-${blockIndex - 1}`}
            >
              {badge.text}
            </span>
          )}
        </div>
      )}

      {!isReset && (
        <div className={styles.blockFireCell}>
          <label className={styles.blockDirectionLabel} htmlFor={`fire-mode-${blockIndex}`}>
            fire
          </label>
          <select
            id={`fire-mode-${blockIndex}`}
            className={styles.blockInputSelect}
            value={block.fire_mode === 'pulse' ? 'pulse' : 'sustained'}
            onChange={(e) => onChange({ ...block, fire_mode: e.target.value })}
            aria-label={`Fire mode for block ${blockIndex}`}
            title="Pulse: fires once on the trigger bar, then re-arms (must go false before firing again). Sustained: stays active every bar the condition holds."
            data-testid={`fire-mode-select-${blockIndex - 1}`}
            disabled={readOnly}
          >
            <option value="pulse">pulse</option>
            <option value="sustained">sustained</option>
          </select>
        </div>
      )}

      <button
        type="button"
        className={styles.blockDeleteBtn}
        onClick={() => setConfirmDelete(true)}
        title={`Remove block ${blockIndex}`}
        aria-label={`Remove block ${blockIndex}`}
        data-testid={`remove-block-${blockIndex - 1}`}
        disabled={readOnly}
      >
        ×
      </button>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete block?"
        message="This block and all of its conditions will be removed."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => { setConfirmDelete(false); onDelete(); }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}

/**
 * Exit-block target picker (v6).
 *
 * Renders a VERTICAL LIST of entry dropdowns bound to
 * ``block.target_entry_block_names`` (array). The first row is always
 * present (it doubles as the empty-state dropdown when no targets are
 * chosen yet). "+ Add block" appends another dropdown row; each row has a
 * remove (×) control.
 *
 * Cross-row dedupe: each dropdown lists every entry EXCEPT those already
 * chosen in OTHER rows — but a row's OWN current value always stays
 * visible/selected in its own dropdown. Unnamed / duplicate-named entries
 * remain present-but-disabled (you can't target them unambiguously).
 *
 * "+ Add block" is disabled when every selectable entry is already chosen
 * or there are no entries at all.
 */
function ExitTargetPicker({ block, entryBlocks, onChange, blockIndex, readOnly = false }) {
  // Number of *extra* empty rows the user has opened beyond the stored
  // names (so "+ Add block" can reveal a fresh dropdown before a value is
  // picked). Declared first — hooks must run unconditionally.
  //
  // R3 fix: this counter is adjusted PRECISELY in setRow/removeRow, never via
  // a blanket "reset to 0 on any name change" effect. The old effect reset it
  // whenever ANY target name changed, so editing one row (e.g. renaming a
  // sibling target) made an opened-but-still-empty extra row vanish. An opened
  // empty row must persist until the user fills it or explicitly removes it.
  // (Switching blocks remounts this component — BlockEditor keys each row by
  // block.id — so cross-block state never leaks; no effect needed for that.)
  const [extraRows, setExtraRows] = useState(0);

  const entryList = Array.isArray(entryBlocks) ? entryBlocks : [];
  const empty = entryList.length === 0;

  // Canonical array of currently-chosen target names.
  const names = Array.isArray(block.target_entry_block_names)
    ? block.target_entry_block_names
    : [];

  // Names that can be unambiguously targeted: named AND not duplicated.
  const nameCounts = new Map();
  for (const e of entryList) {
    if (e && e.name) nameCounts.set(e.name, (nameCounts.get(e.name) || 0) + 1);
  }
  const selectableNames = [];
  for (const [n, c] of nameCounts) {
    if (c === 1) selectableNames.push(n);
  }
  // How many distinct selectable names are already chosen.
  const chosenSelectable = new Set(
    names.filter((n) => selectableNames.includes(n)),
  );
  // "+ Add block" is dead when there's nothing left to add: no entries,
  // no unambiguously-selectable entry, or every selectable one is taken.
  const addDisabled = empty
    || selectableNames.length === 0
    || chosenSelectable.size >= selectableNames.length;

  function commit(nextNames) {
    onChange({ ...block, target_entry_block_names: nextNames });
  }

  function setRow(rowIdx, value) {
    // Operate on the FULLY-rendered row set so editing the implicit first
    // row of an empty array — or a just-added empty row — slots the value
    // at the right index.
    const rendered = names.length > 0
      ? [...names, ...Array(extraRows).fill('')]
      : [''];
    // Was this an opened EXTRA empty row (beyond the stored names)? If so and
    // the user just picked a real name, that row graduates into the stored
    // array — shrink the extra-row counter by one so the rendered row count
    // stays put (the new name re-supplies the row). Editing an already-stored
    // row leaves the counter alone, so sibling empty rows survive (R3).
    const wasExtraEmptyRow = rowIdx >= names.length && !rendered[rowIdx];
    const next = rendered.slice();
    next[rowIdx] = value;
    // Drop blanks (a row reset to "Pick an entry…") so the stored array
    // only ever holds real target names — empty array == "no targets yet".
    if (wasExtraEmptyRow && typeof value === 'string' && value) {
      setExtraRows((n) => Math.max(0, n - 1));
    }
    commit(next.filter((n) => typeof n === 'string' && n));
  }

  function removeRow(rowIdx) {
    // Operate on the FULLY-rendered row set (stored names + any opened
    // empty rows) so removing a trailing just-added empty row works too.
    const rendered = names.length > 0
      ? [...names, ...Array(extraRows).fill('')]
      : [''];
    const next = rendered.slice();
    next.splice(rowIdx, 1);
    const kept = next.filter((n) => typeof n === 'string' && n);
    // If an empty row was removed, also shrink the extra-row counter so the
    // committed names (which are unchanged) don't re-grow the render.
    if (rowIdx >= names.length) {
      setExtraRows((n) => Math.max(0, n - 1));
    }
    commit(kept);
  }

  function addRow() {
    if (addDisabled) return;
    // Reveal a fresh empty dropdown row. The stored array is unchanged
    // until a real name is chosen in it (setRow drops blanks), so we only
    // grow the rendered rows via a local counter.
    setExtraRows((n) => n + 1);
  }


  // Final render rows = stored names (or one implicit empty row) followed
  // by any user-opened extra empty rows.
  const renderRows = names.length > 0
    ? [...names, ...Array(extraRows).fill('')]
    : [''];

  // Stable React keys per rendered row. A named row is keyed by its target
  // name (``name:<name>``) so removing/reordering rows can't bleed a row's
  // identity onto its neighbour; empty rows fall back to a positional token
  // (``empty:<k>``). A defensive ``#n`` suffix disambiguates the rare case
  // where the committed array transiently holds the same name twice (e.g. a
  // dangling name colliding with a real one) so React never sees dupes.
  const seenKeys = new Map();
  let emptyCount = 0;
  const rowKeys = renderRows.map((current) => {
    const base = current ? `name:${current}` : `empty:${emptyCount++}`;
    const n = (seenKeys.get(base) || 0) + 1;
    seenKeys.set(base, n);
    return n === 1 ? base : `${base}#${n}`;
  });

  return (
    <div className={styles.exitTargetList} data-testid={`exit-targets-${blockIndex - 1}`}>
      {renderRows.map((current, rowIdx) => {
        // Names chosen in OTHER rows — excluded from this row's options.
        const chosenElsewhere = new Set(
          renderRows.filter((_, i) => i !== rowIdx).filter((n) => n),
        );
        const isLastRow = rowIdx === renderRows.length - 1;
        const danglingWarn = current && !entryList.some((e) => e.name === current);
        return (
          <div
            className={styles.exitTargetRow}
            key={rowKeys[rowIdx]}
            data-testid={`exit-target-row-${blockIndex - 1}-${rowIdx}`}
          >
            <div className={styles.blockInstrumentCell}>
              <select
                className={styles.blockInputSelect}
                value={current || ''}
                disabled={empty || readOnly}
                onChange={(e) => setRow(rowIdx, e.target.value)}
                aria-label={`Target entry ${rowIdx + 1} for exit block ${blockIndex}`}
                data-testid={`target-entry-select-${blockIndex - 1}-${rowIdx}`}
              >
                {empty ? (
                  <option value="">No entries yet — create an entry block first</option>
                ) : (
                  <>
                    <option value="">Pick an entry…</option>
                    {entryList.map((entry, i) => {
                      const eName = entry.name || `Block ${i + 1}`;
                      const isDuplicate = !!entry.name
                        && nameCounts.get(entry.name) > 1;
                      // Hide entries already chosen in OTHER rows, EXCEPT
                      // this row's own current value (must stay visible).
                      if (entry.name
                          && entry.name !== current
                          && chosenElsewhere.has(entry.name)) {
                        return null;
                      }
                      return (
                        <option
                          key={entry.id || i}
                          value={entry.name || ''}
                          disabled={!entry.name || isDuplicate}
                        >
                          {eName}{isDuplicate ? ' (duplicate)' : ''}{!entry.name ? ' (unnamed)' : ''}
                        </option>
                      );
                    })}
                  </>
                )}
              </select>
              {danglingWarn && (
                <span
                  className={styles.blockInputWarn}
                  title={`Target "${current}" no longer exists`}
                >
                  !
                </span>
              )}
            </div>
            {/* Remove control on every row EXCEPT a lone implicit empty row
                (nothing to remove there). Disabled (kept visible) when locked. */}
            {!(renderRows.length === 1 && !current) && (
              <button
                type="button"
                className={styles.exitTargetRemoveBtn}
                onClick={() => removeRow(rowIdx)}
                title="Remove this target"
                aria-label={`Remove target ${rowIdx + 1} from exit block ${blockIndex}`}
                data-testid={`remove-target-${blockIndex - 1}-${rowIdx}`}
                disabled={readOnly}
              >
                ×
              </button>
            )}
            {isLastRow ? (
              <button
                type="button"
                className={styles.exitTargetAddBtn}
                onClick={addRow}
                disabled={addDisabled || readOnly}
                title={addDisabled
                  ? 'No more entries available to target'
                  : 'Target another entry block'}
                aria-label={`Add another target to exit block ${blockIndex}`}
                data-testid={`add-target-${blockIndex - 1}`}
              >
                + Add block
              </button>
            ) : (
              /* Inert clone reserving the "+ Add block" column on every
                 non-last row, so all rows expose the SAME trailing width and
                 every .blockInstrumentCell (the only shrinkable item) shrinks
                 to the bottom row's width → uniform dropdowns, button kept to
                 the right of the bottom row. A real <button> (not a <span>) so
                 it is byte-for-byte the same box as the live add button — same
                 UA font metrics, pixel-identical width (no magic numbers). It
                 is disabled + tabIndex=-1 + aria-hidden + visibility:hidden +
                 carries NO add-target testid, so it never receives focus, AT,
                 clicks, or collides with getByTestId. */
              <button
                type="button"
                className={styles.exitTargetAddBtn}
                style={{ visibility: 'hidden' }}
                disabled
                tabIndex={-1}
                aria-hidden="true"
              >
                + Add block
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default BlockHeader;
