import { useState, useEffect } from 'react';
import CategoryBrowser from './CategoryBrowser';
import PriceChart from './PriceChart';
import ContinuousChart from './ContinuousChart';
import OptionChainTable from './OptionChainTable';
import ContractDetailPanel from './ContractDetailPanel';
import ChainSnapshotPanel from './ChainSnapshotPanel';
import MultiExpirationSmilePanel from './MultiExpirationSmilePanel';
import styles from './DataPage.module.css';

/**
 * Returns today as YYYY-MM-DD in local time.
 */
function todayISO() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

const OPTIONS_TABS = [
  { key: 'chain', label: 'Chain' },
  { key: 'snapshot', label: 'Smile' },
  { key: 'multi', label: 'Multi-smile' },
];

function DataPage() {
  const [selected, setSelected] = useState(null);
  const [selectedContract, setSelectedContract] = useState(null);

  // ---------------------------------------------------------------------------
  // Tier 2 view state — owned here, passed to snapshot / multi panels.
  // Phase-1 ergonomics: users type ISO date strings into text inputs.
  // ---------------------------------------------------------------------------
  const [optionsView, setOptionsView] = useState('chain');
  const [optionsDate, setOptionsDate] = useState(todayISO);
  const [optionsType, setOptionsType] = useState('C');
  // snapshot tab — single expiration text input
  const [optionsExpiration, setOptionsExpiration] = useState('');
  // multi-smile tab — list of up to 8 expiration strings; user types one at a
  // time into a staging input, then clicks "Add".
  const [optionsExpirations, setOptionsExpirations] = useState([]);
  const [multiExpirationInput, setMultiExpirationInput] = useState('');

  // Reset selectedContract and view whenever the user picks a different
  // options root. Re-anchor the date controls on the root's last trade
  // date so Snapshot / Multi-smile tabs default to a date with data.
  useEffect(() => {
    setSelectedContract(null);
    setOptionsView('chain');
    setOptionsExpirations([]);
    setMultiExpirationInput('');
    setOptionsExpiration('');
    if (selected?.type === 'option' && selected.last_trade_date) {
      setOptionsDate(selected.last_trade_date);
    }
  }, [selected?.collection]);

  // ---------------------------------------------------------------------------
  // Multi-smile expiration helpers
  // ---------------------------------------------------------------------------
  function addMultiExpiration() {
    const val = multiExpirationInput.trim();
    if (!val) return;
    if (optionsExpirations.length >= 8) return;
    if (optionsExpirations.includes(val)) return;
    setOptionsExpirations((prev) => [...prev, val]);
    setMultiExpirationInput('');
  }

  function removeMultiExpiration(exp) {
    setOptionsExpirations((prev) => prev.filter((e) => e !== exp));
  }

  function renderRight() {
    if (!selected) {
      return (
        <div className={styles.welcome}>
          <div className={styles.welcomeInner}>
            <h2>Select an instrument</h2>
            <p>Pick an instrument from the categories on the left to view its price history.</p>
          </div>
        </div>
      );
    }

    if (selected.type === 'option') {
      // No bar data for this root — surface loudly and stop. This is a
      // production / VPN problem, not a UI fallback decision.
      if (!selected.last_trade_date) {
        return (
          <div className={styles.welcome}>
            <div className={styles.welcomeInner}>
              <h2>{selected.collection}: no data available</h2>
              <p>
                The backend reports no recent bars for this root. Check
                that the ingestion pipeline is current; the API
                <code> /api/options/roots </code>
                response carries <code>last_trade_date: null</code> for
                this collection.
              </p>
            </div>
          </div>
        );
      }
      return (
        <div className={styles.optionsWrapper}>
          {/* shared initialFilters anchored on the root's last trade date */}
          {/* Tab strip */}
          <div className={styles.optionsTabs} role="tablist">
            {OPTIONS_TABS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={optionsView === key}
                className={`${styles.optionsTab}${optionsView === key ? ` ${styles.optionsTabActive}` : ''}`}
                onClick={() => setOptionsView(key)}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Tab body */}
          <div className={styles.optionsTabBody}>
            {/* -------- Chain tab (default) -------- */}
            {optionsView === 'chain' && (
              <div className={styles.optionsContainer}>
                <OptionChainTable
                  root={selected.collection}
                  onRowClick={(contract) => setSelectedContract(contract)}
                  initialFilters={{
                    date: selected.last_trade_date,
                    expirationMin: selected.last_trade_date,
                  }}
                />
                {selectedContract && (
                  <ContractDetailPanel
                    collection={selectedContract.collection}
                    instrumentId={selectedContract.instrument_id}
                    onClose={() => setSelectedContract(null)}
                  />
                )}
              </div>
            )}

            {/* -------- Snapshot tab -------- */}
            {optionsView === 'snapshot' && (
              <div className={styles.snapshotView}>
                <div className={styles.snapshotFilters}>
                  <label className={styles.filterLabel}>
                    Date
                    <input
                      type="date"
                      className={styles.filterInput}
                      value={optionsDate}
                      onChange={(e) => setOptionsDate(e.target.value)}
                    />
                  </label>
                  <label className={styles.filterLabel}>
                    Type
                    <select
                      className={styles.filterInput}
                      value={optionsType}
                      onChange={(e) => setOptionsType(e.target.value)}
                    >
                      <option value="C">Calls</option>
                      <option value="P">Puts</option>
                    </select>
                  </label>
                  <label className={styles.filterLabel}>
                    Expiration (YYYY-MM-DD)
                    <input
                      type="text"
                      className={styles.filterInput}
                      placeholder="e.g. 2024-12-20"
                      value={optionsExpiration}
                      onChange={(e) => setOptionsExpiration(e.target.value)}
                    />
                  </label>
                </div>
                {optionsExpiration.trim() ? (
                  <ChainSnapshotPanel
                    root={selected.collection}
                    date={optionsDate}
                    type={optionsType}
                    expiration={optionsExpiration.trim()}
                    onClose={() => setOptionsView('chain')}
                  />
                ) : (
                  <div className={styles.snapshotEmpty} data-testid="snapshot-empty">
                    Enter an expiration date above (YYYY-MM-DD) to load the smile.
                  </div>
                )}
              </div>
            )}

            {/* -------- Multi-smile tab -------- */}
            {optionsView === 'multi' && (
              <div className={styles.snapshotView}>
                <div className={styles.snapshotFilters}>
                  <label className={styles.filterLabel}>
                    Date
                    <input
                      type="date"
                      className={styles.filterInput}
                      value={optionsDate}
                      onChange={(e) => setOptionsDate(e.target.value)}
                    />
                  </label>
                  <label className={styles.filterLabel}>
                    Type
                    <select
                      className={styles.filterInput}
                      value={optionsType}
                      onChange={(e) => setOptionsType(e.target.value)}
                    >
                      <option value="C">Calls</option>
                      <option value="P">Puts</option>
                    </select>
                  </label>
                  <div className={styles.filterLabel}>
                    Add expiration (YYYY-MM-DD)
                    <div className={styles.expirationInputRow}>
                      <input
                        type="text"
                        className={styles.filterInput}
                        placeholder="e.g. 2024-12-20"
                        value={multiExpirationInput}
                        onChange={(e) => setMultiExpirationInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') addMultiExpiration();
                        }}
                      />
                      <button
                        type="button"
                        className={styles.addButton}
                        onClick={addMultiExpiration}
                        disabled={optionsExpirations.length >= 8}
                      >
                        Add
                      </button>
                    </div>
                    {optionsExpirations.length > 0 && (
                      <ul className={styles.expirationList}>
                        {optionsExpirations.map((exp) => (
                          <li key={exp} className={styles.expirationTag}>
                            {exp}
                            <button
                              type="button"
                              className={styles.removeButton}
                              onClick={() => removeMultiExpiration(exp)}
                              aria-label={`Remove ${exp}`}
                            >
                              ×
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
                {optionsExpirations.length > 0 ? (
                  <MultiExpirationSmilePanel
                    root={selected.collection}
                    date={optionsDate}
                    type={optionsType}
                    expirations={optionsExpirations}
                    onClose={() => setOptionsView('chain')}
                  />
                ) : (
                  <div className={styles.snapshotEmpty} data-testid="multi-empty">
                    Add at least one expiration date above to load the smile.
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      );
    }

    if (selected.type === 'continuous') {
      return <ContinuousChart collection={selected.collection} />;
    }

    return (
      <PriceChart
        collection={selected.collection}
        instrument={selected.symbol}
      />
    );
  }

  return (
    <div className={styles.page}>
      <div className={styles.leftPanel}>
        <CategoryBrowser
          selected={selected}
          onSelect={setSelected}
        />
      </div>
      <div className={styles.rightPanel}>
        {renderRight()}
      </div>
    </div>
  );
}

export default DataPage;
