import { useState } from 'react';
import useAsync from '../../hooks/useAsync';
import { listInstruments } from '../../api/data';
import styles from './InstrumentList.module.css';

const PAGE_SIZE = 50;

/**
 * Displays instruments in a collection with pagination.
 * Parent uses key={collection} to remount on collection change, resetting skip.
 */
function InstrumentList({ collection, selected, onSelect }) {
  const [skip, setSkip] = useState(0);

  const { data, loading, error } = useAsync(
    () => listInstruments(collection, { skip, limit: PAGE_SIZE }),
    [collection, skip]
  );

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <span className={styles.heading}>Instruments</span>
        </div>
        <div className={styles.loading}>Loading instruments...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <span className={styles.heading}>Instruments</span>
        </div>
        <div className={styles.error}>
          Failed to load: {error.message}
        </div>
      </div>
    );
  }

  if (!data || !data.items || data.items.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.header}>
          <span className={styles.heading}>Instruments</span>
        </div>
        <div className={styles.empty}>No instruments found.</div>
      </div>
    );
  }

  const { items, total } = data;
  const hasPrev = skip > 0;
  const hasNext = skip + PAGE_SIZE < (total || 0);
  const showPagination = total > PAGE_SIZE;

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <span className={styles.heading}>Instruments</span>
        {total != null && <span className={styles.count}>{total} total</span>}
      </div>
      <ul className={styles.list}>
        {items.map((item) => {
          const id = item._id || item.id || item.symbol || item;
          const label = item.symbol || item.name || id;
          const isActive = selected === id;
          return (
            <li
              key={id}
              className={`${styles.item} ${isActive ? styles.itemActive : ''}`}
              onClick={() => onSelect(id)}
            >
              {label}
            </li>
          );
        })}
      </ul>
      {showPagination && (
        <div className={styles.pagination}>
          <button
            className={styles.pageBtn}
            disabled={!hasPrev || loading}
            onClick={() => setSkip(Math.max(0, skip - PAGE_SIZE))}
          >
            Prev
          </button>
          <span className={styles.pageInfo}>
            {skip + 1}&ndash;{Math.min(skip + PAGE_SIZE, total)} of {total}
          </span>
          <button
            className={styles.pageBtn}
            disabled={!hasNext || loading}
            onClick={() => setSkip(skip + PAGE_SIZE)}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

export default InstrumentList;
