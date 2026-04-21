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
        Platform guide — browse data, build indicators and signals, construct portfolios.
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
            {SECTION_LABELS[id]}
          </button>
        ))}
      </nav>

      {/* ── Overview ── */}
      <section id="help-overview" className={styles.section}>
        <h2 className={styles.sectionHeading}>Overview</h2>
        <p className={styles.conceptText}>
          Trajectoire CAP is a volatility trading simulation platform. Browse historical
          market data, create technical indicators, design entry/exit signals, and
          construct weighted portfolios to analyze performance against real price histories.
        </p>

        <h3 className={styles.conceptTitle}>Workflow</h3>
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
          Each page builds on the previous. Start by exploring price data, then define
          indicators on that data, compose signals from those indicators, and finally
          evaluate strategies through portfolio construction.
        </p>

        <h3 className={styles.conceptTitle}>Saving</h3>
        <p className={styles.conceptText}>
          Autosave is available on the Indicators, Signals, and Portfolio pages. Your work
          is persisted automatically as you make changes.
        </p>

        <h3 className={styles.conceptTitle}>Tips</h3>
        <ul className={styles.tips}>
          <li>
            All charts support zoom (drag to select), pan (<span className={styles.kbd}>Shift</span>+drag),
            and CSV export via a dedicated button in the chart toolbar.
          </li>
          <li>Settings and saved data persist in your browser&apos;s localStorage.</li>
        </ul>
      </section>

      {/* ── Data ── */}
      <section id="help-data" className={styles.section}>
        <h2 className={styles.sectionHeading}>Data</h2>
        <p className={styles.conceptText}>
          Browse collections of instruments organized by asset class: indexes, assets,
          futures, and options. Select a collection, pick an instrument, and view its
          price history as an interactive chart.
        </p>

        <h3 className={styles.conceptTitle}>Price Charts</h3>
        <p className={styles.conceptText}>
          Charts display as <strong>candlestick</strong> (OHLC bodies with wicks) or
          {' '}<strong>line</strong> (close price only). Your default chart type is set in
          Settings. When less than 50% of bars have real OHLC data, the chart automatically
          falls back to line mode and the chart-type selector is hidden.
        </p>

        <Details title="Which futures have candlestick data?">
          <p>
            Crypto futures (BTC, ETH) and VIX have full OHLC from their data sources.
            Most legacy futures (SP500, Gold, bonds, FX) were ingested from a source that
            only provided settlement prices. If these contracts are re-ingested from a
            source with OHLC (e.g., IQFeed, Interactive Brokers), candlestick becomes
            available automatically.
          </p>
        </Details>

        <h3 className={styles.conceptTitle}>Continuous Futures</h3>
        <p className={styles.conceptText}>
          Futures contracts expire. To build a continuous price history, contracts are
          stitched together by rolling from one to the next. The roll creates a seam
          where prices may jump.
        </p>

        <h3 className={styles.conceptTitle}>Adjustment Methods</h3>
        <div className={styles.card}>
          <h3>None (Raw)</h3>
          <p>
            Contracts concatenated without adjustment. Prices jump at roll boundaries.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Proportional</h3>
          <p>
            Prior prices multiplied by the new/old contract price ratio at each roll.
            Preserves percentage returns. Standard for most futures backtesting.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Difference</h3>
          <p>
            The price gap is added to all prior prices at each roll. Preserves dollar
            differences. Useful for spread strategies.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Roll Dates</h3>
        <p className={styles.conceptText}>
          Gray dotted vertical lines mark where one contract ends and the next begins.
          Toggle visibility by clicking the roll dates entry in the chart legend.
        </p>

        <h3 className={styles.conceptTitle}>Cycle Filtering</h3>
        <p className={styles.conceptText}>
          Filter by contract cycle to focus on specific expiration months or contract
          series within a continuous futures chain.
        </p>
      </section>

      {/* ── Portfolio ── */}
      <section id="help-portfolio" className={styles.section}>
        <h2 className={styles.sectionHeading}>Portfolio</h2>
        <p className={styles.conceptText}>
          Construct weighted portfolios of instruments and analyze their historical
          performance with a full metrics suite.
        </p>

        <h3 className={styles.conceptTitle}>Building Portfolios</h3>
        <p className={styles.conceptText}>
          Add holdings and assign weights. Weights are normalized, so only ratios matter:
          60/40, 0.6/0.4, and 3/2 all produce the same allocation. Negative weights
          represent short positions.
        </p>

        <h3 className={styles.conceptTitle}>Display Modes</h3>
        <div className={styles.card}>
          <h3>Portfolio Only</h3>
          <p>
            Combined portfolio equity line normalized to 100. Clean view of overall
            performance.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Normalized ($100)</h3>
          <p>
            Portfolio line alongside each holding, all starting at $100. Fair comparison
            of relative performance regardless of weight differences.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Weighted</h3>
          <p>
            Each holding&apos;s actual weighted equity contribution. Shows real impact of
            each position on the portfolio in absolute terms.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Rebalancing</h3>
        <p className={styles.conceptText}>
          Rebalancing periodically resets allocations to target weights. Without it, price
          movements cause drift: winners grow as a share, losers shrink. Rebalancing
          enforces allocation discipline and can capture mean-reversion by trimming
          outperformers and adding to underperformers.
        </p>
        <p className={styles.conceptText}>
          Rebalance dates appear as dashed purple vertical lines on the chart.
        </p>
        <div className={styles.card}>
          <h3>None (Buy-and-Hold)</h3>
          <p>Initial weights set once; positions drift freely.</p>
        </div>
        <div className={styles.card}>
          <h3>Daily / Weekly / Monthly / Quarterly / Annually</h3>
          <p>
            Holdings adjusted to target weights at period end. Monthly is a common default
            — frequent enough to control drift, infrequent enough to limit transaction costs.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Return Types</h3>
        <div className={styles.card}>
          <h3>Normal Returns</h3>
          <p>
            Standard percentage: (P_today - P_yesterday) / P_yesterday. Intuitive but not
            additive over time.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Log Returns</h3>
          <p>
            ln(P_today / P_yesterday). Additive over time, making them standard for
            multi-period analysis. Nearly identical to normal returns at small magnitudes.
          </p>
        </div>

        <Details title="Math detail: return aggregation">
          <p>
            Two consecutive normal returns of +10% compound to +21% (1.1 &times; 1.1 = 1.21),
            not +20%. Log returns are additive: ln(1.1) + ln(1.1) = ln(1.21). This property
            makes log returns preferable for statistical modeling and time-series analysis.
          </p>
        </Details>

        <h3 className={styles.conceptTitle}>Metrics</h3>
        <p className={styles.conceptText}>
          The metrics panel shows: Sharpe ratio, Sortino ratio, max drawdown, CAGR,
          Calmar ratio, and win rate. Below the main chart, a monthly returns heatmap
          and yearly returns summary are available.
        </p>

        <h3 className={styles.conceptTitle}>Save / Load</h3>
        <p className={styles.conceptText}>
          Portfolios can be saved and loaded. Autosave preserves your current working
          portfolio as you make changes.
        </p>
      </section>

      {/* ── Indicators ── */}
      <section id="help-indicators" className={styles.section}>
        <h2 className={styles.sectionHeading}>Indicators</h2>
        <p className={styles.conceptText}>
          Create and manage technical indicators. The page has three panels: indicator
          list (left), code editor (middle), and parameters (right).
        </p>

        <h3 className={styles.conceptTitle}>Layout</h3>
        <div className={styles.card}>
          <h3>Left Panel</h3>
          <p>
            List of default and custom indicators with search. Default indicators are
            read-only. Use <strong>+ New</strong> to create custom indicators.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Middle Panel</h3>
          <p>
            Python code editor (CodeMirror). Write your indicator logic in a{' '}
            <code>compute(series, ...)</code> function. A documentation tab is
            available for notes.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Right Panel</h3>
          <p>
            Parameters panel with typed inputs, series mapping (pick which instruments
            to apply the indicator to), and a Run button to execute.
          </p>
        </div>

        <Details title="compute() function convention">
          <p>
            Every indicator must define a <code>compute</code> function. The first parameter
            is always <code>series</code> (a dictionary mapping input names to NumPy arrays).
            Additional parameters must have a type annotation (<code>int</code>,{' '}
            <code>float</code>, or <code>bool</code>) and a default value.
          </p>
          <div className={styles.codeBlock}>
{`def compute(series, window: int = 20):
    s = series['price']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(
        s, np.ones(window)/window, mode='valid'
    )
    return out`}
          </div>
          <p>
            Parameters declared in the signature appear as editable fields in the right
            panel. Their default values set the initial inputs. The function must return
            a NumPy array.
          </p>
        </Details>

        <h3 className={styles.conceptTitle}>Own Panel Toggle</h3>
        <p className={styles.conceptText}>
          When enabled, the indicator renders on its own separate chart below the main
          price chart instead of overlaying it. Useful for oscillators and indicators
          with a different scale than price.
        </p>
      </section>

      {/* ── Signals ── */}
      <section id="help-signals" className={styles.section}>
        <h2 className={styles.sectionHeading}>Signals</h2>
        <p className={styles.conceptText}>
          Design entry and exit rules for trading positions. Signals combine indicator
          values and price data into a structured decision model.
        </p>

        <h3 className={styles.conceptTitle}>Structure</h3>
        <div className={styles.card}>
          <h3>Inputs</h3>
          <p>
            Declare data sources (instruments) the signal operates on. These become
            available in conditions.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Blocks</h3>
          <p>
            Four block types: long entry, long exit, short entry, short exit.
            Each block selects an input and contains weighted conditions.
          </p>
        </div>
        <div className={styles.card}>
          <h3>Conditions</h3>
          <p>
            Each condition compares two operands (indicator values, constants, or price
            fields) with a comparison operator. Conditions within a block are combined
            by their weights.
          </p>
        </div>

        <h3 className={styles.conceptTitle}>Weights and Capital</h3>
        <p className={styles.conceptText}>
          Entry block weights control capital allocation. The total budget should
          not exceed 1.0 (100% of capital). Negative weights are not used on blocks
          — use Short Entry blocks instead.
        </p>

        <Details title="Position model: latched entries">
          <p>
            The signal engine uses a latched position model. When an entry condition
            triggers, the position stays on until the matching exit condition fires.
            A long entry remains active until a long exit clears it; similarly for
            short entry / short exit.
          </p>
          <p>
            Every entry block must have a matching exit block. A long entry without a
            long exit will never close the position.
          </p>
        </Details>

        <h3 className={styles.conceptTitle}>Results</h3>
        <p className={styles.conceptText}>
          After running a signal, two stacked charts appear: the top chart shows
          input prices alongside the realized P&amp;L curve, and the bottom chart
          overlays indicator values with entry/exit markers on the price series.
        </p>
      </section>

      {/* ── Settings ── */}
      <section id="help-settings" className={styles.section}>
        <h2 className={styles.sectionHeading}>Settings</h2>
        <div className={styles.card}>
          <h3>Theme</h3>
          <p>Switch between dark and light mode.</p>
        </div>
        <div className={styles.card}>
          <h3>Default Chart Type</h3>
          <p>
            Choose candlestick or line as the default for all price charts. Individual
            charts fall back to line when OHLC data is insufficient.
          </p>
        </div>
        <p className={styles.conceptText}>
          All preferences are stored in your browser&apos;s localStorage and persist
          across sessions.
        </p>
      </section>
    </div>
  );
}

export default HelpPage;
