import { useState, useEffect, useCallback, useMemo } from 'react';
import Plot from 'react-plotly.js';
import { getNotebook } from '../../api/agent';
import renderMarkdown from './renderMarkdown';
import styles from './NotebookPanel.module.css';

/**
 * Strip ANSI escape sequences from a string for display-safe rendering.
 * RCA-4 fix: error tracebacks contain literal \x1b[31m...\x1b[39m sequences
 * that render as garbled text in <pre> elements. Strip them before display.
 * Chosen approach: minimal strip (not ansi-to-html) to avoid adding a dep.
 * Pattern covers SGR sequences (\x1b[N;...m) and a few others (cursor, clear).
 */
function ansiStrip(text) {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\x1b\[[0-9;]*m/g, '');
}

/**
 * Renders a Jupyter notebook fetched from the agent backend.
 *
 * Props:
 *   sessionId          {string|null}  Active session id
 *   notebookReady      {boolean}      Becomes true when backend signals notebook availability
 *   notebookFailedInfo {object|null}  Non-null when notebook_failed WS event received.
 *                                     Shape: { reason, detail, timestamp }
 */
function NotebookPanel({ sessionId, notebookReady, notebookFailedInfo = null }) {
  const [notebook, setNotebook] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchNotebook = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getNotebook(sessionId);
      setNotebook(data);
    } catch (err) {
      setError(err.message || 'Failed to load notebook');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  // Fetch when notebookReady fires or sessionId changes
  useEffect(() => {
    if (notebookReady && sessionId) {
      fetchNotebook();
    }
  }, [notebookReady, sessionId, fetchNotebook]);

  // Reset state when session changes
  useEffect(() => {
    setNotebook(null);
    setError(null);
  }, [sessionId]);

  if (!sessionId) {
    return (
      <div className={styles.panel}>
        <div className={styles.empty}>Select a session to view the notebook.</div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className={styles.panel}>
        <div className={styles.loading}>
          <span className={styles.spinner} />
          Loading notebook...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.panel}>
        <div className={styles.error}>{error}</div>
      </div>
    );
  }

  // Issue 27 F3: notebook_failed state — show explanation before the pending spinner.
  if (notebookFailedInfo && !notebookReady) {
    const reasonLabel =
      notebookFailedInfo.reason === 'parse_error'
        ? 'The notebook file could not be parsed (malformed JSON).'
        : 'The notebook was written without executing cells — no outputs available.';
    return (
      <div className={styles.panel}>
        <div className={styles.failedState} data-testid="notebook-failed-panel">
          <span className={styles.failedIcon} aria-hidden="true">⚠</span>
          <div>
            <strong>Notebook compilation failed</strong>
            <div className={styles.failedDetail}>{reasonLabel}</div>
            {notebookFailedInfo.detail && (
              <div className={styles.failedDetail}>{notebookFailedInfo.detail}</div>
            )}
            <div className={styles.failedHint}>
              To get outputs, send a new message asking the agent to re-run the notebook via <code>compile_workspace</code>.
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!notebook || !notebookReady) {
    return (
      <div className={styles.panel}>
        <div className={styles.loading}>
          <span className={styles.spinner} />
          Pending...
        </div>
      </div>
    );
  }

  const cells = notebook.cells || [];

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Notebook</span>
        <button
          type="button"
          className={styles.runAllBtn}
          onClick={fetchNotebook}
          title="Refresh notebook"
        >
          Refresh
        </button>
      </div>
      <div className={styles.cells}>
        {cells.map((cell, idx) => (
          <NotebookCell key={idx} cell={cell} />
        ))}
      </div>
    </div>
  );
}

function NotebookCell({ cell }) {
  if (cell.cell_type === 'markdown') {
    const source = Array.isArray(cell.source)
      ? cell.source.join('')
      : cell.source || '';
    return (
      <div className={styles.mdCell}>
        <span
          dangerouslySetInnerHTML={{ __html: renderMarkdown(source) }}
        />
      </div>
    );
  }

  if (cell.cell_type === 'code') {
    const source = Array.isArray(cell.source)
      ? cell.source.join('')
      : cell.source || '';
    const execCount = cell.execution_count;
    return (
      <div className={styles.codeCell}>
        <div className={styles.codeCellHeader}>
          <span className={styles.execCount}>
            [{execCount != null ? execCount : ' '}]:
          </span>
        </div>
        <pre className={styles.codeBlock}>
          <code>{source}</code>
        </pre>
        {cell.outputs && cell.outputs.length > 0 && (
          <div className={styles.outputs}>
            {cell.outputs.map((output, oi) => (
              <CellOutput key={oi} output={output} />
            ))}
          </div>
        )}
      </div>
    );
  }

  // Unknown cell type — render as raw text
  const raw = Array.isArray(cell.source)
    ? cell.source.join('')
    : cell.source || '';
  return <pre className={styles.codeBlock}><code>{raw}</code></pre>;
}

function CellOutput({ output }) {
  // Error output — RCA-4: strip ANSI escape codes from traceback before display.
  if (output.output_type === 'error') {
    const rawTb = (output.traceback || []).join('\n');
    const tb = ansiStrip(rawTb);
    const ename = ansiStrip(output.ename || '');
    const evalue = ansiStrip(output.evalue || '');
    return (
      <pre className={styles.errorOutput}>
        {ename}: {evalue}
        {tb && `\n${tb}`}
      </pre>
    );
  }

  // Stream output (stdout/stderr)
  if (output.output_type === 'stream') {
    const text = Array.isArray(output.text) ? output.text.join('') : output.text || '';
    return <pre className={styles.textOutput}>{text}</pre>;
  }

  // Display data or execute_result
  if (output.output_type === 'display_data' || output.output_type === 'execute_result') {
    const data = output.data || {};

    // Plotly figure
    if (data['application/vnd.plotly.v1+json']) {
      return <PlotlyOutput figure={data['application/vnd.plotly.v1+json']} />;
    }

    // Image (base64 PNG)
    if (data['image/png']) {
      return (
        <div className={styles.imageOutput}>
          <img
            src={`data:image/png;base64,${data['image/png']}`}
            alt="Cell output"
          />
        </div>
      );
    }

    // HTML output
    if (data['text/html']) {
      const html = Array.isArray(data['text/html'])
        ? data['text/html'].join('')
        : data['text/html'];
      return (
        <div
          className={styles.htmlOutput}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      );
    }

    // Plain text fallback
    if (data['text/plain']) {
      const text = Array.isArray(data['text/plain'])
        ? data['text/plain'].join('')
        : data['text/plain'];
      return <pre className={styles.textOutput}>{text}</pre>;
    }
  }

  return null;
}

/**
 * Renders a Plotly figure using the same react-plotly.js <Plot> component
 * as the shared Chart wrapper. Applies dark-theme defaults.
 */
function PlotlyOutput({ figure }) {
  const layout = useMemo(
    () => ({
      ...(figure.layout || {}),
      paper_bgcolor: 'transparent',
      plot_bgcolor: '#161922',
      font: { color: '#e4e8f0', family: 'Outfit, sans-serif' },
      margin: { t: 30, r: 20, b: 40, l: 50 },
    }),
    [figure.layout],
  );

  return (
    <div className={styles.plotlyContainer}>
      <Plot
        data={figure.data || []}
        layout={layout}
        config={{ responsive: true, displayModeBar: false }}
        useResizeHandler
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  );
}

export default NotebookPanel;
