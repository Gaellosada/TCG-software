import { useState, useMemo } from 'react';
import { useObjectsV2 } from '../../hooks/marketQueries';
import styles from '../Data/CategoryBrowser.module.css';

/**
 * Kind → display group config. Order follows the ORDERS spec
 * (rate / index / future / option). Colours reuse the v1 category CSS vars so
 * the two browsers read as one design system.
 */
const KIND_GROUPS = [
  { kind: 'rate',   label: 'Rates',   color: 'var(--cat-assets)' },
  { kind: 'index',  label: 'Indexes', color: 'var(--cat-indexes)' },
  { kind: 'future', label: 'Futures', color: 'var(--cat-futures)' },
  { kind: 'option', label: 'Options', color: 'var(--cat-options)' },
];

/**
 * v2 object browser (left panel) — lists objects grouped by ``kind``.
 * Mirrors the visual structure of the v1 CategoryBrowser (collapsible category
 * headers with a coloured bar + object rows), but the grouping key is the
 * schema's ``object.kind`` rather than a collection category.
 */
function ObjectBrowser({ selected, onSelect }) {
  const { data: objects, loading, error } = useObjectsV2();

  // Groups start expanded so the (small) live catalogue is visible at a glance.
  const [expanded, setExpanded] = useState({
    rate: true, index: true, future: true, option: true,
  });

  const grouped = useMemo(() => {
    const byKind = { rate: [], index: [], future: [], option: [] };
    for (const obj of objects || []) {
      if (byKind[obj.kind]) byKind[obj.kind].push(obj);
      // Unknown kinds are ignored on purpose — the schema CHECK constrains
      // kind to the four known values; anything else is a data anomaly.
    }
    for (const k of Object.keys(byKind)) {
      byKind[k].sort((a, b) => (a.symbol || '').localeCompare(b.symbol || ''));
    }
    return byKind;
  }, [objects]);

  function toggle(kind) {
    setExpanded((prev) => ({ ...prev, [kind]: !prev[kind] }));
  }

  if (loading) {
    return <div className={styles.loading}>Loading objects...</div>;
  }
  if (error) {
    return <div className={styles.error}>Failed to load: {error.message || String(error)}</div>;
  }

  return (
    <div className={styles.browser}>
      {KIND_GROUPS.map((group) => {
        const items = grouped[group.kind] || [];
        return (
          <div key={group.kind} className={styles.category}>
            <button className={styles.categoryHeader} onClick={() => toggle(group.kind)}>
              <span className={styles.categoryBar} style={{ background: group.color }} />
              <span className={styles.categoryLabel}>{group.label}</span>
              <span className={styles.chevron}>
                {expanded[group.kind] ? '▾' : '▸'}
              </span>
            </button>

            {expanded[group.kind] && (
              <div className={styles.categoryBody}>
                {items.length === 0 ? (
                  <div className={styles.placeholder}>No {group.label.toLowerCase()} available</div>
                ) : (
                  items.map((obj) => (
                    <button
                      key={obj.object_id}
                      className={`${styles.instrument} ${
                        selected?.object_id === obj.object_id ? styles.instrumentActive : ''
                      }`}
                      onClick={() => onSelect(obj)}
                      title={obj.name || obj.symbol}
                    >
                      <span className={styles.instrumentSymbol}>{obj.symbol}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default ObjectBrowser;
