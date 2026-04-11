import styles from './HelpPage.module.css';

function HelpPage() {
  return (
    <div className={styles.page}>
      <span className={styles.label}>DOCUMENTATION</span>
      <h1 className={styles.title}>Help</h1>
      <p className={styles.subtitle}>
        Documentation and guides for the TCG simulation platform.
      </p>

      <section className={styles.section}>
        <h2 className={styles.sectionHeading}>Getting Started</h2>
        <p className={styles.conceptText}>
          Trajectoire CAP is a financial simulation and exploration platform for
          volatility trading strategies. Browse historical market data, construct
          portfolios, and run backtesting simulations against real price histories.
        </p>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionHeading}>Pages Overview</h2>

        <div className={styles.card}>
          <h3>Data</h3>
          <p>
            Browse your market data collections. Select an asset class, pick an
            instrument, and view its price history as an interactive chart.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Portfolio</h3>
          <p>
            Construct and manage portfolios of instruments. Define allocations and
            rebalancing rules for simulation inputs.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Research</h3>
          <p>
            Run ad-hoc analysis and exploration. Compare instruments, compute
            metrics, and prototype strategies with code-driven workflows.
          </p>
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionHeading}>Key Concepts</h2>

        <h3 className={styles.conceptTitle}>Strategy Signals</h3>
        <p className={styles.conceptText}>
          Strategies define entry and exit rules as Python scripts with a
          self-documenting API. Each strategy produces signals that drive
          simulation execution.
        </p>

        <h3 className={styles.conceptTitle}>Simulation Engines</h3>
        <p className={styles.conceptText}>
          Two engines are available: a <strong>vectorized engine</strong> for fast
          approximation across the full price history, and an{' '}
          <strong>event-based engine</strong> for precise trade-by-trade simulation
          with realistic fill modeling.
        </p>

        <h3 className={styles.conceptTitle}>Result Provenance</h3>
        <p className={styles.conceptText}>
          Every computation tracks the origin of its data. This is critical for
          reproducibility — when you see a result, you can always tell where the
          underlying data came from and whether it might be stale.
        </p>
        <div className={styles.provenanceList}>
          <div className={styles.provenanceItem}>
            <span className={`${styles.badge} ${styles.badgeLegacy}`}>Legacy</span>
            <span>Data imported from the original Java platform. Read-only, will not be refreshed.</span>
          </div>
          <div className={styles.provenanceItem}>
            <span className={`${styles.badge} ${styles.badgePrecomputed}`}>Precomputed</span>
            <span>Cached results from previous simulation runs. Avoids redundant work.</span>
          </div>
          <div className={styles.provenanceItem}>
            <span className={`${styles.badge} ${styles.badgeOnTheFly}`}>On-the-fly</span>
            <span>Freshly computed from raw data at request time. Most current but slower.</span>
          </div>
        </div>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionHeading}>Tips</h2>
        <ul className={styles.tips}>
          <li>All market data is currently sourced from the legacy Java platform and tagged as Legacy provenance.</li>
          <li>Use the Settings page to switch between dark and light themes.</li>
          <li>The Research page will support ad-hoc analysis and instrument comparison in future phases.</li>
        </ul>
      </section>
    </div>
  );
}

export default HelpPage;
