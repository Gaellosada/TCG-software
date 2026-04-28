import { useState, useEffect, useMemo } from 'react';
import CategoryBrowser from './CategoryBrowser';
import PriceChart from './PriceChart';
import ContinuousChart from './ContinuousChart';
import OptionChainTable from './OptionChainTable';
import ContractDetailPanel from './ContractDetailPanel';
import ChainSnapshotPanel from './ChainSnapshotPanel';
import { useOptionExpirations } from './useOptionExpirations';
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
  { key: 'chain', label: 'Contracts' },
  { key: 'continuous', label: 'Continuous' },
  { key: 'snapshot', label: 'Smile' },
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
  // snapshot tab — single expiration date input
  const [optionsExpiration, setOptionsExpiration] = useState('');
  // Smile cycle filter — null means "All cycles" (no filter).
  // The list of available cycles and the auto-selected default are
  // populated client-side from each smile response (a SmilePoint now
  // carries `expiration_cycle`).
  const [optionsCycle, setOptionsCycle] = useState(null);
  const [availableCycles, setAvailableCycles] = useState([]);

  // Distinct expirations for the picked option root — drives the Smile
  // tab's expiration <select> so users can only pick days that actually
  // have contracts. Null root → empty list.
  const optionRoot =
    selected && selected.type === 'option' ? selected.collection : null;
  const { expirations: rootExpirations, loading: rootExpirationsLoading } =
    useOptionExpirations(optionRoot);

  // Latest expiration on top of the dropdown.
  const rootExpirationOptions = useMemo(
    () => [...rootExpirations].reverse(),
    [rootExpirations],
  );

  // Default the Smile expiration to the LATEST available date once the
  // list loads (or when the user picks a different root). User can change
  // it via the dropdown.
  useEffect(() => {
    if (rootExpirations.length === 0) return;
    if (
      !optionsExpiration ||
      !rootExpirations.includes(optionsExpiration)
    ) {
      setOptionsExpiration(rootExpirations[rootExpirations.length - 1]);
    }
  }, [rootExpirations, optionsExpiration]);

  // Reset selectedContract and view whenever the user picks a different
  // options root. Re-anchor the date controls on the root's last trade
  // date so the Snapshot tab defaults to a date with data.
  useEffect(() => {
    setSelectedContract(null);
    setOptionsView('chain');
    setOptionsExpiration('');
    setOptionsCycle(null);
    setAvailableCycles([]);
    if (selected?.type === 'option' && selected.last_trade_date) {
      setOptionsDate(selected.last_trade_date);
    }
  }, [selected?.collection]);

  // Whenever the smile re-keys (different expiration / type / date) the
  // available cycles may differ — reset and let the next response
  // repopulate them. The cycle filter is intentionally cleared here so
  // the first fetch returns the full population, from which we
  // auto-select the most-populated cycle.
  useEffect(() => {
    setOptionsCycle(null);
    setAvailableCycles([]);
  }, [optionsExpiration, optionsType, optionsDate]);

  // Receive the raw ChainSnapshotResponse from the panel and extract the
  // distinct cycles present in the points. Auto-select the most-populated
  // cycle the first time we see >1 cycle in a response — that yields
  // one trace per strike on roots like OPT_SP_500 by default.
  function handleSnapshotData(response) {
    if (!response || !Array.isArray(response.series)) return;
    const counts = new Map();
    for (const s of response.series) {
      if (!s || !Array.isArray(s.points)) continue;
      for (const p of s.points) {
        const c = p && typeof p.expiration_cycle === 'string' ? p.expiration_cycle : '';
        counts.set(c, (counts.get(c) || 0) + 1);
      }
    }
    const cycles = [...counts.keys()].filter((c) => c !== '');
    cycles.sort();
    setAvailableCycles((prev) => {
      // Only update if the set differs — avoids tearing down the
      // dropdown when the user toggles the field.
      if (prev.length === cycles.length && prev.every((c, i) => c === cycles[i])) {
        return prev;
      }
      return cycles;
    });
    // Auto-select the most-populated cycle on the first response that
    // surfaces cycle metadata. We pick "first response" as: optionsCycle
    // is currently null AND we found at least one cycle.
    setOptionsCycle((current) => {
      if (current !== null) return current;
      if (cycles.length === 0) return null;
      let best = cycles[0];
      let bestCount = counts.get(best) || 0;
      for (const c of cycles) {
        const n = counts.get(c) || 0;
        if (n > bestCount) {
          best = c;
          bestCount = n;
        }
      }
      return best;
    });
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
                {/* Keyed by collection so switching roots remounts the
                    component: old chain rows wiped immediately, new
                    initialFilters applied (last_trade_date differs per
                    root), and the loading state is visible from the first
                    render — no stale rows from the previous root. */}
                <OptionChainTable
                  key={selected.collection}
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
                    Cycle
                    <select
                      className={styles.filterInput}
                      value={optionsCycle ?? ''}
                      onChange={(e) => {
                        const v = e.target.value;
                        setOptionsCycle(v === '' ? null : v);
                      }}
                    >
                      <option value="">All cycles</option>
                      {availableCycles.map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                  </label>
                  <label className={styles.filterLabel}>
                    Expiration
                    <select
                      className={styles.filterInput}
                      value={
                        optionsExpiration && rootExpirations.includes(optionsExpiration)
                          ? optionsExpiration
                          : ''
                      }
                      onChange={(e) => setOptionsExpiration(e.target.value)}
                      disabled={rootExpirationsLoading || rootExpirations.length === 0}
                    >
                      <option value="">
                        {rootExpirationsLoading ? 'Loading…' : 'Pick an expiration'}
                      </option>
                      {rootExpirationOptions.map((exp) => (
                        <option key={exp} value={exp}>{exp}</option>
                      ))}
                    </select>
                  </label>
                </div>
                {optionsExpiration.trim() && rootExpirations.includes(optionsExpiration.trim()) ? (
                  <ChainSnapshotPanel
                    root={selected.collection}
                    date={optionsDate}
                    type={optionsType}
                    expiration={optionsExpiration.trim()}
                    expiration_cycle={optionsCycle}
                    onSnapshotData={handleSnapshotData}
                    onClose={() => setOptionsView('chain')}
                  />
                ) : (
                  <div className={styles.snapshotEmpty} data-testid="snapshot-empty">
                    {rootExpirationsLoading
                      ? 'Loading available expirations…'
                      : rootExpirations.length === 0
                      ? 'No expirations available for this root.'
                      : 'Pick an expiration above to load the smile.'}
                  </div>
                )}
              </div>
            )}

            {/* -------- Continuous tab (placeholder — coming soon) -------- */}
            {optionsView === 'continuous' && (
              <div className={styles.snapshotView}>
                <div className={styles.snapshotEmpty} data-testid="continuous-empty">
                  <div>
                    <strong>Continuous options series — coming soon</strong>
                    <p style={{ marginTop: 8, fontStyle: 'normal', maxWidth: 480 }}>
                      Stitch successive expirations into a single continuous
                      price series by rolling at chosen criteria (DTE,
                      |Δ| target, or calendar) — same idea as the futures
                      continuous chart, applied to options.
                    </p>
                  </div>
                </div>
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
