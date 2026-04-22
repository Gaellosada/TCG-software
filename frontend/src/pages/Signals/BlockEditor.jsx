import { useState } from 'react';
import OperandSlot from './OperandSlot';
import BlockHeader from './BlockHeader';
import ConfirmDialog from '../../components/ConfirmDialog';
import DocView from '../Indicators/DocView';
import {
  ALL_OPS,
  OP_LABELS,
  ROLLING_OP_HELP,
  conditionShape,
  defaultCondition,
  migrateCondition,
} from './conditionOps';
import { defaultBlock, isBlockRunnable, collectEntryIds } from './blockShape';
import { SECTIONS, cascadeDeleteEntry } from './storage';
import styles from './Signals.module.css';

const SECTION_LABELS = {
  entries: 'Entries',
  exits: 'Exits',
};

/**
 * Middle panel — block/condition editor (v4 / signals-refactor-v4).
 *
 * Two-section model: `entries` and `exits`. A block in the exits section
 * additionally picks a `target_entry_block_name` from the signal's entry
 * blocks. Entry deletion cascades through `cascadeDeleteEntry` from
 * storage.js so referencing exits are removed and a brief inline banner
 * surfaces above the Exits list.
 *
 * Props:
 *   rules              {Object}     { entries: [], exits: [] }
 *   onRulesChange      {Function}
 *   inputs             {Array}      the signal's declared inputs
 *   indicators         {Array}
 *   doc                {string}
 *   onDocChange        {Function}
 *   section?           {'entries'|'exits'} — if provided, parent controls the tab
 *   onSectionChange?   {Function}   — parent-controlled tab setter
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
}) {
  // Internal tab state is used when the parent does NOT control section.
  // Supports 'entries' | 'exits' | 'doc'.
  const [internalTab, setInternalTab] = useState('entries');
  const activeTab = sectionProp || internalTab;
  const isDocTab = activeTab === 'doc';
  const section = isDocTab ? 'entries' : activeTab;

  // Cascade notice shown above the Exits list after cascade delete.
  const [cascadeNotice, setCascadeNotice] = useState(null);

  const blocks = Array.isArray(rules?.[section]) ? rules[section] : [];
  const entryBlocks = Array.isArray(rules?.entries) ? rules.entries : [];
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
    // v4: defaults come from blockShape.defaultBlock(section), which stamps
    // a stable id and adds target_entry_block_name on exits.
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
      return { ...b, conditions: [...(b.conditions || []), defaultCondition('gt')] };
    });
    updateBlocks(next);
  }

  function handleRemoveCondition(blockIdx, condIdx) {
    const next = blocks.map((b, i) => {
      if (i !== blockIdx) return b;
      return { ...b, conditions: (b.conditions || []).filter((_, j) => j !== condIdx) };
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
            readOnly={false}
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
                  isFirst={blockIdx === 0}
                  onUpdateBlock={(next) => handleUpdateBlock(blockIdx, next)}
                  onAddCondition={() => handleAddCondition(blockIdx)}
                  onRemoveCondition={(condIdx) => handleRemoveCondition(blockIdx, condIdx)}
                  onUpdateCondition={(condIdx, next) => handleUpdateCondition(blockIdx, condIdx, next)}
                  onRemoveBlock={() => handleRemoveBlock(blockIdx)}
                  inputs={inputs}
                  indicators={indicators}
                />
              ))
            )}
            <button
              type="button"
              className={styles.addBlockBtn}
              onClick={handleAddBlock}
              data-testid="add-block-btn"
            >
              + Add block (OR)
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
  isFirst,
  onUpdateBlock,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onRemoveBlock,
  inputs,
  indicators,
}) {
  const conditions = block.conditions || [];
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
      className={styles.block}
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
        onChange={onUpdateBlock}
        onDelete={onRemoveBlock}
        blockIndex={blockIdx + 1}
        status={runnable ? 'ok' : 'warn'}
        blockIdx={blockIdx}
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
            inputs={inputs}
            indicators={indicators}
          />
        ))
      )}
      <button
        type="button"
        className={styles.addCondBtn}
        onClick={onAddCondition}
        data-testid={`add-condition-${blockIdx}`}
      >
        + Add condition (AND)
      </button>
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
  inputs,
  indicators,
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const shape = conditionShape(condition.op);

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

  const opSelect = (
    <select
      className={styles.opSelect}
      value={condition.op}
      onChange={(e) => updateOp(e.target.value)}
      aria-label="Operator"
      data-testid={`op-select-${blockIdx}-${condIdx}`}
    >
      {ALL_OPS.map((op) => (
        <option key={op} value={op}>{OP_LABELS[op] || op}</option>
      ))}
    </select>
  );

  return (
    <div className={styles.condition} data-testid={`condition-${blockIdx}-${condIdx}`}>
      {!isFirst && <div className={styles.conditionAndLabel}>AND</div>}
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
              />
            </div>
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
              />
            </div>
          </>
        )}
        {shape === 'rolling' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.operand}
                onChange={(next) => updateOperand('operand', next)}
                inputs={inputs}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} operand`}
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
