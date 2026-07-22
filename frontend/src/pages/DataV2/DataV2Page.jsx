import { useState } from 'react';
import ObjectBrowser from './ObjectBrowser';
import ObjectDetail from './ObjectDetail';
import styles from '../Data/DataPage.module.css';

/**
 * Database v2 page — a schema-native explorer over the dwh star schema
 * ``tcg_instruments_v2`` (object → contract → serie → fact_*), served by the
 * ``/api/data-v2`` router. Parallel to the v1 Data page (which stays as-is):
 * the object browser groups objects by ``kind`` instead of by collection
 * category, and drill-down is object → contracts + series → chart.
 *
 * Reuses the v1 DataPage layout CSS (left browser panel + right detail panel)
 * and the shared Chart component for all rendering.
 */
function DataV2Page() {
  const [selected, setSelected] = useState(null);

  return (
    <div className={styles.page}>
      <div className={styles.leftPanel}>
        <ObjectBrowser selected={selected} onSelect={setSelected} />
      </div>
      <div className={styles.rightPanel}>
        {selected ? (
          // Remount on object identity so switching objects wipes the
          // previous series selection / continuous controls.
          <ObjectDetail key={selected.object_id} object={selected} />
        ) : (
          <div className={styles.welcome}>
            <div className={styles.welcomeInner}>
              <h2>Select an object</h2>
              <p>
                Pick an object from the list on the left to browse its
                contracts and series, chart an individual series, or build a
                continuous series (futures &amp; options).
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default DataV2Page;
