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

  // Reset selectedContract and view whenever the user picks a different options root.
  useEffect(() => {
    setSelectedContract(null);
    setOptionsView('chain');
    setOptionsExpirations([]);
    setMultiExpirationInput('');
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
      return (
        <div className={styles.optionsWrapper}>
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
                <ChainSnapshotPanel
                  root={selected.collection}
                  date={optionsDate}
                  type={optionsType}
                  expiration={optionsExpiration}
                  onClose={() => setOptionsView('chain')}
                />
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
                <MultiExpirationSmilePanel
                  root={selected.collection}
                  date={optionsDate}
                  type={optionsType}
                  expirations={optionsExpirations}
                  onClose={() => setOptionsView('chain')}
                />
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
