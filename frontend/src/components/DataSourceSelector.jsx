// Market-data source selector (Database v1 | Database v2).
//
// Used at ADD TIME only, inside InstrumentPickerModal, to pick the source for a
// NEW instrument (portfolio holding / signal input / indicator series). A source
// is chosen ONCE here and is immutable thereafter — existing rows show a
// read-only <SourceBadge>, not this control. (There is no page-level default and
// no per-run selector; the read-only badge is the only other source surface.)
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
 * @param {string=}  p.label     control label (default "Data source"). Callers
 *                               that use the control as a SEED default (not a
 *                               per-run wire field) pass a seed-only label.
 * @param {string=}  p.helper    optional sub-text under the row (seed-only
 *                               explainer). Omitted when null (default).
 * @param {string=}  p.title     select tooltip. Defaults to the per-run wording;
 *                               seed sites override it to say so.
 * @param {string=}  p.testId    base for the data-testids so multiple instances
 *                               (e.g. a page selector + a modal selector) never
 *                               collide. Default "data-source" reproduces the
 *                               original ``data-source-selector`` / ``-select`` /
 *                               ``-v2-notes`` ids verbatim.
 */
function DataSourceSelector({
  value,
  onChange,
  disabled = false,
  id = 'data-source-select',
  showNotes = true,
  label = 'Data source',
  helper = null,
  title = 'Which market-data warehouse this run reads from. v1 is the reference; v2 is the new star schema.',
  testId = 'data-source',
}) {
  const isV2 = value === DATA_SOURCE_V2;
  return (
    <div className={styles.wrap} data-testid={`${testId}-selector`}>
      <div className={styles.row}>
        <label className={styles.label} htmlFor={id}>{label}</label>
        <select
          id={id}
          className={styles.select}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          data-testid={`${testId}-select`}
          title={title}
        >
          {DATA_SOURCE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>
      {helper && (
        <div className={styles.helper} data-testid={`${testId}-helper`}>{helper}</div>
      )}
      {isV2 && showNotes && (
        <div className={styles.notes} data-testid={`${testId}-v2-notes`} role="note">
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
