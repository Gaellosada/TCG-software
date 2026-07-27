// Per-run market-data source selector (Database v1 | Database v2).
//
// ONE component, used verbatim by the Portfolio page (config bar, next to
// Compute) and the Signals page (Run panel, above the Run button) — the
// workflow this exists for is "run it on v1, run it on v2, compare", so the
// control sits one click from the compute action on both pages rather than in
// Settings. Project rule: reuse identical components across pages; do NOT build
// a page-specific variant.
//
// When v2 is selected it also renders the measured v2 capability limits
// (``V2_LIMITATIONS``) so the user is never silently misled about coverage,
// cycles, streams or the differing end dates.

import {
  DATA_SOURCE_OPTIONS,
  DATA_SOURCE_V2,
  V2_LIMITATIONS,
} from '../lib/dataSource';
import styles from './DataSourceSelector.module.css';

/**
 * @param {Object}   p
 * @param {'v1'|'v2'} p.value
 * @param {(v: 'v1'|'v2') => void} p.onChange
 * @param {boolean=} p.disabled
 * @param {string=}  p.id        select element id (unique per page instance)
 * @param {boolean=} p.showNotes render the v2 limitation notes (default true)
 */
function DataSourceSelector({
  value,
  onChange,
  disabled = false,
  id = 'data-source-select',
  showNotes = true,
}) {
  const isV2 = value === DATA_SOURCE_V2;
  return (
    <div className={styles.wrap} data-testid="data-source-selector">
      <div className={styles.row}>
        <label className={styles.label} htmlFor={id}>Data source</label>
        <select
          id={id}
          className={styles.select}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          data-testid="data-source-select"
          title="Which market-data warehouse this run reads from. v1 is the reference; v2 is the new star schema."
        >
          {DATA_SOURCE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>
      {isV2 && showNotes && (
        <div className={styles.notes} data-testid="data-source-v2-notes" role="note">
          <div className={styles.notesTitle}>Database v2 limits</div>
          <ul className={styles.notesList}>
            {V2_LIMITATIONS.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default DataSourceSelector;
