import { useState, useEffect } from 'react';
import { listCollections, listInstruments } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import styles from './CategoryBrowser.module.css';

/**
 * Maps collections into display categories.
 * INDEX -> "Indexes", ETF/FOREX/FUND -> "Assets", FUT_* -> "Futures",
 * OPT_* -> "Options"
 */
const CATEGORY_CONFIG = [
  {
    key: 'indexes',
    label: 'Indexes',
    color: 'var(--cat-indexes)',
    collections: ['INDEX'],
  },
  {
    key: 'assets',
    label: 'Assets',
    color: 'var(--cat-assets)',
    collections: ['ETF', 'FOREX', 'FUND'],
  },
  {
    key: 'futures',
    label: 'Futures',
    color: 'var(--cat-futures)',
    dynamicFutures: true,
  },
  {
    key: 'options',
    label: 'Options',
    color: 'var(--cat-options)',
    dynamicOptions: true,
  },
];

function CategoryBrowser({ selected, onSelect }) {
  const [categories, setCategories] = useState([]);
  const [expanded, setExpanded] = useState({ indexes: false, assets: false, futures: false, options: false });
  const [expandedFutGroups, setExpandedFutGroups] = useState({});
  const [contractsExpanded, setContractsExpanded] = useState({});
  const [contractsData, setContractsData] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Auto-collapse futures groups that don't own the current selection
  useEffect(() => {
    if (!selected) {
      setExpandedFutGroups({});
      setContractsExpanded({});
      return;
    }
    const selCollection = selected.collection;
    setExpandedFutGroups((prev) => {
      const next = {};
      for (const key of Object.keys(prev)) {
        next[key] = key === selCollection;
      }
      // Ensure the selected collection's group is open
      if (selCollection) next[selCollection] = true;
      return next;
    });
    setContractsExpanded((prev) => {
      // Only keep "Individual Contracts" open if an individual contract is selected in that group
      const next = {};
      for (const key of Object.keys(prev)) {
        next[key] = selected.type === 'instrument' && key === selCollection;
      }
      return next;
    });
  }, [selected]);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        setLoading(true);
        setError(null);

        const collections = await listCollections();

        const result = await Promise.all(
          CATEGORY_CONFIG.map(async (cat) => {
            if (cat.dynamicFutures) {
              const futCollections = collections.filter((c) => c.startsWith('FUT_'));
              return { ...cat, futCollections, isFutures: true };
            }

            if (cat.dynamicOptions) {
              const resp = await getOptionRoots();
              return { ...cat, optionRoots: resp.roots || [], isOptions: true };
            }

            // Filter available collections for this category
            const available = cat.collections.filter((c) => collections.includes(c));
            const groups = await Promise.all(
              available.map(async (collName) => {
                const res = await listInstruments(collName);
                return {
                  collection: collName,
                  instruments: (res.items || []).map((item) => ({
                    symbol: item.symbol,
                    collection: item.collection || collName,
                  })),
                };
              })
            );

            return { ...cat, groups, isFutures: false };
          })
        );

        if (!cancelled) {
          setCategories(result);
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message);
          setLoading(false);
        }
      }
    }

    load();
    return () => { cancelled = true; };
  }, []);

  function toggleCategory(key) {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function handleFutGroupClick(collName) {
    const wasExpanded = expandedFutGroups[collName];
    if (wasExpanded) {
      // Clicking an already-expanded group collapses it and deselects
      setExpandedFutGroups((prev) => ({ ...prev, [collName]: false }));
      setContractsExpanded((prev) => ({ ...prev, [collName]: false }));
      if (selected?.collection === collName) {
        onSelect(null);
      }
    } else {
      // Expanding a group auto-selects its continuous series
      onSelect({ type: 'continuous', collection: collName });
    }
  }

  async function toggleContracts(collName) {
    const wasExpanded = contractsExpanded[collName];
    setContractsExpanded((prev) => ({ ...prev, [collName]: !wasExpanded }));
    // Lazy-load individual contracts on first expand
    if (!wasExpanded && !contractsData[collName]) {
      try {
        const res = await listInstruments(collName, { skip: 0, limit: 500 });
        setContractsData((prev) => ({ ...prev, [collName]: res.items || [] }));
      } catch {
        setContractsData((prev) => ({ ...prev, [collName]: [] }));
      }
    }
  }

  function isSelected(sel, type, symbol, collection) {
    if (!sel) return false;
    if (type === 'continuous') {
      return sel.type === 'continuous' && sel.collection === collection;
    }
    return sel.symbol === symbol && sel.collection === collection;
  }

  if (loading) {
    return <div className={styles.loading}>Loading instruments...</div>;
  }

  if (error) {
    return <div className={styles.error}>Failed to load: {error}</div>;
  }

  return (
    <div className={styles.browser}>
      {categories.map((cat) => (
        <div key={cat.key} className={styles.category}>
          <button
            className={styles.categoryHeader}
            onClick={() => toggleCategory(cat.key)}
          >
            <span className={styles.categoryBar} style={{ background: cat.color }} />
            <span className={styles.categoryLabel}>{cat.label}</span>
            <span className={styles.chevron}>
              {expanded[cat.key] ? '\u25BE' : '\u25B8'}
            </span>
          </button>

          {expanded[cat.key] && (
            <div className={styles.categoryBody}>
              {cat.isOptions ? (
                !cat.optionRoots || cat.optionRoots.length === 0 ? (
                  <div className={styles.placeholder}>No options roots available</div>
                ) : (
                  cat.optionRoots.map((root) => (
                    <button
                      key={root.collection}
                      className={`${styles.optionRoot} ${
                        selected?.type === 'option' && selected?.collection === root.collection
                          ? styles.optionRootActive
                          : ''
                      }`}
                      onClick={() =>
                        onSelect({
                          type: 'option',
                          collection: root.collection,
                          instrument_id: null,
                          expiry: null,
                          strike: null,
                          optionType: null,
                          // Surface the root's last bar date so DataPage / the
                          // chain hook can default the query date to a value
                          // the data actually covers (today is typically past
                          // the ingestion cutoff and returns zero rows).
                          last_trade_date: root.last_trade_date ?? null,
                          expiration_last: root.expiration_last ?? null,
                        })
                      }
                    >
                      <span className={styles.optionRootName}>{root.name}</span>
                      <span className={styles.optionRootBadges}>
                        {root.has_greeks && (
                          <span className={styles.optionGreeksBadge}>Greeks</span>
                        )}
                        {root.strike_factor_verified === false && (
                          <span
                            className={styles.optionVerificationBadge}
                            title="Strike factor verification pending; some bond/rate option strikes may display at the wrong scale until verified."
                          >
                            Verification pending
                          </span>
                        )}
                      </span>
                    </button>
                  ))
                )
              ) : cat.isFutures ? (
                !cat.futCollections || cat.futCollections.length === 0 ? (
                  <div className={styles.placeholder}>No futures collections available</div>
                ) : (
                  cat.futCollections.map((collName) => (
                    <div key={collName} className={styles.group}>
                      <button
                        className={`${styles.futGroupHeader} ${
                          isSelected(selected, 'continuous', null, collName)
                            ? styles.futGroupActive
                            : ''
                        }`}
                        onClick={() => handleFutGroupClick(collName)}
                      >
                        <span className={styles.futGroupChevron}>
                          {expandedFutGroups[collName] ? '\u25BE' : '\u25B8'}
                        </span>
                        <span className={styles.groupLabel} style={{ padding: 0 }}>
                          {collName}
                        </span>
                      </button>

                      {expandedFutGroups[collName] && (
                        <div>
                          {/* Continuous Series entry */}
                          <button
                            className={`${styles.instrument} ${styles.continuousEntry} ${
                              isSelected(selected, 'continuous', null, collName)
                                ? styles.instrumentActive
                                : ''
                            }`}
                            onClick={() => onSelect({ type: 'continuous', collection: collName })}
                          >
                            <span className={styles.instrumentSymbol}>
                              Continuous Series
                            </span>
                          </button>

                          {/* Individual Contracts sub-section (lazy-loaded) */}
                          <button
                            className={styles.contractsToggle}
                            onClick={() => toggleContracts(collName)}
                          >
                            <span className={styles.futGroupChevron}>
                              {contractsExpanded[collName] ? '\u25BE' : '\u25B8'}
                            </span>
                            <span className={styles.contractsLabel}>
                              Individual Contracts
                            </span>
                          </button>

                          {contractsExpanded[collName] && (
                            <div className={styles.contractsList}>
                              {!contractsData[collName] ? (
                                <div className={styles.placeholder}>Loading contracts...</div>
                              ) : contractsData[collName].length === 0 ? (
                                <div className={styles.placeholder}>No contracts found</div>
                              ) : (
                                contractsData[collName].map((inst) => (
                                  <button
                                    key={inst.symbol}
                                    className={`${styles.instrument} ${
                                      isSelected(selected, 'instrument', inst.symbol, collName)
                                        ? styles.instrumentActive
                                        : ''
                                    }`}
                                    onClick={() => onSelect({ type: 'instrument', symbol: inst.symbol, collection: collName })}
                                  >
                                    <span className={styles.instrumentSymbol}>{inst.symbol}</span>
                                  </button>
                                ))
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  ))
                )
              ) : (
                cat.groups.map((group) => (
                  <div key={group.collection} className={styles.group}>
                    {cat.groups.length > 1 && (
                      <div className={styles.groupLabel}>{group.collection}</div>
                    )}
                    {group.instruments.map((inst) => (
                      <button
                        key={inst.symbol}
                        className={`${styles.instrument} ${
                          isSelected(selected, 'instrument', inst.symbol, group.collection)
                            ? styles.instrumentActive
                            : ''
                        }`}
                        onClick={() => onSelect({ symbol: inst.symbol, collection: group.collection })}
                      >
                        <span className={styles.instrumentSymbol}>{inst.symbol}</span>
                      </button>
                    ))}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export default CategoryBrowser;
