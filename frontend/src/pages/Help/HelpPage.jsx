import { useState } from 'react';
import styles from './HelpPage.module.css';

const SECTIONS = ['overview', 'data', 'options', 'portfolio', 'indicators', 'signals', 'tickets', 'settings'];

const SECTION_LABELS = {
  overview: 'Overview',
  data: 'Data',
  options: 'Options',
  portfolio: 'Portfolio',
  indicators: 'Indicators',
  signals: 'Signals',
  tickets: 'Tickets',
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
          Indicators, Signals, and Portfolios autosave to the backend, so your saved
          work follows you across reloads and machines. A &ldquo;Cloud&rdquo; status
          shows when a save is in flight. Lightweight UI preferences (chart type, the
          autosave toggle) stay in your browser&apos;s localStorage.
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

      {/* ── Options ── */}
      <section id="help-options" className={styles.section}>
        <h2 className={styles.sectionHeading}>Options</h2>

        <p className={styles.conceptText}>
          Option chains live on the <strong>Data</strong> page. Pick an option
          root in the left browser to open its chain. Three views are available
          for an option root.
        </p>

        <h3 className={styles.conceptTitle}>Chain table</h3>
        <p className={styles.conceptText}>
          For a chosen expiration, the table lists every strike (calls and puts)
          with bid / ask / mid and, where available, the Greeks. Values that
          come straight from the warehouse are shown plain; values the platform
          computed on the fly are italic with a{' '}
          <span className={styles.kbd}>&#9426;</span> badge, and a missing value
          shows an em-dash with the reason on hover.
        </p>

        <h3 className={styles.conceptTitle}>Per-contract history</h3>
        <p className={styles.conceptText}>
          Selecting a contract charts its history over time: price (mid) plus
          IV, delta (&Delta;), gamma (&Gamma;), theta (&Theta;) and vega
          (&nu;). Reference markers show the contract&apos;s first trade,
          expiration, the ATM cross, and the dates where &#124;&Delta;&#124;
          first reaches 0.30, 0.50 and 0.70.
        </p>

        <h3 className={styles.conceptTitle}>Smile snapshot</h3>
        <p className={styles.conceptText}>
          The snapshot view plots IV (or delta) across strikes for a single date
          and expiration &mdash; the volatility smile. Toggle calls vs puts, the
          plotted field (IV or delta), and the x-axis between raw strike and
          moneyness (K/S).
        </p>

        <h3 className={styles.conceptTitle}>Greeks coverage</h3>
        <p className={styles.conceptText}>
          Greeks are not stored for every root or every date. When a root has no
          Greeks, the Greek series (gamma, vega, theta) are unavailable and the
          relevant controls are disabled.
        </p>

        <Details title="Option streams (for indicators)">
          <p>
            An option indicator does not read one fixed contract &mdash; it reads
            a <strong>stream</strong>: on each date the engine reselects the
            contract matching your rule, so the series follows, say, &ldquo;the
            ~30-day, ~0.25-delta call&rdquo; as the chain rolls. You choose the
            option type (call/put), the expiration cycle, a maturity rule and a
            selection criterion (see <strong>Indicators</strong> &rsaquo; option
            stream inputs for what each cycle / maturity / selection means), and
            which series to read: mid price, IV, delta, gamma, vega, theta, open
            interest, or volume.
          </p>
          <p>
            <strong>Roll offset</strong> rolls to the next contract a chosen
            number of calendar days early (0 = roll at the rule&apos;s normal
            time). <strong>Back-adjustment</strong> (None / Ratio / Difference,
            the same methods as continuous futures) smooths the jump at each roll
            and applies to the <strong>mid-price</strong> stream only &mdash; it
            is ignored for IV, the Greeks, and the volume/open-interest streams,
            where a roll jump is not meaningful.
          </p>
        </Details>

        <Details title="How option backtests are priced">
          <p>
            When you add an option to a signal or portfolio you select it by a
            rule &mdash; e.g. &ldquo;the 10-delta put&rdquo; &mdash; not a
            fixed contract, so the contract you hold is rolled to a new one at
            each expiry. The backtest holds one selected contract between
            rolls and books its daily P&amp;L from the change in that
            contract&apos;s own premium, as a percentage of your capital,
            compounding it into the equity curve; at each roll it closes the
            expiring contract and opens the newly-selected one, so a roll is
            never counted as a price jump.
          </p>
          <p>
            Direction is the sign of the leg&apos;s weight (a short option
            profits as its premium decays). Size is <strong>nav_times</strong>
            &nbsp;&mdash; the premium notional you hold as a percentage of NAV
            (100% = full notional); because a short option&apos;s premium can
            multiply on a sell-off, a full-size short can wipe out, so use a
            small percentage.
          </p>
        </Details>
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

        <Details title="Block composition: AND / THEN and fire modes">
          <p>
            Conditions in a block can be organized into groups: conditions
            joined by <strong>AND</strong> form a conjunction group, and{' '}
            <strong>THEN</strong> separates groups in sequence. So{' '}
            <code>(A AND B) THEN (C AND D)</code> means both A and B become
            true, then both C and D become true within the THEN window
            (strictly after). Each connector is set independently per gap, so
            a block can mix AND and THEN freely along its condition list.
          </p>
          <p>
            Every block also has a fire mode. <strong>Pulse</strong> (the
            default for new blocks) fires only on the bar the block&apos;s
            condition completes, then re-arms. <strong>Sustained</strong>{' '}
            (legacy behavior, still available per block) stays true for as
            long as the condition holds. A triggered exit always resets any
            in-progress THEN-sequence or tap count on the entries it targets.
          </p>
        </Details>

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

      {/* ── Tickets ── */}
      <section id="help-tickets" className={styles.section}>
        <h2 className={styles.sectionHeading}>Tickets</h2>

        <p className={styles.conceptText}>
          A ticket is a free-text note you jot down whenever you hit an issue —
          a confusing result, a bug, or anything worth coming back to. The
          Tickets page is a simple running list, newest first.
        </p>

        <h3 className={styles.conceptTitle}>Add, edit, delete</h3>
        <ul className={styles.tips}>
          <li>
            <strong>Add</strong> &mdash; type in the box at the top and click
            <strong> Add</strong> (or press{' '}
            <span className={styles.kbd}>Ctrl</span>/<span className={styles.kbd}>Cmd</span>
            +<span className={styles.kbd}>Enter</span>). The button stays
            disabled until you type something.
          </li>
          <li>
            <strong>Edit</strong> &mdash; click the pencil (or double-click the
            text) to edit in place;{' '}
            <span className={styles.kbd}>Ctrl</span>/<span className={styles.kbd}>Cmd</span>
            +<span className={styles.kbd}>Enter</span> saves,{' '}
            <span className={styles.kbd}>Esc</span> cancels.
          </li>
          <li>
            <strong>Delete</strong> &mdash; the &times; asks for confirmation
            first. Unlike archiving a signal or indicator, deleting a ticket is
            <strong> permanent</strong> &mdash; the note is removed for good and
            cannot be recovered.
          </li>
        </ul>
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
