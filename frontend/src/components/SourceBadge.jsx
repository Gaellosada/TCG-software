// Read-only market-data source badge ('v1' | 'v2').
//
// A per-instrument source is chosen ONCE, at add-time, in the instrument
// picker, and is IMMUTABLE thereafter: to change it the user deletes the
// instrument and re-adds it from the other database. This badge is the
// display-only surface of that choice — it renders the source but exposes NO
// control to mutate it. Shown for v1 too (never hidden) so every row states its
// warehouse explicitly. Purely presentational; it never touches the wire.
import { DATA_SOURCE_V2 } from '../lib/dataSource';
import styles from './SourceBadge.module.css';

/**
 * @param {Object}   p
 * @param {*}        p.source  the instrument's own source; anything not exactly
 *                             'v2' renders as v1 (mirrors coerceDataSource).
 * @param {string=}  p.testId  data-testid for the badge element.
 */
function SourceBadge({ source, testId }) {
  const isV2 = source === DATA_SOURCE_V2;
  return (
    <span
      className={`${styles.badge} ${isV2 ? styles.v2 : styles.v1}`}
      data-testid={testId}
      data-source={isV2 ? 'v2' : 'v1'}
      title={`Market-data source: ${isV2 ? 'Database v2 (new star schema)' : 'Database v1 (tcg_instruments)'}. Fixed at add time — delete and re-add to change it.`}
    >
      {isV2 ? 'v2' : 'v1'}
    </span>
  );
}

export default SourceBadge;
