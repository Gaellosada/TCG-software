import { useState, useEffect } from 'react';
import { listCollections, listInstruments } from '../../api/data';
import styles from './CategoryBrowser.module.css';

/**
 * Maps collections into display categories.
 * INDEX -> "Indexes", ETF/FOREX/FUND -> "Assets", FUT_* -> "Futures"
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
    collections: [],
    placeholder: 'Continuous rolling coming in Phase 2',
  },
];

function CategoryBrowser({ selected, onSelect }) {
  const [categories, setCategories] = useState([]);
  const [expanded, setExpanded] = useState({ indexes: true, assets: true, futures: false });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        setLoading(true);
        setError(null);

        const collections = await listCollections();

        // Build category data by fetching instruments for non-futures collections
        const result = await Promise.all(
          CATEGORY_CONFIG.map(async (cat) => {
            if (cat.placeholder) {
              return { ...cat, groups: [], isFutures: true };
            }

            // Filter available collections for this category
            const available = cat.collections.filter((c) => collections.includes(c));
            const groups = await Promise.all(
              available.map(async (collName) => {
                const res = await listInstruments(collName);
                return {
                  collection: collName,
                  instruments: res.items.map((item) => ({
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
              {cat.isFutures ? (
                <div className={styles.placeholder}>{cat.placeholder}</div>
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
                          selected?.symbol === inst.symbol ? styles.instrumentActive : ''
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
