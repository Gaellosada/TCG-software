import styles from './PortfolioPage.module.css';

function PortfolioPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Portfolio</h1>
      <p className={styles.description}>
        Portfolio construction and optimization tools.
      </p>
      <div className={styles.comingSoon}>
        <p>Coming in Phase 3</p>
      </div>
    </div>
  );
}

export default PortfolioPage;
