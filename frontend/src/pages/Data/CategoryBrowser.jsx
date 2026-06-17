import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { listCollections, listInstruments } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import { queryKeys } from '../../queryKeys';
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

/**
 * Render the greeks-availability badge for an option root:
 * - stored_greeks_ratio >= 0.9   → solid green "Greeks"
 *                                  (vendor reliably ships them; e.g. OPT_SP_500)
 * - 0.1 <= ratio < 0.9           → split green/gray "Greeks"
 *                                  (vendor ships them for a fraction of docs;
 *                                  e.g. OPT_BTC ~37%, OPT_JPYUSD ~30%)
 * - ratio < 0.1 && has_computed_greeks → gray "Comp. Greeks"
 *                                  (vendor stocks ~none but engine can compute;
 *                                  e.g. OPT_VIX via Black-76 + FUT_VIX forward)
 * - otherwise                    → no badge (e.g. OPT_ETH — no greeks anywhere)
 *
 * Falls back to the legacy ``has_greeks`` flag when the new fields are absent
 * (older response shape or tests that don't populate them).
 */
function renderGreeksBadge(root) {
  const ratio = typeof root.stored_greeks_ratio === 'number'
    ? root.stored_greeks_ratio
    : (root.has_greeks ? 1 : 0);
  const canCompute = root.has_computed_greeks ?? false;

  if (ratio >= 0.9) {
    return <span className={styles.greeksBadge}>Greeks</span>;
  }
  if (ratio >= 0.1) {
    return <span className={styles.greeksBadgePartial}>Greeks</span>;
  }
  if (canCompute) {
    return <span className={styles.greeksBadgeComputed}>Comp. Greeks</span>;
  }
  return null;
}

/**
 * Composite sidebar loader: collections + per-collection instruments +
 * option roots, fanned out exactly as the old useEffect did. Pure data
 * orchestration so it can back a single TanStack query (cached → instant
 * re-render on navigation, silent background revalidate). The ``signal`` is
 * threaded into every api call so a superseded load is cancelled.
 */
export async function loadCategories(signal) {
  const collections = await listCollections(null, { signal });

  return Promise.all(
    CATEGORY_CONFIG.map(async (cat) => {
      if (cat.dynamicFutures) {
        const futCollections = collections.filter((c) => c.startsWith('FUT_'));
        return { ...cat, futCollections, isFutures: true };
      }

      if (cat.dynamicOptions) {
        const resp = await getOptionRoots({ signal });
        return { ...cat, optionRoots: resp.roots || [], isOptions: true };
      }

      // Filter available collections for this category
      const available = cat.collections.filter((c) => collections.includes(c));
      const groups = await Promise.all(
        available.map(async (collName) => {
          const res = await listInstruments(collName, { signal });
          return {
            collection: collName,
            instruments: (res.items || []).map((item) => ({
              symbol: item.symbol,
              collection: item.collection || collName,
            })),
          };
        }),
      );

      return { ...cat, groups, isFutures: false };
    }),
  );
}

function CategoryBrowser({ selected, onSelect }) {
  const [expanded, setExpanded] = useState({ indexes: false, assets: false, futures: false, options: false });
  const [expandedFutGroups, setExpandedFutGroups] = useState({});
  const [contractsExpanded, setContractsExpanded] = useState({});
  const [contractsData, setContractsData] = useState({});

  // SWR composite query: on first mount it loads (shows "Loading instruments…"
  // once); on every re-navigation the cached category tree renders INSTANTLY
  // with no loading flash, and a stale entry silently revalidates in the
  // background. ``categories`` defaults to [] to match the old initial state.
  const {
    data: categories = [],
    isPending,
    fetchStatus,
    error: queryError,
  } = useQuery({
    queryKey: queryKeys.market.categoryBrowser(),
    queryFn: ({ signal }) => loadCategories(signal),
  });
  // Only the first-ever load (no cache) shows the spinner — a background
  // revalidate must not (that is the no-spinner-on-navigation guarantee).
  const loading = isPending && fetchStatus !== 'idle';
  // Preserve the old string-message error shape (render used ``error`` directly).
  const error = queryError ? (queryError.message || String(queryError)) : null;

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
                      className={`${styles.instrument} ${
                        selected?.type === 'option' && selected?.collection === root.collection
                          ? styles.instrumentActive
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
                      <span className={styles.instrumentSymbol}>{root.name}</span>
                      <span className={styles.badges}>
                        {renderGreeksBadge(root)}
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

/**
 * Warm the CategoryBrowser composite cache entry ahead of first navigation.
 *
 * Called once on app startup so the FIRST visit to /data renders the sidebar
 * instantly (no loading flash) — the data is already cached. Uses the EXACT
 * same query key + queryFn as the component's ``useQuery`` so the warmed entry
 * is the one CategoryBrowser reads on mount.
 *
 * ``prefetchQuery`` is fire-and-forget and swallows errors internally, so a
 * backend hiccup at startup simply leaves the cache cold — the component then
 * loads normally (its own error/loading handling stands). Returns the promise
 * for callers/tests that want to await completion.
 */
export function prefetchCategoryBrowser(queryClient) {
  return queryClient.prefetchQuery({
    queryKey: queryKeys.market.categoryBrowser(),
    queryFn: ({ signal }) => loadCategories(signal),
  });
}

export default CategoryBrowser;
