import { useState } from 'react';
import OperandPicker from './OperandPicker';
import {
  ALL_OPS,
  OP_LABELS,
  conditionShape,
  defaultCondition,
  migrateCondition,
} from './conditionOps';
import { DIRECTIONS } from './storage';
import styles from './Signals.module.css';

const DIRECTION_LABELS = {
  long_entry: 'Long entry',
  long_exit: 'Long exit',
  short_entry: 'Short entry',
  short_exit: 'Short exit',
};

/**
 * Middle panel — block/condition editor for a single signal.
 *
 * Four direction tabs (long_entry / long_exit / short_entry / short_exit)
 * switch the set of blocks being edited. Within each direction:
 *   - blocks are OR'd together (any block firing ⇒ score > 0),
 *   - conditions inside a block are AND'd (all must fire).
 *
 * Props:
 *   rules             {Object}   {long_entry: Block[], long_exit: Block[], ...}
 *   onRulesChange     {Function} (nextRules) => void
 *   indicators        {Array}    list of saved indicators from the Indicators
 *                                localStorage; rendered in the Indicator tab
 *                                of the operand picker.
 *
 * Block logic is local to this component — mutations always produce a
 * deep-ish clone of the active direction and propagate the full
 * ``rules`` object upward. The parent is responsible for plumbing this
 * into its signal state + storage.
 */
function BlockEditor({ rules, onRulesChange, indicators }) {
  const [direction, setDirection] = useState('long_entry');
  const blocks = Array.isArray(rules?.[direction]) ? rules[direction] : [];

  function updateBlocks(nextBlocks) {
    onRulesChange({ ...rules, [direction]: nextBlocks });
  }

  function handleAddBlock() {
    updateBlocks([...blocks, { conditions: [defaultCondition('gt')] }]);
  }

  function handleRemoveBlock(blockIdx) {
    updateBlocks(blocks.filter((_, i) => i !== blockIdx));
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
        Blocks are <strong>OR</strong>&rsquo;d together. Conditions inside a block
        are <strong>AND</strong>&rsquo;d.
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
              isFirst={blockIdx === 0}
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
  isFirst,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onRemoveBlock,
  indicators,
}) {
  const conditions = block.conditions || [];
  return (
    <div className={styles.block} data-testid={`block-${blockIdx}`}>
      {!isFirst && <div className={styles.blockOrLabel}>OR</div>}
      <div className={styles.blockHeader}>
        <span className={styles.blockLabel}>Block {blockIdx + 1}</span>
        <button
          type="button"
          className={styles.deleteBtn}
          onClick={onRemoveBlock}
          title="Remove block"
          aria-label={`Remove block ${blockIdx + 1}`}
          data-testid={`remove-block-${blockIdx}`}
        >
          ×
        </button>
      </div>
      {conditions.length === 0 ? (
        <div className={styles.blockEmpty}>
          Empty block — this block will never fire. Add a condition.
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
      {/*
        Iter-2: condition renders HORIZONTALLY —
          <operand1>  <op-select>  <operand2>         (binary)
          <operand>   in_range    <min> .. <max>      (range)
          <operand>   rolling_xx  <lookback>          (rolling)
        A single flex row keeps the operator visually centred between its
        operands rather than stacked above them.
      */}
      <div className={styles.conditionRow}>
        <span className={styles.conditionLabel}>Cond {condIdx + 1}</span>
        {shape === 'binary' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.lhs}
                onChange={(next) => updateOperand('lhs', next)}
                indicators={indicators}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.rhs}
                onChange={(next) => updateOperand('rhs', next)}
                indicators={indicators}
              />
            </div>
          </>
        )}
        {shape === 'range' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.operand}
                onChange={(next) => updateOperand('operand', next)}
                indicators={indicators}
              />
            </div>
            <div className={styles.conditionOpCell}>{opSelect}</div>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.min}
                onChange={(next) => updateOperand('min', next)}
                indicators={indicators}
              />
            </div>
            <span className={styles.conditionRangeSep}>..</span>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.max}
                onChange={(next) => updateOperand('max', next)}
                indicators={indicators}
              />
            </div>
          </>
        )}
        {shape === 'rolling' && (
          <>
            <div className={styles.conditionOperandCell}>
              <OperandPicker
                value={condition.operand}
                onChange={(next) => updateOperand('operand', next)}
                indicators={indicators}
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
          onClick={onRemove}
          title="Remove condition"
          aria-label={`Remove condition ${condIdx + 1} of block ${blockIdx + 1}`}
          data-testid={`remove-condition-${blockIdx}-${condIdx}`}
        >
          ×
        </button>
      </div>
    </div>
  );
}

export default BlockEditor;
