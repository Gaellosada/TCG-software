import styles from './DataPage.module.css';

function DataPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Data</h1>
      <p className={styles.description}>
        Market data explorer and dataset management.
      </p>
      <div className={styles.placeholder}>
        <p>Content loading...</p>
      </div>
    </div>
  );
}

export default DataPage;
