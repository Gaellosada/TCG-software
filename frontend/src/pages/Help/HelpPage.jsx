import { useState } from 'react';
import styles from './HelpPage.module.css';

const SECTIONS = ['general', 'data', 'portfolio'];

function HelpPage() {
  const [activeSection, setActiveSection] = useState('general');

  return (
    <div className={styles.page}>
      <span className={styles.label}>DOCUMENTATION</span>
      <h1 className={styles.title}>Help</h1>
      <p className={styles.subtitle}>
        Documentation and guides for the TCG simulation platform.
      </p>

      <nav className={styles.nav}>
        {SECTIONS.map((id) => (
          <button
            key={id}
            className={`${styles.navBtn} ${activeSection === id ? styles.navBtnActive : ''}`}
            onClick={() => {
              setActiveSection(id);
              document.getElementById(`help-${id}`)?.scrollIntoView({ behavior: 'smooth' });
            }}
          >
            {id.charAt(0).toUpperCase() + id.slice(1)}
          </button>
        ))}
      </nav>

      {/* ── General ── */}
      <section id="help-general" className={styles.section}>
        <h2 className={styles.sectionHeading}>General</h2>
        <p className={styles.conceptText}>
          Trajectoire CAP is a volatility trading simulation platform. Browse
          historical market data, construct weighted portfolios, and run
          backtesting simulations against real price histories.
        </p>
        <p className={styles.conceptText}>
          The platform is organized around two main sections:
        </p>
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
            Construct and manage weighted portfolios of instruments. Define
            allocations, rebalancing rules, and analyze performance metrics.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Settings</h3>
        <p className={styles.conceptText}>
          Use the Settings page to switch between dark and light themes and to
          set your default chart type preference (candlestick or line).
        </p>

        <h3 className={styles.conceptTitle}>Tips</h3>
        <ul className={styles.tips}>
          <li>All market data is currently sourced from the legacy Java platform.</li>
          <li>Use keyboard shortcuts where available for faster navigation.</li>
        </ul>
      </section>

      {/* ── Data ── */}
      <section id="help-data" className={styles.section}>
        <h2 className={styles.sectionHeading}>Data</h2>
        <p className={styles.conceptText}>
          The Data page lets you browse collections of instruments organized by
          asset class. Select a collection, pick an instrument, and view its
          price history as an interactive chart.
        </p>

        <h3 className={styles.conceptTitle}>Price Charts</h3>
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

        <h3 className={styles.conceptTitle}>Continuous Futures Rolling</h3>
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
          and the next begins. These are toggleable — click the roll dates entry
          in the chart legend to show or hide them.
        </p>

        <h3 className={styles.conceptTitle}>Cycle Filtering</h3>
        <p className={styles.conceptText}>
          Filter the visible data by contract cycle to focus on specific
          expiration months or contract series within a continuous futures chain.
        </p>
      </section>

      {/* ── Portfolio ── */}
      <section id="help-portfolio" className={styles.section}>
        <h2 className={styles.sectionHeading}>Portfolio</h2>
        <p className={styles.conceptText}>
          The Portfolio page lets you construct weighted portfolios of
          instruments and analyze their historical performance.
        </p>

        <h3 className={styles.conceptTitle}>Building Portfolios</h3>
        <p className={styles.conceptText}>
          Add holdings to your portfolio and assign weights to each. Weights are
          normalized internally, so only the ratios matter: a 60/40 split,
          0.6/0.4, and 3/2 all produce the same allocation. Negative weights
          represent short positions — the portfolio borrows and sells the
          instrument, profiting from price declines.
        </p>

        <h3 className={styles.conceptTitle}>Chart Display Modes</h3>
        <p className={styles.conceptText}>
          The equity chart has three display modes, selectable via the pill
          toggle above the chart. Each mode answers a different question.
        </p>
        <div className={styles.card}>
          <h3>Portfolio Only</h3>
          <p>
            Shows only the combined portfolio equity line, normalized to start
            at 100. Use this for a clean view of overall performance without
            visual clutter from individual holdings.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Normalized ($100)</h3>
          <p>
            Shows the portfolio line alongside each individual holding, all
            normalized to start at $100. This answers &ldquo;if I had invested
            $100 in each holding separately, how would they compare?&rdquo;
            Because every line starts at the same value, you get a fair
            comparison of relative performance regardless of weight differences.
            This is the default mode.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Weighted</h3>
          <p>
            Shows each holding&apos;s actual weighted equity contribution to the
            portfolio. A holding with a 60% weight will appear larger than one
            with a 10% weight, reflecting their real impact on the portfolio.
            Use this to see how much each position contributed in absolute terms.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Rebalancing</h3>
        <p className={styles.conceptText}>
          Rebalancing periodically resets allocations back to their target
          weights. Without rebalancing, price movements cause positions to drift:
          winning instruments grow as a share of the portfolio, losers shrink.
          Over time, the actual allocation can diverge significantly from the
          intended one.
        </p>
        <p className={styles.conceptText}>
          Rebalancing enforces allocation discipline and can capture
          mean-reversion: it systematically trims outperforming positions and
          adds to underperforming ones. Rebalanced portfolios typically exhibit
          lower volatility than a buy-and-hold equivalent.
        </p>
        <p className={styles.conceptText}>
          Rebalance dates are shown as dashed purple vertical lines on the chart.
        </p>

        <h3 className={styles.conceptTitle}>Available Frequencies</h3>
        <div className={styles.card}>
          <h3>None (Buy-and-Hold)</h3>
          <p>
            No rebalancing. Initial weights are set once and never adjusted.
            Positions drift freely with market movements.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Daily / Weekly / Monthly / Quarterly / Annually</h3>
          <p>
            At the end of each period, holdings are adjusted back to target
            weights. Monthly rebalancing is a common default — frequent enough
            to control drift, infrequent enough to limit transaction costs.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Return Types</h3>
        <p className={styles.conceptText}>
          Returns can be expressed in two mathematically equivalent ways. The
          choice affects how returns aggregate over time.
        </p>
        <div className={styles.card}>
          <h3>Normal Returns</h3>
          <p>
            Standard percentage returns: (price_today - price_yesterday) /
            price_yesterday. Intuitive and directly interpretable — a normal
            return of +0.10 means your $100 became $110. However, normal
            returns are not additive over time: two consecutive +10% days
            do not compound to +20%.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Log Returns</h3>
          <p>
            Natural logarithm of the price ratio: ln(price_today /
            price_yesterday). Log returns are additive over time, which makes
            them mathematically convenient for multi-period analysis and
            statistical modeling. For small return magnitudes, log returns and
            normal returns are nearly identical. Log returns are the standard
            in quantitative finance.
          </p>
        </div>
      </section>
    </div>
  );
}

export default HelpPage;
