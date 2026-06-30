import { useState, useCallback } from 'react';
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

// Default window (bars) seeded when a block is first switched AND → THEN.
const DEFAULT_LINK_WINDOW = 5;

/**
 * Re-chain a block's temporal ``links`` after the condition at ``removedIdx``
 * is removed. The backend requires a chain to cover EVERY successor gap
 * (``{1..n-1}``) or none, so a chained block must stay a FULL chain over the
 * surviving conditions — never a partial one.
 *
 * Strategy: if the block was a chain (any links) and ≥2 conditions remain,
 * rebuild a full chain over the ``remainingCount`` conditions, preserving the
 * window of each surviving gap where it can be mapped back and defaulting any
 * new/ambiguous gap to ``defaultWindow``. If <2 conditions remain, or it
 * wasn't a chain, return ``undefined`` (CNF).
 *
 * Window mapping: a surviving gap at new successor index ``j`` (1-based)
 * corresponds to the OLD successor index of the condition that now sits at new
 * position ``j`` — i.e. old index ``j`` if ``j < removedIdx`` else ``j+1``. If
 * that old key had a window, reuse it; otherwise default.
 *
 * Pure. Defensive against a missing/garbage map.
 */
export function reindexLinksAfterRemoval(links, removedIdx, remainingCount, defaultWindow = DEFAULT_LINK_WINDOW) {
  if (!links || typeof links !== 'object' || Object.keys(links).length === 0) return undefined;
  if (!Number.isInteger(remainingCount) || remainingCount < 2) return undefined;
  const out = {};
  for (let j = 1; j < remainingCount; j += 1) {
    // The new condition at position j came from old position (j < removedIdx ? j : j+1).
    const oldKey = j < removedIdx ? j : j + 1;
    const w = links[oldKey];
    out[j] = (Number.isFinite(w) && w >= 1) ? Math.floor(w) : defaultWindow;
  }
  return out;
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
      const prevConds = b.conditions || [];
      const conditions = [...prevConds, defaultCondition('gt')];
      // A CNF block (no links) stays CNF on add. A CHAINED block must stay a
      // FULL contiguous chain (the backend rejects a partial chain, gate G3),
      // so EXTEND the map: the appended condition opens a new successor gap
      // whose key == the pre-append condition count (1-based successor index).
      // Seed its window with the default — existing windows are kept. Mirrors
      // ``reindexLinksAfterRemoval`` which keeps links consistent on removal.
      const isChain = b.links && typeof b.links === 'object' && Object.keys(b.links).length > 0;
      if (!isChain) return { ...b, conditions };
      const links = { ...b.links, [prevConds.length]: DEFAULT_LINK_WINDOW };
      return { ...b, conditions, links };
    });
    updateBlocks(next);
  }

  function handleRemoveCondition(blockIdx, condIdx) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const nextConds = (b.conditions || []).filter((_, j) => j !== condIdx);
      // A chained block must stay a FULL chain over the surviving conditions
      // (the backend rejects a partial chain). Re-chain: preserve surviving
      // gap windows, default any new gap, drop to CNF if <2 conditions remain.
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

  // Switch the WHOLE block between an ordered chain and plain CNF.
  //
  // The backend (G3) requires a block's links to cover EVERY successor gap
  // ``{1..n-1}`` or none — a partial chain is rejected (HTTP 400). So toggling
  // ANY gap to THEN converts the whole block to a chain: every gap gets a link
  // (existing windows kept, unset gaps seeded with the default window).
  // Toggling any THEN gap back to AND reverts the WHOLE block to CNF (clears
  // all links). This keeps the per-gap chip UX while only ever emitting a
  // backend-valid full chain. Per-link windows stay individually editable via
  // ``handleUpdateLinkWindow``.
  function handleSetChainMode(blockIdx, on) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      const nConds = (b.conditions || []).length;
      if (!on || nConds < 2) {
        // Revert to CNF (or nothing to chain).
        return { ...b, links: undefined };
      }
      const prev = (b.links && typeof b.links === 'object') ? b.links : {};
      const nextLinks = {};
      for (let k = 1; k < nConds; k += 1) {
        const existing = prev[k];
        nextLinks[k] = (Number.isFinite(existing) && existing >= 1)
          ? Math.floor(existing) : DEFAULT_LINK_WINDOW;
      }
      return { ...b, links: nextLinks };
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
                  onSetChainMode={(on) => handleSetChainMode(blockIdx, on)}
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
  onSetChainMode,
  onUpdateLinkWindow,
  onRemoveBlock,
  inputs,
  indicators,
  readOnly = false,
}) {
  const [descOpen, setDescOpen] = useState(false);
  const conditions = block.conditions || [];
  const enabled = block.enabled !== false;
  const links = (block.links && typeof block.links === 'object') ? block.links : null;
  // The block is a chain iff it has any link. Backend enforces full coverage,
  // so when chained every gap is linked (all-or-nothing). A reset block is
  // never a chain (links are rejected there) — guard defensively.
  const isChain = section !== 'resets' && !!links && Object.keys(links).length > 0;

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
        conditions.map((cond, condIdx) => (
          <Condition
            key={condIdx}
            blockIdx={blockIdx}
            condIdx={condIdx}
            isFirst={condIdx === 0}
            condition={cond}
            onChange={(next) => onUpdateCondition(condIdx, next)}
            onRemove={() => onRemoveCondition(condIdx)}
            // Reset blocks never carry a sequence (backend rejects links there),
            // so the THEN toggle is suppressed and they stay pure CNF.
            sequenceable={section !== 'resets'}
            onSetChainMode={onSetChainMode}
            onUpdateLinkWindow={onUpdateLinkWindow}
            // Whole-block chain mode (all-or-nothing: every gap is linked when
            // chained). Each gap's chip flips the whole block.
            isChain={isChain}
            // This gap's individual window (keyed by SUCCESSOR index = condIdx).
            linkWithin={links && Number.isFinite(links[condIdx]) ? links[condIdx] : null}
            inputs={inputs}
            indicators={indicators}
            readOnly={readOnly}
          />
        ))
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
  isFirst,
  condition,
  onChange,
  onRemove,
  sequenceable = true,
  onSetChainMode,
  onUpdateLinkWindow,
  isChain = false,
  linkWithin = null,
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

  // Flip the WHOLE block between an ordered chain and CNF. The backend requires
  // every successor gap linked or none (no partial chain), so each gap's chip
  // toggles the block-wide mode — not just this one gap.
  function toggleChain() {
    onSetChainMode(!isChain);
  }

  // Edit just THIS gap's window (bars); chain membership is unchanged.
  function updateLinkWindow(raw) {
    const n = parseInt(raw, 10);
    onUpdateLinkWindow(condIdx, Number.isFinite(n) && n >= 1 ? n : 1);
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

  // The separator chip between this condition and the previous one. For a
  // sequenceable section it is an AND ⇄ THEN toggle that flips the whole block
  // between CNF and one ordered chain (all gaps linked or none). When chained,
  // each gap also exposes its own "within [W] bars" window. Reset blocks (not
  // sequenceable) keep the plain static AND label.
  let separator = null;
  if (!isFirst) {
    if (!sequenceable) {
      separator = <div className={styles.conditionAndLabel}>AND</div>;
    } else if (isChain) {
      separator = (
        <div className={styles.conditionLinkChip} data-testid={`link-chip-${blockIdx}-${condIdx}`}>
          <button
            type="button"
            className={`${styles.conditionLinkToggle} ${styles.conditionLinkToggleActive}`}
            onClick={toggleChain}
            disabled={readOnly}
            title="Ordered: this condition must fire AFTER the previous one, within the window. Click to revert the whole block to AND."
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
              value={linkWithin ?? DEFAULT_LINK_WINDOW}
              onChange={(e) => updateLinkWindow(e.target.value)}
              aria-label="Within window (bars)"
              data-testid={`link-window-${blockIdx}-${condIdx}`}
              readOnly={readOnly}
            />
            bars
          </span>
        </div>
      );
    } else {
      separator = (
        <div className={styles.conditionLinkChip} data-testid={`link-chip-${blockIdx}-${condIdx}`}>
          <button
            type="button"
            className={styles.conditionLinkToggle}
            onClick={toggleChain}
            disabled={readOnly}
            title="AND (simultaneous). Click to make the whole block an ordered sequence (each condition must fire AFTER the previous, within a window)."
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

export default BlockEditor;
