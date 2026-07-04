import { useState, useCallback, Fragment } from 'react';
import OperandSlot from './OperandSlot';
import BlockHeader from './BlockHeader';
import ConfirmDialog from '../../components/ConfirmDialog';
import DocView from '../Indicators/DocView';
import {
  ALL_OPS,
  CROSS_OPS,
  OP_LABELS,
  ROLLING_OP_HELP,
  conditionShape,
  defaultCondition,
  migrateCondition,
  isLegacyOp,
} from './conditionOps';
import { defaultBlock, isBlockRunnable, collectEntryIds } from './blockShape';
import { SECTIONS, cascadeDeleteEntry } from './storage';
import styles from './Signals.module.css';

const SECTION_LABELS = {
  entries: 'Entries',
  exits: 'Exits',
  resets: 'Resets',
};

const ADD_BLOCK_LABELS = {
  entries: '+ Add block (OR)',
  exits: '+ Add block (OR)',
  resets: '+ Add reset block',
};

// Default window (bars) seeded when a gap is switched AND → THEN.
const DEFAULT_LINK_WINDOW = 5;

/**
 * Re-index a block's temporal ``links`` (the set of THEN-boundary gaps) after
 * the condition at ``removedIdx`` (0-based) is removed. ``links`` is a subset of
 * ``{1..n-1}`` keyed by SUCCESSOR index; partial maps are valid (each present
 * gap = a THEN boundary; absent = AND). Removing a condition MERGES the two gaps
 * on either side of it, so:
 *   - a gap ``k < removedIdx`` is untouched (both endpoints survive at the same
 *     index);
 *   - the gap ``k == removedIdx`` (the boundary INTO the removed condition) is
 *     dropped;
 *   - a gap ``k > removedIdx`` shifts down by one (its later endpoint slides
 *     left), keeping its window. The merged gap therefore inherits the window
 *     of the boundary that led OUT of the removed condition.
 * Windows are preserved verbatim — no re-seeding. Returns ``undefined`` (CNF)
 * when nothing survives or < 2 conditions remain.
 *
 * Pure. Defensive against a missing / garbage map.
 */
export function reindexLinksAfterRemoval(links, removedIdx, remainingCount) {
  if (!links || typeof links !== 'object' || Object.keys(links).length === 0) return undefined;
  if (!Number.isInteger(remainingCount) || remainingCount < 2) return undefined;
  const out = {};
  for (const [k, v] of Object.entries(links)) {
    const idx = Number(k);
    if (!Number.isInteger(idx) || idx < 1) continue;
    const w = (Number.isFinite(v) && v >= 1) ? Math.floor(v) : null;
    if (w === null) continue;
    if (idx < removedIdx) {
      out[String(idx)] = w;
    } else if (idx === removedIdx) {
      // Boundary into the removed condition — dropped (gaps merge).
      continue;
    } else {
      const newIdx = idx - 1;
      if (newIdx >= 1 && newIdx < remainingCount) out[String(newIdx)] = w;
    }
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Partition a block's conditions into conjunction groups from its THEN
 * boundaries. A gap present in ``links`` (keyed by the later condition's
 * successor index) starts a new group; absent = same group. Returns an array of
 * arrays of 0-based condition indices. Zero links ⇒ one group ⇒ CNF.
 *
 * Pure.
 */
export function partitionGroups(condCount, links) {
  const groups = [];
  let current = [];
  for (let i = 0; i < condCount; i += 1) {
    if (i > 0 && links && (i in links)) {
      groups.push(current);
      current = [];
    }
    current.push(i);
  }
  if (current.length > 0) groups.push(current);
  return groups;
}

/**
 * Middle panel — block/condition editor (v4 / signals-refactor-v4).
 *
 * Three-section model: `entries`, `exits`, and `resets`. A block in the
 * exits section picks one OR MORE `target_entry_block_names` from the
 * signal's entry blocks (v6 — one exit may close several entries). Reset
 * blocks are signal-global (no per-block input or target). Entry deletion
 * cascades through `cascadeDeleteEntry` from storage.js: the deleted
 * entry's name is stripped from every exit's target list and an exit is
 * removed only if its list becomes empty; a brief inline banner surfaces
 * above the Exits list when whole exits are removed.
 *
 * Props:
 *   rules              {Object}     { entries: [], exits: [], resets: [] }
 *   onRulesChange      {Function}
 *   inputs             {Array}      the signal's declared inputs
 *   indicators         {Array}
 *   doc                {string}
 *   onDocChange        {Function}
 *   section?           {'entries'|'exits'|'resets'} — if provided, parent controls the tab
 *   onSectionChange?   {Function}   — parent-controlled tab setter
 *   readOnly?          {boolean}    — VIEW-only mode (locked signal). Tab
 *                                     switching + block/description expand stay
 *                                     interactive; every EDIT control (add
 *                                     block/condition, operands, weights, names,
 *                                     enable toggle, delete, doc edit) is
 *                                     disabled. Threaded into Block / Condition /
 *                                     BlockHeader / OperandSlot / DocView.
 */
function BlockEditor({
  rules,
  onRulesChange,
  inputs,
  indicators,
  doc,
  onDocChange,
  section: sectionProp,
  onSectionChange,
  readOnly = false,
}) {
  // Internal tab state is used when the parent does NOT control section.
  // Supports 'entries' | 'exits' | 'resets' | 'doc'.
  const [internalTab, setInternalTab] = useState('entries');
  const activeTab = sectionProp || internalTab;
  const isDocTab = activeTab === 'doc';
  const section = isDocTab ? 'entries' : activeTab;

  // Cascade notice shown above the Exits list after cascade delete.
  const [cascadeNotice, setCascadeNotice] = useState(null);

  const blocks = Array.isArray(rules?.[section]) ? rules[section] : [];
  const entryBlocks = Array.isArray(rules?.entries) ? rules.entries : [];
  const resetBlocks = Array.isArray(rules?.resets) ? rules.resets : [];
  const entryIds = collectEntryIds(entryBlocks);

  function setTab(next) {
    if (onSectionChange && next !== 'doc') {
      onSectionChange(next);
    } else {
      setInternalTab(next);
    }
  }

  function updateRules(nextRules) {
    onRulesChange(nextRules);
  }

  function updateBlocks(nextBlocks) {
    onRulesChange({ ...rules, [section]: nextBlocks });
  }

  function handleAddBlock() {
    // defaults come from blockShape.defaultBlock(section), which stamps
    // a stable id and adds target_entry_block_names: [] on exits.
    updateBlocks([...blocks, defaultBlock(section)]);
  }

  function handleRemoveBlock(blockIdx) {
    if (section === 'entries') {
      const removed = blocks[blockIdx];
      // Cascade delete: pure helper returns a new signal-shaped object. We
      // only have rules here, so we emulate the signal by nesting under
      // { rules } and extract back out.
      const signalLike = { rules };
      const nextSignal = cascadeDeleteEntry(signalLike, removed?.id);
      const nextRules = nextSignal.rules;
      const removedExitCount = (rules.exits || []).length - (nextRules.exits || []).length;
      if (removedExitCount > 0) {
        setCascadeNotice(
          `Deleted entry removed ${removedExitCount} referencing exit${removedExitCount === 1 ? '' : 's'}`
        );
        // eslint-disable-next-line no-console
        console.warn(
          `[signals] cascade delete: entry ${removed?.id} removed ${removedExitCount} referencing exit(s)`
        );
      } else {
        setCascadeNotice(null);
      }
      updateRules(nextRules);
    } else {
      updateBlocks(blocks.filter((_, i) => i !== blockIdx));
    }
  }

  function handleUpdateBlock(blockIdx, nextBlock) {
    updateBlocks(blocks.map((b, i) => (i === blockIdx ? nextBlock : b)));
  }

  function handleAddCondition(blockIdx) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const conditions = [...(b.conditions || []), defaultCondition('gt')];
      // The appended condition joins the last group with AND by default (no new
      // THEN boundary). Existing links keep their meaning — their keys are all
      // < the old condition count, so they stay in range. The user can promote
      // the new gap to THEN with its connector.
      return { ...b, conditions };
    });
    updateBlocks(next);
  }

  function handleRemoveCondition(blockIdx, condIdx) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const nextConds = (b.conditions || []).filter((_, j) => j !== condIdx);
      // Links are a partial THEN-boundary map, so removing a condition just
      // merges the two gaps around it: the boundary INTO the removed condition
      // is dropped and later gaps shift down by one, each keeping its window
      // verbatim (no re-seeding). Falls back to CNF (links omitted) when
      // nothing survives or < 2 conditions remain.
      const nextLinks = reindexLinksAfterRemoval(b.links, condIdx, nextConds.length);
      return {
        ...b,
        conditions: nextConds,
        links: nextLinks, // undefined ⇒ CNF
      };
    });
    updateBlocks(next);
  }

  function handleUpdateCondition(blockIdx, condIdx, nextCondition) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      return {
        ...b,
        conditions: (b.conditions || []).map((c, j) => (j === condIdx ? nextCondition : c)),
      };
    });
    updateBlocks(next);
  }

  // Toggle a SINGLE gap (successor index ``condIdx``) between AND and THEN.
  //
  // The backend now accepts any subset of ``{1..n-1}`` as THEN boundaries, so
  // each gap is independent: presenting a key = THEN (seed the default window),
  // deleting it = AND. When the last key is removed the block falls back to CNF
  // (links omitted). Per-gap windows stay individually editable via
  // ``handleUpdateLinkWindow``.
  function handleToggleLink(blockIdx, condIdx) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const prev = (b.links && typeof b.links === 'object') ? { ...b.links } : {};
      if (condIdx in prev) {
        delete prev[condIdx]; // THEN → AND
      } else {
        prev[condIdx] = DEFAULT_LINK_WINDOW; // AND → THEN
      }
      const links = Object.keys(prev).length > 0 ? prev : undefined;
      return { ...b, links };
    });
    updateBlocks(next);
  }

  // Edit a SINGLE per-link window (bars) without changing chain membership.
  // Only meaningful when the block is already a chain. Clamps to int >= 1.
  function handleUpdateLinkWindow(blockIdx, condIdx, within) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const prev = (b.links && typeof b.links === 'object') ? b.links : {};
      if (!(condIdx in prev)) return b; // not a chain / no such gap — no-op
      const w = Number.isFinite(within) && within >= 1 ? Math.floor(within) : 1;
      return { ...b, links: { ...prev, [condIdx]: w } };
    });
    updateBlocks(next);
  }

  function dismissCascadeNotice() {
    setCascadeNotice(null);
  }

  return (
    <div className={styles.blockEditor} data-testid="block-editor">
      <div className={styles.sectionTabs} role="tablist">
        {SECTIONS.map((sec) => {
          const count = (rules?.[sec] || []).length;
          return (
            <button
              type="button"
              key={sec}
              role="tab"
              aria-selected={activeTab === sec}
              data-testid={`section-tab-${sec}`}
              className={`${styles.sectionTab} ${activeTab === sec ? styles.sectionTabActive : ''}`}
              onClick={() => setTab(sec)}
            >
              {SECTION_LABELS[sec]}
              {count > 0 && (
                <span className={styles.sectionTabCount}> ({count})</span>
              )}
            </button>
          );
        })}
        <span className={styles.directionTabSpacer} />
        <button
          type="button"
          role="tab"
          aria-selected={isDocTab}
          data-testid="section-tab-doc"
          className={`${styles.sectionTab} ${isDocTab ? styles.sectionTabActive : ''}`}
          onClick={() => setInternalTab('doc')}
        >
          Documentation
        </button>
      </div>

      {isDocTab ? (
        <div className={styles.docViewWrapper}>
          <DocView
            value={doc}
            onChange={onDocChange}
            readOnly={readOnly}
            placeholder="No documentation yet. Click Edit to add some."
          />
        </div>
      ) : (
        <>
          <div className={styles.directionHint}>
            Blocks are <strong>OR</strong>&rsquo;d. Conditions in a block are <strong>AND</strong>&rsquo;d.
          </div>

          {section === 'exits' && cascadeNotice && (
            <div className={styles.cascadeNotice} role="status" data-testid="cascade-notice">
              <span>{cascadeNotice}</span>
              <button
                type="button"
                className={styles.cascadeNoticeDismiss}
                onClick={dismissCascadeNotice}
                aria-label="Dismiss notice"
                data-testid="cascade-notice-dismiss"
              >
                ×
              </button>
            </div>
          )}

          <div className={styles.blocksList}>
            {blocks.length === 0 ? (
              <div className={styles.blocksEmpty}>
                No blocks. Add one to express a rule in this section.
              </div>
            ) : (
              blocks.map((block, blockIdx) => (
                <Block
                  key={block.id || `${section}-${blockIdx}`}
                  blockIdx={blockIdx}
                  block={block}
                  section={section}
                  entryBlocks={entryBlocks}
                  entryIds={entryIds}
                  resetBlocks={resetBlocks}
                  isFirst={blockIdx === 0}
                  onUpdateBlock={(next) => handleUpdateBlock(blockIdx, next)}
                  onAddCondition={() => handleAddCondition(blockIdx)}
                  onRemoveCondition={(condIdx) => handleRemoveCondition(blockIdx, condIdx)}
                  onUpdateCondition={(condIdx, next) => handleUpdateCondition(blockIdx, condIdx, next)}
                  onToggleLink={(condIdx) => handleToggleLink(blockIdx, condIdx)}
                  onUpdateLinkWindow={(condIdx, within) => handleUpdateLinkWindow(blockIdx, condIdx, within)}
                  onRemoveBlock={() => handleRemoveBlock(blockIdx)}
                  inputs={inputs}
                  indicators={indicators}
                  readOnly={readOnly}
                />
              ))
            )}
            <button
              type="button"
              className={styles.addBlockBtn}
              onClick={handleAddBlock}
              data-testid="add-block-btn"
              disabled={readOnly}
            >
              {ADD_BLOCK_LABELS[section] || '+ Add block (OR)'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function Block({
  blockIdx,
  block,
  section,
  entryBlocks,
  entryIds,
  resetBlocks,
  isFirst,
  onUpdateBlock,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onToggleLink,
  onUpdateLinkWindow,
  onRemoveBlock,
  inputs,
  indicators,
  readOnly = false,
}) {
  const [descOpen, setDescOpen] = useState(false);
  const conditions = block.conditions || [];
  const enabled = block.enabled !== false;
  // Reset blocks never carry a sequence (backend rejects links there), so a
  // reset block is always pure CNF (one group). Elsewhere ``links`` is the set
  // of THEN-boundary gaps.
  const links = (section !== 'resets' && block.links && typeof block.links === 'object')
    ? block.links
    : null;
  const sequenceable = section !== 'resets';
  // Partition conditions into conjunction groups from the THEN boundaries. One
  // group ⇒ plain CNF (rendered flat); >1 group ⇒ each group is visually bound
  // and separated by a THEN connector so ``(A AND B) THEN (C AND D)`` reads
  // unambiguously.
  const groups = partitionGroups(conditions.length, links);
  const multiGroup = groups.length > 1;
  // Static reminder shown on ENTRY blocks that carry an in-progress THEN chain:
  // a targeting exit resets that in-flight sequence. Always-on backend
  // behaviour; surfaced as a note. Scoped to chains ONLY — the UI can author
  // only ``rolling``-mode cross-counts (no count_mode control; the API folds an
  // absent count_mode to "rolling"), and a rolling count's trailing window
  // ages out on its own rather than being reset by an exit, so claiming a
  // tap-count reset would be false for every block a user can build here.
  const hasChain = sequenceable && !!links && Object.keys(links).length > 0;
  const showExitResetNote = section === 'entries' && hasChain;

  const handleToggleEnabled = useCallback((e) => {
    onUpdateBlock({ ...block, enabled: e.target.checked });
  }, [block, onUpdateBlock]);

  const handleDescriptionChange = useCallback((e) => {
    onUpdateBlock({ ...block, description: e.target.value });
  }, [block, onUpdateBlock]);

  // For exits we pass the entry blocks themselves to isBlockRunnable so
  // it can verify the target entry's input is configured (exits inherit
  // the input from their target entry).
  const runnable = isBlockRunnable(
    block,
    section,
    inputs,
    section === 'exits' ? entryBlocks : entryIds,
  );

  return (
    <div
      className={`${styles.block}${enabled ? '' : ` ${styles.blockDisabled}`}`}
      data-testid={`block-${blockIdx}`}
      data-block-id={block.id || ''}
      aria-label={section === 'entries' ? `Entry block ${blockIdx + 1} (${block.id || ''})` : undefined}
    >
      {!isFirst && <div className={styles.blockOrLabel}>OR</div>}
      <BlockHeader
        block={block}
        section={section}
        inputs={inputs}
        entryBlocks={entryBlocks}
        resetBlocks={resetBlocks}
        onChange={onUpdateBlock}
        onDelete={onRemoveBlock}
        blockIndex={blockIdx + 1}
        status={runnable ? 'ok' : 'warn'}
        blockIdx={blockIdx}
        enabled={enabled}
        onToggleEnabled={handleToggleEnabled}
        readOnly={readOnly}
      />
      {conditions.length === 0 ? (
        <div className={styles.blockEmpty}>
          Empty block — add a condition below.
        </div>
      ) : (
        groups.map((group, groupIdx) => {
          // Successor index of this group's first condition == the THEN
          // boundary that opens it (0 for the first group, which has none).
          const boundaryIdx = group[0];
          return (
            <Fragment key={`grp-${boundaryIdx}`}>
              {groupIdx > 0 && (
                <ThenConnector
                  blockIdx={blockIdx}
                  condIdx={boundaryIdx}
                  within={links && Number.isFinite(links[boundaryIdx]) ? links[boundaryIdx] : null}
                  onToggle={() => onToggleLink(boundaryIdx)}
                  onUpdateWindow={onUpdateLinkWindow}
                  readOnly={readOnly}
                />
              )}
              <div
                className={multiGroup ? styles.conditionGroup : undefined}
                data-testid={`condition-group-${blockIdx}-${groupIdx}`}
              >
                {group.map((condIdx, idxInGroup) => (
                  <Condition
                    key={condIdx}
                    blockIdx={blockIdx}
                    condIdx={condIdx}
                    isFirstInGroup={idxInGroup === 0}
                    condition={conditions[condIdx]}
                    onChange={(next) => onUpdateCondition(condIdx, next)}
                    onRemove={() => onRemoveCondition(condIdx)}
                    // Reset blocks never carry a sequence (backend rejects links
                    // there), so the AND↔THEN toggle is suppressed — plain AND.
                    sequenceable={sequenceable}
                    // Promote THIS gap (the one before this condition) to THEN,
                    // splitting the group here.
                    onToggleLink={onToggleLink}
                    inputs={inputs}
                    indicators={indicators}
                    readOnly={readOnly}
                  />
                ))}
              </div>
            </Fragment>
          );
        })
      )}
      {showExitResetNote && (
        <div className={styles.exitResetNote} data-testid={`exit-reset-note-${blockIdx}`}>
          In-progress sequence resets when a targeting exit fires.
        </div>
      )}
      <div className={styles.blockFooter}>
        <button
          type="button"
          className={styles.addCondBtn}
          onClick={onAddCondition}
          data-testid={`add-condition-${blockIdx}`}
          disabled={readOnly}
        >
          + Add condition (AND)
        </button>
        <button
          type="button"
          className={styles.blockDescriptionToggle}
          aria-expanded={descOpen}
          onClick={() => setDescOpen((v) => !v)}
          data-testid={`block-desc-toggle-${blockIdx}`}
        >
          <span className={`${styles.blockDescriptionCaret}${descOpen ? ` ${styles.blockDescriptionCaretOpen}` : ''}`}>
            ▶
          </span>
          Description
        </button>
      </div>
      {descOpen && (
        <textarea
          className={styles.blockDescriptionTextarea}
          value={block.description || ''}
          onChange={handleDescriptionChange}
          placeholder={readOnly ? 'No block description.' : 'Optional block description…'}
          aria-label="Block description"
          data-testid={`block-desc-textarea-${blockIdx}`}
          readOnly={readOnly}
        />
      )}
    </div>
  );
}

function Condition({
  blockIdx,
  condIdx,
  isFirstInGroup,
  condition,
  onChange,
  onRemove,
  sequenceable = true,
  onToggleLink,
  inputs,
  indicators,
  readOnly = false,
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const shape = conditionShape(condition.op);
  const isCross = CROSS_OPS.includes(condition.op);
  const legacy = isLegacyOp(condition.op);
  // ``count`` drives whether the ×N / within W cross controls are shown. A
  // count of 1 (a plain crossover) keeps the row visually identical to the
  // pre-feature crossover — only a small "×N" reveal button is shown.
  const crossCount = Number.isInteger(condition.count) && condition.count >= 1 ? condition.count : 1;
  const crossWindow = Number.isInteger(condition.window) && condition.window >= 1 ? condition.window : 1;
  const [crossOpen, setCrossOpen] = useState(crossCount > 1);

  function updateOp(nextOp) {
    onChange(migrateCondition(condition, nextOp));
  }

  function updateOperand(slot, nextOperand) {
    onChange({ ...condition, [slot]: nextOperand });
  }

  function updateLookback(raw) {
    const n = parseInt(raw, 10);
    onChange({ ...condition, lookback: Number.isFinite(n) ? n : 1 });
  }

  function updateCrossField(field, raw) {
    const n = parseInt(raw, 10);
    onChange({ ...condition, [field]: Number.isFinite(n) && n >= 1 ? n : 1 });
  }

  // The op selector is replaced by a static label for retired (legacy) ops —
  // they are no longer in ALL_OPS so a <select> would render blank.
  const opSelect = legacy ? (
    <span
      className={styles.legacyOpLabel}
      data-testid={`op-legacy-${blockIdx}-${condIdx}`}
      title="Retired operator — still evaluated, but no longer editable. Recreate with another operator if you need to change it."
    >
      {OP_LABELS[condition.op] || condition.op}
      <span className={styles.legacyBadge}>legacy</span>
    </span>
  ) : (
    <select
      className={styles.opSelect}
      value={condition.op}
      onChange={(e) => updateOp(e.target.value)}
      aria-label="Operator"
      data-testid={`op-select-${blockIdx}-${condIdx}`}
      disabled={readOnly}
    >
      {ALL_OPS.map((op) => (
        <option key={op} value={op}>{OP_LABELS[op] || op}</option>
      ))}
    </select>
  );

  // cross ×N / within W controls. Shown only when count > 1 OR the user has
  // expanded them; a plain crossover (count === 1, collapsed) shows just a
  // compact "×N" reveal button so it reads identically to a pre-feature row.
  const crossControls = isCross ? (
    crossCount > 1 || crossOpen ? (
      <div className={styles.crossCountCell} data-testid={`cross-controls-${blockIdx}-${condIdx}`}>
        <span className={styles.conditionInlineLabel}>×</span>
        <input
          type="number"
          min="1"
          step="1"
          className={styles.crossCountInput}
          value={crossCount}
          onChange={(e) => updateCrossField('count', e.target.value)}
          aria-label="Crossings required (N)"
          data-testid={`cross-count-${blockIdx}-${condIdx}`}
          readOnly={readOnly}
        />
        <span className={styles.conditionInlineLabel}>within</span>
        <input
          type="number"
          min="1"
          step="1"
          className={styles.crossCountInput}
          value={crossWindow}
          onChange={(e) => updateCrossField('window', e.target.value)}
          aria-label="Within window (bars)"
          data-testid={`cross-window-${blockIdx}-${condIdx}`}
          readOnly={readOnly}
        />
        <span className={styles.conditionInlineLabel}>bars</span>
      </div>
    ) : (
      <button
        type="button"
        className={styles.crossExpandBtn}
        onClick={() => setCrossOpen(true)}
        title="Require N crossings within a trailing window of W bars"
        aria-label="Add ×N within W controls"
        data-testid={`cross-expand-${blockIdx}-${condIdx}`}
        disabled={readOnly}
      >
        ×N
      </button>
    )
  ) : null;

  // The separator chip above this condition. A condition that OPENS a group
  // (isFirstInGroup) has no in-group separator: either it is the block's very
  // first condition, or a THEN connector (rendered by the parent Block between
  // groups) already precedes it. Within a group, every non-opening condition
  // sits on an AND gap: for a sequenceable section that AND is a toggle
  // (click ⇒ THEN, splitting the group here); reset blocks (not sequenceable)
  // keep a static AND label.
  let separator = null;
  if (!isFirstInGroup) {
    if (!sequenceable) {
      separator = <div className={styles.conditionAndLabel}>AND</div>;
    } else {
      separator = (
        <div className={styles.conditionLinkChip} data-testid={`link-chip-${blockIdx}-${condIdx}`}>
          <button
            type="button"
            className={styles.conditionLinkToggle}
            onClick={() => onToggleLink(condIdx)}
            disabled={readOnly}
            title="AND (simultaneous with the group). Click to make this an ordered THEN step — the conditions after it must fire AFTER this group, within a window."
            data-testid={`link-toggle-${blockIdx}-${condIdx}`}
          >
            AND
          </button>
        </div>
      );
    }
  }

  return (
    <div className={styles.condition} data-testid={`condition-${blockIdx}-${condIdx}`}>
      {separator}
      <div className={styles.conditionRow}>
        <span className={styles.conditionLabel}>Cond {condIdx + 1}</span>
        {shape === 'binary' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.lhs}
                onChange={(next) => updateOperand('lhs', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} lhs`}
                readOnly={readOnly}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.rhs}
                onChange={(next) => updateOperand('rhs', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} rhs`}
                readOnly={readOnly}
              />
            </div>
            {crossControls}
          </>
        )}
        {shape === 'range' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.operand}
                onChange={(next) => updateOperand('operand', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} operand`}
                readOnly={readOnly}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.min}
                onChange={(next) => updateOperand('min', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} min`}
                readOnly={readOnly}
              />
            </div>
            <span className={styles.conditionRangeSep}>..</span>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.max}
                onChange={(next) => updateOperand('max', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} max`}
                readOnly={readOnly}
              />
            </div>
          </>
        )}
        {shape === 'rolling' && (
          // Rolling is RETIRED from authoring (block-temporal-composition v1):
          // it is still evaluated by the backend and fully rendered here as a
          // read-only "legacy" chip — operand, the static op label, and
          // lookback are all VISIBLE but not editable (recreate the condition
          // with a current operator to change it). The row can still be
          // deleted via the × button.
          <>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.operand}
                onChange={(next) => updateOperand('operand', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} operand`}
                readOnly={readOnly || legacy}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            {ROLLING_OP_HELP[condition.op] && (
              <button
                type="button"
                className={styles.rollingInfoBtn}
                title={ROLLING_OP_HELP[condition.op]}
                aria-label={`About ${OP_LABELS[condition.op] || condition.op}`}
                data-testid={`rolling-info-${blockIdx}-${condIdx}`}
                onClick={(e) => e.preventDefault()}
              >
                i
              </button>
            )}
            <div className={styles.conditionLookbackCell}>
              <span className={styles.conditionInlineLabel}>lookback</span>
              <input
                type="number"
                min="1"
                step="1"
                className={styles.lookbackInput}
                value={condition.lookback ?? 1}
                onChange={(e) => updateLookback(e.target.value)}
                aria-label="Lookback (int)"
                readOnly={readOnly || legacy}
              />
            </div>
          </>
        )}
        <button
          type="button"
          className={styles.deleteBtn}
          onClick={() => setConfirmRemove(true)}
          title="Remove condition"
          aria-label={`Remove condition ${condIdx + 1} of block ${blockIdx + 1}`}
          data-testid={`remove-condition-${blockIdx}-${condIdx}`}
          disabled={readOnly}
        >
          ×
        </button>
      </div>
      <ConfirmDialog
        open={confirmRemove}
        title="Delete condition?"
        message="This condition will be removed from the block."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => { setConfirmRemove(false); onRemove(); }}
        onCancel={() => setConfirmRemove(false)}
      />
    </div>
  );
}

/**
 * The THEN boundary rendered BETWEEN two conjunction groups. Carries the
 * ordered-sequence toggle (click ⇒ merge the groups back to one AND group) and
 * the per-boundary "within [W] bars" window. Keyed by the successor index of
 * the group it opens (``condIdx``) so its ``link-toggle`` / ``link-window``
 * testids are unique per gap — the same gap never also renders an AND chip.
 */
function ThenConnector({ blockIdx, condIdx, within, onToggle, onUpdateWindow, readOnly = false }) {
  function updateWindow(raw) {
    const n = parseInt(raw, 10);
    onUpdateWindow(condIdx, Number.isFinite(n) && n >= 1 ? n : 1);
  }
  return (
    <div
      className={styles.conditionThenConnector}
      data-testid={`then-connector-${blockIdx}-${condIdx}`}
    >
      <button
        type="button"
        className={`${styles.conditionLinkToggle} ${styles.conditionLinkToggleActive}`}
        onClick={onToggle}
        disabled={readOnly}
        title="Ordered: the group below must fire AFTER the group above, within the window. Click to merge back into one AND group."
        data-testid={`link-toggle-${blockIdx}-${condIdx}`}
      >
        THEN
      </button>
      <span className={styles.linkWindowWrap}>
        within
        <input
          type="number"
          min="1"
          step="1"
          className={styles.linkWindowInput}
          value={within ?? DEFAULT_LINK_WINDOW}
          onChange={(e) => updateWindow(e.target.value)}
          aria-label="Within window (bars)"
          data-testid={`link-window-${blockIdx}-${condIdx}`}
          readOnly={readOnly}
        />
        bars
      </span>
    </div>
  );
}

export default BlockEditor;
