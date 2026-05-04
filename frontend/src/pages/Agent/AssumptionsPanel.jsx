import { useMemo } from 'react';
import styles from './AssumptionsPanel.module.css';

const SOURCE_CLASSES = {
  default: styles.badgeDefault,
  inferred: styles.badgeInferred,
  user: styles.badgeUser,
};

const CONFIDENCE_CLASSES = {
  high: styles.dotHigh,
  medium: styles.dotMedium,
  low: styles.dotLow,
};

/**
 * Displays live ASSUMPTIONS.json content grouped by field group.
 *
 * Props:
 *   assumptions  {Array}  [{field, value, source, confidence, rationale, group}]
 */
function AssumptionsPanel({ assumptions }) {
  // Group assumptions by their `group` field
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
            <div className={styles.groupHeader}>{groupName}</div>
            {items.map((a) => (
              <div key={a.field} className={styles.assumption}>
                <div className={styles.assumptionTop}>
                  <span className={styles.fieldName}>{a.field}</span>
                  <div className={styles.badges}>
                    <span
                      className={`${styles.sourceBadge} ${SOURCE_CLASSES[a.source] || styles.badgeDefault}`}
                    >
                      {a.source || 'default'}
                    </span>
                    {a.confidence && (
                      <span
                        className={`${styles.confidenceDot} ${CONFIDENCE_CLASSES[a.confidence] || ''}`}
                        title={`Confidence: ${a.confidence}`}
                      />
                    )}
                  </div>
                </div>
                <div className={styles.value}>{formatValue(a.value)}</div>
                {a.rationale && (
                  <div className={styles.rationale}>{a.rationale}</div>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export default AssumptionsPanel;
