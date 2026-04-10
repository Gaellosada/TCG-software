import styles from './SavedStrategiesPage.module.css';

function SavedStrategiesPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Saved Strategies</h1>
      <p className={styles.description}>
        View and manage your saved trading strategies.
      </p>
      <div className={styles.comingSoon}>
        <p>Coming soon</p>
      </div>
    </div>
  );
}

export default SavedStrategiesPage;
