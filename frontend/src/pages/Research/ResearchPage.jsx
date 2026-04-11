import styles from './ResearchPage.module.css';

function ResearchPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Research</h1>
      <p className={styles.description}>
        Backtesting engine and strategy research workspace.
      </p>
      <div className={styles.comingSoon}>
        <p>Coming in Phase 5</p>
      </div>
    </div>
  );
}

export default ResearchPage;
