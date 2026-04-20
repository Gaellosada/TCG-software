import { useState } from 'react';
import OperandSlot from './OperandSlot';
import BlockHeader from './BlockHeader';
import ConfirmDialog from './ConfirmDialog';
import {
  ALL_OPS,
  OP_LABELS,
  conditionShape,
  defaultCondition,
  migrateCondition,
} from './conditionOps';
import { defaultBlock, isBlockRunnable } from './blockShape';
import { DIRECTIONS } from './storage';
import styles from './Signals.module.css';

const DIRECTION_LABELS = {
  long_entry: 'Long entry',
  long_exit: 'Long exit',
  short_entry: 'Short entry',
  short_exit: 'Short exit',
};

/**
 * Middle panel — block/condition editor (iter-3 redesign).
 *
 * Block header uses BlockHeader (instrument + weight + delete).
 * Operand slots use OperandSlot (+ menu / × confirm).
 * Deletes route through ConfirmDialog.
 *
 * Props:
 *   rules              {Object}
 *   onRulesChange      {Function}
 *   indicators         {Array}
 */
function BlockEditor({ rules, onRulesChange, indicators }) {
  const [direction, setDirection] = useState('long_entry');
  const blocks = Array.isArray(rules?.[direction]) ? rules[direction] : [];

  function updateBlocks(nextBlocks) {
    onRulesChange({ ...rules, [direction]: nextBlocks });
  }

  function handleAddBlock() {
    // Iter-3 + ORDERS guardrail 2: no defaults — a fresh block has a
    // null instrument, weight 0, and NO conditions. User explicitly adds
    // each piece.
    updateBlocks([...blocks, defaultBlock()]);
  }

  function handleRemoveBlock(blockIdx) {
    updateBlocks(blocks.filter((_, i) => i !== blockIdx));
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

  return (
    <div className={styles.blockEditor} data-testid="block-editor">
      <div className={styles.directionTabs} role="tablist">
        {DIRECTIONS.map((dir) => {
          const count = (rules?.[dir] || []).length;
          return (
            <button
              type="button"
              key={dir}
              role="tab"
              aria-selected={direction === dir}
              data-testid={`direction-tab-${dir}`}
              className={`${styles.directionTab} ${direction === dir ? styles.directionTabActive : ''}`}
              onClick={() => setDirection(dir)}
            >
              {DIRECTION_LABELS[dir]}
              {count > 0 && (
                <span className={styles.directionTabCount}> ({count})</span>
              )}
            </button>
          );
        })}
      </div>

      <div className={styles.directionHint}>
        Blocks are <strong>OR</strong>&rsquo;d. Conditions in a block are <strong>AND</strong>&rsquo;d.
      </div>

      <div className={styles.blocksList}>
        {blocks.length === 0 ? (
          <div className={styles.blocksEmpty}>
            No blocks. Add one to express a rule in this direction.
          </div>
        ) : (
          blocks.map((block, blockIdx) => (
            <Block
              key={blockIdx}
              blockIdx={blockIdx}
              block={block}
              direction={direction}
              isFirst={blockIdx === 0}
              onUpdateBlock={(next) => handleUpdateBlock(blockIdx, next)}
              onAddCondition={() => handleAddCondition(blockIdx)}
              onRemoveCondition={(condIdx) => handleRemoveCondition(blockIdx, condIdx)}
              onUpdateCondition={(condIdx, next) => handleUpdateCondition(blockIdx, condIdx, next)}
              onRemoveBlock={() => handleRemoveBlock(blockIdx)}
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
    </div>
  );
}

function Block({
  blockIdx,
  block,
  direction,
  isFirst,
  onUpdateBlock,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onRemoveBlock,
  indicators,
}) {
  const conditions = block.conditions || [];
  const runnable = isBlockRunnable(block);
  return (
    <div className={styles.block} data-testid={`block-${blockIdx}`}>
      {!isFirst && <div className={styles.blockOrLabel}>OR</div>}
      <div className={styles.blockStatusDotRow}>
        <span
          className={`${styles.blockStatusDot} ${runnable ? styles.blockStatusDotOk : styles.blockStatusDotWarn}`}
          title={runnable ? 'Block ready' : 'Block not yet runnable (pick instrument + at least one complete condition)'}
          data-testid={`block-status-${blockIdx}`}
          data-runnable={runnable ? 'true' : 'false'}
          aria-hidden="true"
        />
      </div>
      <BlockHeader
        block={block}
        direction={direction}
        onChange={onUpdateBlock}
        onDelete={onRemoveBlock}
        blockIndex={blockIdx + 1}
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
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} lhs`}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.rhs}
                onChange={(next) => updateOperand('rhs', next)}
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
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} operand`}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.min}
                onChange={(next) => updateOperand('min', next)}
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} min`}
              />
            </div>
            <span className={styles.conditionRangeSep}>..</span>
            <div className={styles.conditionOperandCell}>
              <OperandSlot
                operand={condition.max}
                onChange={(next) => updateOperand('max', next)}
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
                indicators={indicators}
                slotLabel={`cond ${condIdx + 1} operand`}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
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
