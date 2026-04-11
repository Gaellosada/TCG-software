import { useState } from 'react';
import CategoryBrowser from './CategoryBrowser';
import PriceChart from './PriceChart';
import styles from './DataPage.module.css';

function DataPage() {
  const [selected, setSelected] = useState(null);

  return (
    <div className={styles.page}>
      <div className={styles.leftPanel}>
        <CategoryBrowser
          selected={selected}
          onSelect={setSelected}
        />
      </div>
      <div className={styles.rightPanel}>
        {selected ? (
          <PriceChart
            collection={selected.collection}
            instrument={selected.symbol}
          />
        ) : (
          <div className={styles.welcome}>
            <div className={styles.welcomeInner}>
              <h2>Select an instrument</h2>
              <p>Pick an instrument from the categories on the left to view its price history.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default DataPage;
