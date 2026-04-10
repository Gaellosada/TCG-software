import { useState } from 'react';
import CollectionList from './CollectionList';
import InstrumentList from './InstrumentList';
import PriceChart from './PriceChart';
import styles from './DataPage.module.css';

function DataPage() {
  const [selectedCollection, setSelectedCollection] = useState(null);
  const [selectedInstrument, setSelectedInstrument] = useState(null);

  function handleCollectionSelect(collection) {
    setSelectedCollection(collection);
    setSelectedInstrument(null);
  }

  function handleInstrumentSelect(instrument) {
    setSelectedInstrument(instrument);
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Data</h1>
        <p className={styles.description}>
          Browse market data collections and view instrument price histories.
        </p>
      </div>
      <div className={styles.layout}>
        <div className={styles.leftPanel}>
          <CollectionList
            selected={selectedCollection}
            onSelect={handleCollectionSelect}
          />
          {selectedCollection && (
            <InstrumentList
              key={selectedCollection}
              collection={selectedCollection}
              selected={selectedInstrument}
              onSelect={handleInstrumentSelect}
            />
          )}
        </div>
        <div className={styles.rightPanel}>
          {selectedInstrument ? (
            <PriceChart
              collection={selectedCollection}
              instrument={selectedInstrument}
            />
          ) : (
            <div className={styles.welcome}>
              <div className={styles.welcomeInner}>
                <h2>Select an instrument to view its price history</h2>
                <p>
                  {selectedCollection
                    ? 'Pick an instrument from the list on the left.'
                    : 'Start by selecting a collection, then choose an instrument.'}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default DataPage;
