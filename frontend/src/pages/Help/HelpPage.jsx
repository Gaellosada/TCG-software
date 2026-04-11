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
        <h2 className={styles.sectionHeading}>Continuous Futures Rolling</h2>
        <p className={styles.conceptText}>
          Futures contracts expire. To build a continuous price history for
          backtesting, contracts are stitched together by rolling from one
          to the next. The roll creates a seam where prices may jump.
        </p>

        <h3 className={styles.conceptTitle}>Adjustment Methods</h3>
        <div className={styles.card}>
          <h3>None (Raw)</h3>
          <p>
            Contracts are concatenated without adjustment. Prices jump at roll
            boundaries. Use this when your strategy explicitly handles rolls.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Proportional</h3>
          <p>
            At each roll, all prior prices are multiplied by the ratio of
            new-to-old contract prices. Preserves percentage returns across
            rolls. Standard for most futures backtesting.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Difference</h3>
          <p>
            At each roll, the price gap is added to all prior prices.
            Preserves dollar differences. Useful for spread strategies.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Roll Dates</h3>
        <p className={styles.conceptText}>
          Gray dotted vertical lines on the chart mark where one contract ends
          and the next begins. These are recorded for transparency — you can
          always see exactly where rolls occurred.
        </p>

        <h3 className={styles.conceptTitle}>Chart Types &amp; Data Availability</h3>
        <p className={styles.conceptText}>
          When OHLC data (Open, High, Low, Close) is available, the chart can
          display as <strong>candlestick</strong> (filled bodies showing the
          open-close range with high-low wicks). You can set your default chart
          type in <strong>Settings</strong>.
        </p>
        <p className={styles.conceptText}>
          However, many legacy futures contracts only store a{' '}
          <strong>close/settle price</strong> — the open, high, and low fields
          are missing. When less than half the bars have real OHLC values, the
          chart automatically falls back to a <strong>line chart</strong> and
          the chart-type selector is hidden.
        </p>
        <div className={styles.card}>
          <h3>Which futures have candlestick?</h3>
          <p>
            Crypto futures (BTC, ETH) and VIX have full OHLC from their data
            sources. Most other legacy futures (SP500, Gold, bonds, FX) were
            ingested from a source that only provided settlement prices. If
            these contracts are re-ingested from a source that includes OHLC
            (e.g., IQFeed, Interactive Brokers), candlestick will become
            available automatically.
          </p>
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
