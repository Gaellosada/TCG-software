import { useState } from 'react';
import styles from './HelpPage.module.css';

const SECTIONS = ['overview', 'data', 'portfolio', 'indicators', 'signals', 'settings'];

const SECTION_LABELS = {
  overview: 'Overview',
  data: 'Data',
  portfolio: 'Portfolio',
  indicators: 'Indicators',
  signals: 'Signals',
  settings: 'Settings',
};

function Details({ title, children }) {
  return (
    <details className={styles.details}>
      <summary className={styles.summary}>{title}</summary>
      <div className={styles.detailsBody}>{children}</div>
    </details>
  );
}

function HelpPage() {
  const [activeSection, setActiveSection] = useState('overview');

  return (
    <div className={styles.page}>
      <span className={styles.label}>DOCUMENTATION</span>
      <h1 className={styles.title}>Help</h1>
      <p className={styles.subtitle}>
        Non-obvious behaviors and conventions.
      </p>

      <nav className={styles.nav}>
        {SECTIONS.map((id) => (
          <button
            key={id}
            className={`${styles.navBtn} ${activeSection === id ? styles.navBtnActive : ''}`}
            aria-current={activeSection === id ? 'true' : undefined}
            onClick={() => {
              setActiveSection(id);
              document.getElementById(`help-${id}`)?.scrollIntoView({ behavior: 'smooth' });
            }}
          >
            {SECTION_LABELS[id]}
          </button>
        ))}
      </nav>

      {/* ── Overview ── */}
      <section id="help-overview" className={styles.section}>
        <h2 className={styles.sectionHeading}>Overview</h2>
        <p className={styles.conceptText}>
          Trajectoire CAP is a financial data exploration and backtesting platform.
        </p>

        <div className={styles.workflow}>
          <span className={styles.workflowStep}>Data</span>
          <span className={styles.workflowArrow}>&rarr;</span>
          <span className={styles.workflowStep}>Indicators</span>
          <span className={styles.workflowArrow}>&rarr;</span>
          <span className={styles.workflowStep}>Signals</span>
          <span className={styles.workflowArrow}>&rarr;</span>
          <span className={styles.workflowStep}>Portfolio</span>
        </div>

        <p className={styles.conceptText}>
          Indicators, Signals, and Portfolio pages autosave to your browser&apos;s
          localStorage. All preferences and saved work persist across sessions on the
          same browser.
        </p>

        <h3 className={styles.conceptTitle}>Worth knowing</h3>
        <ul className={styles.tips}>
          <li>
            Every chart legend has a CSV-export entry that downloads the currently
            visible lines.
          </li>
          <li>
            Charts support drag-to-zoom and{' '}
            <span className={styles.kbd}>Shift</span>+drag to pan.
          </li>
        </ul>
      </section>

      {/* ── Data ── */}
      <section id="help-data" className={styles.section}>
        <h2 className={styles.sectionHeading}>Data</h2>

        <h3 className={styles.conceptTitle}>Candlestick vs line</h3>
        <p className={styles.conceptText}>
          When less than 50% of bars have real OHLC data, the chart silently falls back
          to line mode and hides the chart-type selector. Most legacy futures (SP500,
          gold, bonds, FX) are settlement-only; crypto futures and VIX have full OHLC.
        </p>

        <h3 className={styles.conceptTitle}>Continuous futures</h3>
        <p className={styles.conceptText}>
          Expiring contracts are stitched into a single series by rolling at the contract
          boundary. Without adjustment, prices jump at each roll.
        </p>
        <ul className={styles.tips}>
          <li><strong>None</strong> &mdash; raw concatenation; prices jump at every roll.</li>
          <li><strong>Ratio</strong> &mdash; prior prices scaled by the new/old ratio. Preserves percentage returns — the right default for most return-based backtests.</li>
          <li><strong>Difference</strong> &mdash; the price gap is added to prior prices. Preserves dollar differences. Useful for spread strategies.</li>
        </ul>

        <p className={styles.conceptText}>
          Roll dates appear as gray dotted vertical lines. Toggle them via the
          &ldquo;Roll Dates&rdquo; entry in the chart legend.
        </p>
      </section>

      {/* ── Portfolio ── */}
      <section id="help-portfolio" className={styles.section}>
        <h2 className={styles.sectionHeading}>Portfolio</h2>

        <p className={styles.conceptText}>
          Weights are normalized: 60/40, 0.6/0.4, and 3/2 are all equivalent. Negative
          weights represent short positions.
        </p>

        <h3 className={styles.conceptTitle}>Display modes</h3>
        <ul className={styles.tips}>
          <li><strong>Portfolio Only</strong> &mdash; combined equity line normalized to 100.</li>
          <li><strong>Normalized ($100)</strong> &mdash; portfolio line plus each holding, all starting at $100. Fair relative comparison regardless of weight differences.</li>
          <li><strong>Weighted</strong> &mdash; each holding&apos;s actual weighted contribution to the portfolio.</li>
        </ul>

        <h3 className={styles.conceptTitle}>Rebalancing</h3>
        <p className={styles.conceptText}>
          Without rebalancing, winners grow as a share of the portfolio and losers
          shrink. Rebalancing periodically resets to target weights, enforcing allocation
          discipline. Monthly is a common default — frequent enough to control drift,
          infrequent enough to limit transaction costs. Rebalance dates appear as dashed
          purple lines on the chart.
        </p>

        <h3 className={styles.conceptTitle}>Returns grid</h3>
        <p className={styles.conceptText}>
          A monthly heatmap and yearly summary appear below the equity chart. A toggle
          switches between normal and log returns.
        </p>
        <Details title="Normal vs. log returns">
          <p>
            <strong>Normal:</strong> not additive — two +10% returns compound to +21%,
            not +20%. <strong>Log:</strong> ln(P/P_prev) is additive over time and
            standard for multi-period statistical analysis. Nearly identical to normal
            returns at small magnitudes.
          </p>
        </Details>
      </section>

      {/* ── Indicators ── */}
      <section id="help-indicators" className={styles.section}>
        <h2 className={styles.sectionHeading}>Indicators</h2>

        <p className={styles.conceptText}>
          Three panels: indicator list (left), Python code editor (middle), parameters
          and run controls (right).
        </p>

        <Details title="compute() function convention">
          <p>
            Every indicator defines a <code>compute</code> function. The first parameter
            is always <code>series</code> (a dict of input-name → NumPy array). Other
            parameters must have a type annotation (<code>int</code>, <code>float</code>,
            or <code>bool</code>) and a default. Declared parameters become editable
            fields in the right panel.
          </p>
          <pre className={styles.codeBlock}><code>
{`def compute(series, window: int = 20):
    s = series['price']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(
        s, np.ones(window)/window, mode='valid'
    )
    return out`}
          </code></pre>
          <p>The function must return a NumPy array.</p>
        </Details>

        <h3 className={styles.conceptTitle}>Own panel toggle</h3>
        <p className={styles.conceptText}>
          When enabled, the indicator renders on a separate chart below the price chart
          rather than overlaying it. Use this for oscillators and anything whose scale
          differs from price.
        </p>

        <h3 className={styles.conceptTitle}>Option stream inputs</h3>
        <p className={styles.conceptText}>
          Option-native indicators (ATM contract IV, Term-Structure Slope) consume option
          chains instead of spot. They expose three selectors below to pick which
          contract to read on each date.
        </p>

        <Details title="Expiration cycles (M, W3, W1, W2, W4, W, Q)">
          <ul>
            <li><strong>M</strong> &mdash; standard monthly, AM-settled. On SPX, only Mar/Jun/Sep/Dec — 8 of 12 months are empty.</li>
            <li><strong>W3</strong> &mdash; PM-settled, 3rd Friday of every month (SPXW). Best choice for monthly SPX indicators.</li>
            <li><strong>W1, W2, W4</strong> &mdash; 1st / 2nd / 4th Friday weeklies (SPX only). For short-dated strategies.</li>
            <li><strong>W</strong> &mdash; generic weekly cycle (crypto, VIX). Not the same as the Friday-specific weeklies above.</li>
            <li><strong>Q</strong> &mdash; quarterly, used by some roots (crypto).</li>
          </ul>
        </Details>

        <Details title="Maturity rules">
          <ul>
            <li><strong>Nearest to Target DTE</strong> &mdash; picks the expiration closest to N days-to-expiration. Adapts to the actual chain.</li>
            <li><strong>End of Month</strong> &mdash; last business day of the offset month.</li>
            <li><strong>+N Days</strong> &mdash; reference date plus N calendar days.</li>
            <li><strong>Next 3rd Friday</strong>, <strong>Fixed Date</strong> &mdash; pure date arithmetic; ignore the chain.</li>
          </ul>
        </Details>

        <Details title="Selection criteria">
          <ul>
            <li><strong>By Moneyness (K/S)</strong> &mdash; strike/spot closest to target (1.0 = ATM).</li>
            <li><strong>By Delta</strong> &mdash; delta closest to target (0.5 ≈ near-ATM call).</li>
            <li><strong>By Strike</strong> &mdash; a specific strike price.</li>
          </ul>
        </Details>
      </section>

      {/* ── Signals ── */}
      <section id="help-signals" className={styles.section}>
        <h2 className={styles.sectionHeading}>Signals</h2>

        <h3 className={styles.conceptTitle}>Structure</h3>
        <p className={styles.conceptText}>
          Conditions within a block are <strong>AND</strong>&rsquo;d. Blocks within a
          direction are <strong>OR</strong>&rsquo;d. Four direction types exist: long
          entry, long exit, short entry, short exit.
        </p>

        <h3 className={styles.conceptTitle}>Weights and capital</h3>
        <p className={styles.conceptText}>
          Entry blocks carry a weight that controls capital allocation. Weights above
          <strong> 1.0</strong> apply leverage. Set initial capital in the right panel
          before running.
        </p>

        <Details title="Position model: latched entries">
          <p>
            When an entry condition triggers, the position stays on until the matching
            exit fires. Long entry latches until long exit clears it; short entry until
            short exit. Every entry needs a matching exit, or the position never closes.
          </p>
        </Details>

        <Details title="Per-block reset binding (new)">
          <p>
            Each entry block can be bound to a reset condition. Once the block fires,
            it will not re-arm until its bound reset condition becomes true. Binding is
            per-block, so different blocks in the same direction can have independent
            re-arm rules. Use this to express &ldquo;fire on the cross, then ignore
            further crosses until price retraces&rdquo; without a phantom indicator.
          </p>
        </Details>

        <h3 className={styles.conceptTitle}>Run options</h3>
        <p className={styles.conceptText}>
          <strong>Initial Capital</strong> scales the P&amp;L curve.{' '}
          <strong>Don&apos;t Repeat Entries</strong> is a display-only filter — it hides
          consecutive duplicate markers in the chart but does not change the underlying
          computation.
        </p>

        <h3 className={styles.conceptTitle}>Risk-free rate</h3>
        <p className={styles.conceptText}>
          The risk-free rate used by Sharpe and Sortino metrics is configured globally
          in Settings, not per signal.
        </p>
      </section>

      {/* ── Settings ── */}
      <section id="help-settings" className={styles.section}>
        <h2 className={styles.sectionHeading}>Settings</h2>
        <p className={styles.conceptText}>
          <strong>Default Chart Type</strong> picks candlestick or line for all price
          charts. Individual charts still fall back to line when OHLC data is
          insufficient (see Data &rsaquo; Candlestick vs line).
        </p>
      </section>
    </div>
  );
}

export default HelpPage;
