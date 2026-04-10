import useAsync from '../../hooks/useAsync';
import { listCollections } from '../../api/data';
import styles from './CollectionList.module.css';

function CollectionList({ selected, onSelect }) {
  const { data: collections, loading, error } = useAsync(() => listCollections(), []);

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.heading}>Collections</div>
        <div className={styles.loading}>Loading collections...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.heading}>Collections</div>
        <div className={styles.error}>
          Failed to load collections: {error.message}
        </div>
      </div>
    );
  }

  if (!collections || collections.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.heading}>Collections</div>
        <div className={styles.empty}>No collections found.</div>
      </div>
    );
  }

  // Group by asset_class if available
  const grouped = {};
  for (const col of collections) {
    const group = col.asset_class || 'Other';
    if (!grouped[group]) grouped[group] = [];
    grouped[group].push(col);
  }
  const groupKeys = Object.keys(grouped).sort();

  return (
    <div className={styles.container}>
      <div className={styles.heading}>Collections</div>
      <ul className={styles.list}>
        {groupKeys.map((group) =>
          grouped[group].map((col) => {
            const name = col.name || col.collection_name || col;
            const id = typeof col === 'string' ? col : col.name || col.collection_name;
            const isActive = selected === id;
            return (
              <li
                key={id}
                className={`${styles.item} ${isActive ? styles.itemActive : ''}`}
                onClick={() => onSelect(id)}
              >
                <span>{id}</span>
                {groupKeys.length > 1 && (
                  <span className={styles.assetClass}>{group}</span>
                )}
              </li>
            );
          })
        )}
      </ul>
    </div>
  );
}

export default CollectionList;
