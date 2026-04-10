import { useState } from 'react';
import styles from './HelpPage.module.css';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'provenance', label: 'Provenance' },
  { id: 'navigation', label: 'Navigation' },
];

function OverviewTab() {
  return (
    <div className={styles.content}>
      <h2>Trajectoire CAP</h2>
      <p>
        A financial simulation and exploration platform for volatility trading
        strategies. Browse historical market data, construct portfolios, and run
        backtesting simulations against real price histories.
      </p>

      <h2>What you can do</h2>
      <ul>
        <li>
          <strong>Browse market data</strong> — explore collections of
          instruments across asset classes (indices, ETFs, futures, options) with
          interactive price charts.
        </li>
        <li>
          <strong>Build portfolios</strong> — select instruments and configure
          allocations for backtesting.
        </li>
        <li>
          <strong>Run strategy simulations</strong> — execute trading strategies
          against historical data with both vectorized (fast approximation) and
          event-based (precise) simulation engines.
        </li>
      </ul>

      <h2>Platform sections</h2>
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
      <div className={styles.card}>
        <h3>Saved Strategies</h3>
        <p>
          Store and organize tested strategy configurations. Review past
          simulation results and export findings.
        </p>
      </div>
    </div>
  );
}

function ProvenanceTab() {
  return (
    <div className={styles.content}>
      <h2>Result provenance</h2>
      <p>
        Every computation in this platform tracks the origin of its data. This
        is critical for reproducibility: when you see a result, you can always
        tell where the underlying data came from and whether it might be stale.
      </p>

      <h2>Source types</h2>
      <div className={styles.card}>
        <h3>
          <span className={`${styles.badge} ${styles.badgeLegacy}`}>Legacy</span>
        </h3>
        <p>
          Data imported from the original Java platform. These datasets have
          been migrated as-is and reflect the state of the legacy system at the
          time of export. Legacy data is read-only and will not be refreshed.
        </p>
      </div>
      <div className={styles.card}>
        <h3>
          <span className={`${styles.badge} ${styles.badgePrecomputed}`}>Precomputed</span>
        </h3>
        <p>
          Cached results from previous simulation or computation runs. Using
          precomputed results avoids redundant work. The platform records when
          each result was computed and which inputs were used, so you can verify
          whether the cache is still valid for your scenario.
        </p>
      </div>
      <div className={styles.card}>
        <h3>
          <span className={`${styles.badge} ${styles.badgeOnTheFly}`}>On-the-fly</span>
        </h3>
        <p>
          Freshly computed from raw data at request time. These results use the
          latest data and current computation logic. On-the-fly results take
          longer but guarantee you are working with the most current output.
        </p>
      </div>

      <h2>Why this matters</h2>
      <p>
        In quantitative finance, trusting your results requires knowing exactly
        how they were produced. Provenance tracking lets you:
      </p>
      <ul>
        <li>Reproduce any result by re-running with the same inputs.</li>
        <li>
          Detect stale data — if market data has been updated since a cached
          result was computed, you know to recompute.
        </li>
        <li>
          Audit the pipeline — trace any output back to its raw data source
          and the computation steps applied.
        </li>
      </ul>
    </div>
  );
}

function NavigationTab() {
  return (
    <div className={styles.content}>
      <h2>Using the Data section</h2>
      <p>The Data section follows a three-level drill-down pattern:</p>
      <ol>
        <li>
          <strong>Select a collection</strong> — the left panel lists all
          available data collections, grouped by asset class. Click one to load
          its instruments.
        </li>
        <li>
          <strong>Select an instrument</strong> — the instrument list appears
          below the collection list, showing symbols in the chosen collection.
          Use pagination controls if the collection has more than 50 instruments.
        </li>
        <li>
          <strong>View the price chart</strong> — the right panel shows an
          interactive Plotly chart of the instrument's close price history. Use
          your mouse to zoom (click and drag) or pan (shift + drag). Double-click
          to reset the view.
        </li>
      </ol>

      <h2>Using the Portfolio section</h2>
      <p className={styles.comingSoon}>
        Portfolio management is under development. You will be able to
        construct portfolios by selecting instruments from the Data section and
        configuring allocation weights and rebalancing schedules.
      </p>

      <h2>Using the Research section</h2>
      <p className={styles.comingSoon}>
        The Research workspace is under development. It will provide a
        code-driven environment for ad-hoc analysis, instrument comparison, and
        strategy prototyping.
      </p>

      <h2>Using Saved Strategies</h2>
      <p className={styles.comingSoon}>
        Strategy storage is under development. Completed simulations will be
        saved here with their full configuration and results for review and
        comparison.
      </p>
    </div>
  );
}

const TAB_COMPONENTS = {
  overview: OverviewTab,
  provenance: ProvenanceTab,
  navigation: NavigationTab,
};

function HelpPage() {
  const [activeTab, setActiveTab] = useState('overview');
  const ActiveTabComponent = TAB_COMPONENTS[activeTab];

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Help</h1>
      <p className={styles.description}>
        Documentation and guides for the TCG simulation platform.
      </p>
      <div className={styles.tabs}>
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            className={`${styles.tab} ${activeTab === id ? styles.tabActive : ''}`}
            onClick={() => setActiveTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <ActiveTabComponent />
    </div>
  );
}

export default HelpPage;
