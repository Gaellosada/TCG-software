import { Fragment, useState } from 'react';
import Card from '../../components/Card';
import ConfirmDialog from '../../components/ConfirmDialog';
import { formatInstrument } from './formatInstrument';
import styles from './HoldingsList.module.css';

const COL_COUNT = 7;

/**
 * Displays portfolio holdings with editable weights and remove buttons.
 * Purely presentational — state is managed by usePortfolio.
 *
 * Signal legs show an expandable detail row with their input instruments.
 */
export default function HoldingsList({ legs, legDateRanges, onUpdateLeg, onRemoveLeg, onOpenAddModal, onOpenSignalModal }) {
  const [pendingRemove, setPendingRemove] = useState(null);
  const [expandedSignals, setExpandedSignals] = useState(new Set());

  const toggleExpand = (legId) => {
    setExpandedSignals((prev) => {
      const next = new Set(prev);
      if (next.has(legId)) next.delete(legId);
      else next.add(legId);
      return next;
    });
  };

  return (
    <Card
      title="Holdings"
      right={
        <div className={styles.headerButtons}>
          <button
            className={styles.addBtn}
            type="button"
            onClick={onOpenAddModal}
            aria-label="Add holding"
          >
            + Add Holding
          </button>
          <button
            className={`${styles.addBtn} ${styles.addSignalBtn}`}
            type="button"
            onClick={onOpenSignalModal}
            aria-label="Add signal"
          >
            + Add Signal
          </button>
        </div>
      }
    >
      {legs.length === 0 ? (
        <div className={styles.empty}>
          No instruments added. Click &quot;+ Add Holding&quot; or &quot;+ Add Signal&quot; to build your portfolio.
        </div>
      ) : (
        <div className={styles.tableWrapper}>
          <table className={styles.table} aria-label="Portfolio holdings">
            <thead>
              <tr>
                <th className={styles.thLabel}>Label</th>
                <th className={styles.thType}>Type</th>
                <th className={styles.thInstrument}>Instrument</th>
                <th className={styles.thRange}>Start Date</th>
                <th className={styles.thRange}>End Date</th>
                <th className={styles.thWeight}>Weight</th>
                <th className={styles.thActions} aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {legs.map((leg, index) => {
                const isSignal = leg.type === 'signal';
                const isExpanded = isSignal && expandedSignals.has(leg.id);
                const inputs = isSignal ? (leg.signalSpec?.inputs || []) : [];

                return (
                  <Fragment key={leg.id}>
                    <tr>
                      <td>
                        <input
                          className={styles.labelInput}
                          type="text"
                          value={leg.label}
                          onChange={(e) => onUpdateLeg(index, { label: e.target.value })}
                          spellCheck={false}
                          aria-label={`Label for ${leg.label}`}
                        />
                      </td>
                      <td>
                        <span className={styles.typeBadge} data-type={leg.type}>
                          {isSignal
                            ? 'Signal'
                            : leg.type === 'continuous'
                              ? 'Continuous'
                              : 'Instrument'}
                        </span>
                      </td>
                      <td className={styles.monoCell}>
                        {isSignal ? (
                          <button
                            className={styles.expandBtn}
                            type="button"
                            onClick={() => toggleExpand(leg.id)}
                            aria-expanded={isExpanded}
                            aria-label={`${isExpanded ? 'Collapse' : 'Expand'} inputs for ${leg.label}`}
                          >
                            <span className={styles.expandArrow} data-open={isExpanded}>&#9656;</span>
                            {leg.signalName || '\u2014'}
                            <span className={styles.inputCount}>
                              {inputs.length} input{inputs.length !== 1 ? 's' : ''}
                            </span>
                          </button>
                        ) : leg.type === 'continuous' ? (
                          <span>
                            <span className={styles.instrumentPrimary}>{leg.collection}</span>
                            <span className={styles.instrumentSecondary}>{leg.strategy || 'front_month'}</span>
                          </span>
                        ) : (
                          <span>
                            <span className={styles.instrumentPrimary}>{leg.symbol}</span>
                            <span className={styles.instrumentSecondary}>{leg.collection}</span>
                          </span>
                        )}
                      </td>
                      <td className={styles.rangeCell}>
                        {legDateRanges?.[leg.id]?.start || '\u2014'}
                      </td>
                      <td className={styles.rangeCell}>
                        {legDateRanges?.[leg.id]?.end || '\u2014'}
                      </td>
                      <td>
                        <div className={styles.weightInputWrap}>
                          <input
                            className={styles.weightInput}
                            type="number"
                            min="-100"
                            max="100"
                            step="0.1"
                            value={leg.weight}
                            onChange={(e) =>
                              onUpdateLeg(index, {
                                weight: e.target.value === '' ? '' : Number(e.target.value),
                              })
                            }
                            aria-label={`Weight for ${leg.label}`}
                          />
                          <span className={styles.weightSuffix}>%</span>
                        </div>
                      </td>
                      <td>
                        <button
                          className={styles.removeBtn}
                          type="button"
                          onClick={() => setPendingRemove({ index, label: leg.label })}
                          title={`Remove ${leg.label}`}
                          aria-label={`Remove ${leg.label}`}
                        >
                          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" width="14" height="14" aria-hidden="true">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                          </svg>
                        </button>
                      </td>
                    </tr>
                    {isExpanded && inputs.length > 0 && (
                      <tr className={styles.detailRow}>
                        <td colSpan={COL_COUNT} className={styles.detailCell}>
                          <div className={styles.inputsDetail}>
                            {inputs.map((inp) => (
                              <div key={inp.id} className={styles.inputDetailRow}>
                                <span className={styles.inputDetailId}>{inp.id}</span>
                                <span className={styles.inputDetailInstrument}>
                                  {formatInstrument(inp.instrument, 'Not configured')}
                                </span>
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {legs.length > 0 && (() => {
        const totalAbs = legs.reduce((sum, l) => sum + Math.abs(Number(l.weight || 0)), 0);
        return (
          <div className={styles.weightSummary}>
            <span className={styles.weightSummaryLabel}>
              Total absolute weight:
            </span>
            <span className={styles.weightSummaryValue}>{totalAbs.toFixed(1)}%</span>
            <span className={styles.weightSummaryHint}>
              (sum of |weights|; negative = short position)
            </span>
          </div>
        );
      })()}

      <ConfirmDialog
        open={pendingRemove !== null}
        title="Remove holding?"
        message={
          pendingRemove
            ? `"${pendingRemove.label}" will be removed from this portfolio.`
            : ''
        }
        confirmLabel="Remove"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => {
          const pending = pendingRemove;
          setPendingRemove(null);
          if (pending) onRemoveLeg(pending.index);
        }}
        onCancel={() => setPendingRemove(null)}
      />
    </Card>
  );
}
