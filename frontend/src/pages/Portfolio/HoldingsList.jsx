import { useState, useEffect } from 'react';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import styles from './HoldingsList.module.css';

/**
 * Fetches available providers for a leg and renders a dropdown (or static text).
 */
function ProviderSelect({ leg, onChange }) {
  const [providers, setProviders] = useState([]);
  const [fetched, setFetched] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function fetchProviders() {
      try {
        let data;
        if (leg.type === 'continuous') {
          data = await getContinuousSeries(leg.collection, {
            strategy: leg.strategy || 'front_month',
            adjustment: leg.adjustment || 'none',
          });
        } else {
          data = await getInstrumentPrices(leg.collection, leg.symbol);
        }
        if (!cancelled) {
          setProviders(data.available_providers || []);
          setFetched(true);
        }
      } catch {
        if (!cancelled) setFetched(true);
      }
    }
    fetchProviders();
    return () => { cancelled = true; };
  }, [leg.collection, leg.symbol, leg.type, leg.strategy, leg.adjustment]);

  if (!fetched) {
    return <span className={styles.monoCell}>...</span>;
  }

  if (providers.length <= 1) {
    return <span className={styles.monoCell}>{providers[0] || 'Auto'}</span>;
  }

  return (
    <select
      className={styles.providerSelect}
      value={leg.provider || ''}
      onChange={(e) => onChange(e.target.value || null)}
      aria-label={`Provider for ${leg.label}`}
    >
      <option value="">Auto</option>
      {providers.map((p) => (
        <option key={p} value={p}>{p}</option>
      ))}
    </select>
  );
}

/**
 * Displays portfolio holdings with editable weights and remove buttons.
 * Purely presentational — state is managed by usePortfolio.
 */
export default function HoldingsList({ legs, onUpdateLeg, onRemoveLeg, onOpenAddModal }) {
  return (
    <div className={styles.section}>
      <div className={styles.header}>
        <span className={styles.label}>Holdings</span>
        <button
          className={styles.addBtn}
          type="button"
          onClick={onOpenAddModal}
          aria-label="Add holding"
        >
          + Add Holding
        </button>
      </div>

      {legs.length === 0 ? (
        <div className={styles.empty}>
          No instruments added. Click &quot;+ Add Holding&quot; to build your portfolio.
        </div>
      ) : (
        <div className={styles.tableWrapper}>
          <table className={styles.table} aria-label="Portfolio holdings">
            <thead>
              <tr>
                <th className={styles.thLabel}>Label</th>
                <th className={styles.thType}>Type</th>
                <th className={styles.thCollection}>Collection</th>
                <th className={styles.thInstrument}>Instrument</th>
                <th className={styles.thProvider}>Provider</th>
                <th className={styles.thWeight}>Weight</th>
                <th className={styles.thActions} aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {legs.map((leg, index) => (
                <tr key={leg.id}>
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
                      {leg.type === 'continuous' ? 'Continuous' : 'Instrument'}
                    </span>
                  </td>
                  <td className={styles.monoCell}>{leg.collection}</td>
                  <td className={styles.monoCell}>
                    {leg.type === 'continuous'
                      ? `${leg.strategy || 'front_month'}`
                      : leg.symbol}
                  </td>
                  <td>
                    <ProviderSelect
                      leg={leg}
                      onChange={(provider) => onUpdateLeg(index, { provider })}
                    />
                  </td>
                  <td>
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
                  </td>
                  <td>
                    <button
                      className={styles.removeBtn}
                      type="button"
                      onClick={() => {
                        if (window.confirm(`Remove "${leg.label}" from portfolio?`)) {
                          onRemoveLeg(index);
                        }
                      }}
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
              ))}
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
            <span className={styles.weightSummaryValue}>{totalAbs.toFixed(1)}</span>
            <span className={styles.weightSummaryHint}>
              (sum of |weights|; negative = short position)
            </span>
          </div>
        );
      })()}
    </div>
  );
}
