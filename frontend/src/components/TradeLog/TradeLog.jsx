import { useMemo, useState } from 'react';
import styles from './TradeLog.module.css';

/**
 * Collapsible Trades panel rendered below the page-level Statistics
 * panel. Reads `response.trades` and joins each row with the matching
 * position's price series for open/close prices. P&L is derived
 * frontend-side per the Wave-2 locked contract (CONTRACT.md §2).
 *
 * `exitDescriptions` — `{ [exit_block_id]: description }` built by
 * the caller from the selected signal's `rules.exits[]`.
 * `entryDescriptions` — `{ [entry_block_id]: description }` built by
 * the caller from the selected signal's `rules.entries[]`.
 */

function formatTs(ts) {
  if (!Number.isFinite(ts)) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '—';
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
}

function formatPrice(p) {
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return p.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function formatSignedPercent(fraction) {
  if (typeof fraction !== 'number' || !Number.isFinite(fraction)) return '—';
  const pct = fraction * 100;
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(pct === Math.trunc(pct) ? 0 : 2)}%`;
}

function priceAtBar(positionsByInputId, inputId, bar) {
  if (bar === null || bar === undefined) return null;
  const pos = positionsByInputId.get(inputId);
  if (!pos || !pos.price || !Array.isArray(pos.price.values)) return null;
  const v = pos.price.values[bar];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function computePnl(mode, openPrice, closePrice, signedWeight) {
  if (openPrice === null || closePrice === null || openPrice <= 0 || closePrice <= 0) {
    return null;
  }
  const w = signedWeight ?? 0;
  if (mode === 'log') {
    return Math.log(closePrice / openPrice) * w;
  }
  return (closePrice / openPrice - 1) * w;
}

function TradeLog({
  trades = [],
  timestamps = [],
  positions = [],
  exitDescriptions = {},
  entryDescriptions = {},
}) {
  const [open, setOpen] = useState(false);
  const [pnlMode, setPnlMode] = useState('realised');

  const positionsByInputId = useMemo(() => {
    const m = new Map();
    for (const p of positions) {
      if (p && p.input_id) m.set(p.input_id, p);
    }
    return m;
  }, [positions]);

  const rows = useMemo(() => {
    const sorted = [...trades].sort((a, b) => {
      const ao = a.open_bar ?? -1;
      const bo = b.open_bar ?? -1;
      if (ao !== bo) return ao - bo;
      return String(a.entry_block_id || '').localeCompare(String(b.entry_block_id || ''));
    });
    return sorted.map((tr) => {
      const openTs = Number.isInteger(tr.open_bar) ? timestamps[tr.open_bar] : null;
      const closeTs = Number.isInteger(tr.close_bar) ? timestamps[tr.close_bar] : null;
      const openPrice = priceAtBar(positionsByInputId, tr.input_id, tr.open_bar);
      const closePrice = priceAtBar(positionsByInputId, tr.input_id, tr.close_bar);
      return {
        ...tr,
        _openTs: openTs,
        _closeTs: closeTs,
        _openPrice: openPrice,
        _closePrice: closePrice,
      };
    });
  }, [trades, timestamps, positionsByInputId]);

  const count = rows.length;
  const headingId = 'trade-log-heading';
  const bodyId = 'trade-log-body';
  const pnlHeader = pnlMode === 'log' ? 'Log P&L' : 'Realised P&L';

  return (
    <div className={styles.tradeLog} data-testid="trade-log">
      <div className={styles.headerRow}>
        <button
          type="button"
          className={styles.header}
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={bodyId}
          id={headingId}
          data-testid="trade-log-toggle"
        >
          <span className={styles.chevron} aria-hidden="true">{open ? '▾' : '▸'}</span>
          <span className={styles.title}>Trades</span>
          <span className={styles.count} data-testid="trade-log-count">({count})</span>
        </button>
        <div
          className={styles.pnlToggle}
          onClick={(e) => e.stopPropagation()}
          data-testid="pnl-mode-toggle"
        >
          <button
            type="button"
            className={`${styles.pnlPill} ${pnlMode === 'realised' ? styles.pnlPillActive : ''}`}
            onClick={() => setPnlMode('realised')}
            data-testid="pnl-pill-realised"
          >
            Realised
          </button>
          <button
            type="button"
            className={`${styles.pnlPill} ${pnlMode === 'log' ? styles.pnlPillActive : ''}`}
            onClick={() => setPnlMode('log')}
            data-testid="pnl-pill-log"
          >
            Log
          </button>
        </div>
      </div>
      {open && (
        <div id={bodyId} className={styles.body} role="region" aria-labelledby={headingId}>
          {count === 0 ? (
            <div className={styles.empty} data-testid="trade-log-empty">No trades</div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">Open</th>
                    <th scope="col">Close</th>
                    <th scope="col">Input</th>
                    <th scope="col">Direction</th>
                    <th scope="col">Size</th>
                    <th scope="col">Open price</th>
                    <th scope="col">Close price</th>
                    <th scope="col" data-testid="pnl-col-header">{pnlHeader}</th>
                    <th scope="col">Entry reason</th>
                    <th scope="col">Exit reason</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((tr) => {
                    const isClosed = tr.close_bar !== null && tr.close_bar !== undefined;
                    const directionClass = tr.direction === 'long'
                      ? styles.dirLong
                      : styles.dirShort;
                    const pnl = computePnl(pnlMode, tr._openPrice, tr._closePrice, tr.signed_weight);
                    const pnlClass = pnl === null
                      ? ''
                      : pnl >= 0
                        ? styles.pnlPos
                        : styles.pnlNeg;

                    const entryName = tr.entry_block_name || '(unnamed)';
                    const entryTooltip = tr.entry_block_id
                      ? (entryDescriptions[tr.entry_block_id] || '')
                      : '';

                    const exitName = isClosed ? (tr.exit_block_name || '(unnamed)') : 'open';
                    const exitTooltip = isClosed && tr.exit_block_id
                      ? (exitDescriptions[tr.exit_block_id] || '')
                      : '';

                    return (
                      <tr key={`${tr.entry_block_id}|${tr.open_bar}`} data-testid="trade-row">
                        <td>{formatTs(tr._openTs)}</td>
                        <td>{isClosed ? formatTs(tr._closeTs) : <span className={styles.openTag}>open</span>}</td>
                        <td>{tr.input_id}</td>
                        <td>
                          <span className={`${styles.dirPill} ${directionClass}`}>
                            {tr.direction}
                          </span>
                        </td>
                        <td className={tr.signed_weight >= 0 ? styles.pnlPos : styles.pnlNeg}>
                          {formatSignedPercent(tr.signed_weight)}
                        </td>
                        <td>{formatPrice(tr._openPrice)}</td>
                        <td>{isClosed ? formatPrice(tr._closePrice) : <span className={styles.openTag}>—</span>}</td>
                        <td className={pnlClass}>
                          {pnl === null ? '—' : formatSignedPercent(pnl)}
                        </td>
                        <td>
                          <span
                            className={styles.reason}
                            title={entryTooltip || undefined}
                            data-testid="trade-entry-reason"
                            data-reason-tooltip={entryTooltip}
                          >
                            {entryName}
                          </span>
                        </td>
                        <td>
                          <span
                            className={isClosed ? styles.reason : styles.openTag}
                            title={exitTooltip || undefined}
                            data-testid="trade-exit-reason"
                            data-reason-tooltip={exitTooltip}
                          >
                            {exitName}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default TradeLog;
