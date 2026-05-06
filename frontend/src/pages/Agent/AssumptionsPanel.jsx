import { useMemo } from 'react';
import styles from './AssumptionsPanel.module.css';

const SOURCE_STRIPE = {
  user: styles.stripeUser,
  inferred: styles.stripeInferred,
  default: styles.stripeDefault,
};

/**
 * Displays live ASSUMPTIONS.json content grouped by field group.
 *
 * Option B layout: compact key-value rows with a 3 px left-border stripe
 * per row colored by `source` (user=green, inferred=blue, default=dim).
 * Rationale is always visible inline beneath each row.
 * Group label is a thin divider — no heavy section box.
 *
 * Props:
 *   assumptions  {Array}  [{field, value, source, confidence, rationale, group}]
 */
function AssumptionsPanel({ assumptions }) {
  // Group assumptions by their `group` field, preserving insertion order
  const groups = useMemo(() => {
    const map = new Map();
    for (const a of assumptions) {
      const g = a.group || 'General';
      if (!map.has(g)) map.set(g, []);
      map.get(g).push(a);
    }
    return map;
  }, [assumptions]);

  function formatValue(value) {
    if (value === null || value === undefined) return 'null';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Assumptions</span>
        {assumptions.length > 0 && (
          <span className={styles.count}>{assumptions.length}</span>
        )}
      </div>

      <div className={styles.list}>
        {assumptions.length === 0 && (
          <div className={styles.empty}>
            No assumptions yet — the agent will surface them as it works.
          </div>
        )}

        {[...groups.entries()].map(([groupName, items]) => (
          <div key={groupName} className={styles.group}>
            {/* Thin divider label — no heavy section box */}
            <div className={styles.groupDivider}>
              <span className={styles.groupLabel}>{groupName}</span>
            </div>

            {items.map((a) => {
              const stripeClass = SOURCE_STRIPE[a.source] || styles.stripeDefault;
              return (
                <div key={a.field} className={`${styles.assumption} ${stripeClass}`}>
                  {/* Row: fieldName + value on same line */}
                  <div className={styles.row}>
                    <span className={styles.fieldName}>{a.field}</span>
                    <span className={styles.value}>{formatValue(a.value)}</span>
                  </div>
                  {/* Rationale always visible beneath, inheriting the left stripe */}
                  {a.rationale && (
                    <div className={styles.rationale}>{a.rationale}</div>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

export default AssumptionsPanel;
