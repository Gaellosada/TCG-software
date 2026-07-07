import { useMemo, useState } from 'react';
import PillToggle from '../PillToggle';
import styles from './TradeLog.module.css';

/**
 * Collapsible Trades panel. Reads `response.trades` and joins each row
 * with the matching position's price series for open/close prices.
 * P&L is derived frontend-side: realised = (close/open - 1) * signed_weight;
 * log = ln(close/open) * signed_weight. The mode toggle picks between them.
 *
 * `entryDescriptions` / `exitDescriptions` are maps `{ [block_id]: description }`
 * supplied by the caller from the selected signal's rules.
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

export function formatSignedPercent(fraction) {
  if (typeof fraction !== 'number' || !Number.isFinite(fraction)) return '—';
  const pct = fraction * 100;
  const sign = pct > 0 ? '+' : '';
  // Always two decimals: integer-detection via === is FP-fragile
  // (e.g. (110/100 - 1) * 100 = 10.000000000000009).
  return `${sign}${pct.toFixed(2)}%`;
}

// Standard notation (never scientific), locale pinned to 'en-US' for
// deterministic output across machines (matches src/utils/format.js).
// Magnitude-aware precision:
//   |qty| >= 1 → up to 2 decimals but the FULL integer part is kept
//     (so 14325 → "14,325", not the 4-sig-fig "14,320").
//   |qty| <  1 → up to 4 significant digits, so sub-1 fractions stay
//     meaningful (0.0004123 → "0.0004123").
const QTY_FMT_LARGE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const QTY_FMT_SMALL = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 4 });

// Signed amount (2 decimals, thousands separators, explicit +/-). Used for a
// roll row's per-segment realised P&L, which the backend supplies as a DOLLAR
// figure (`segment_pnl`) rather than the frontend-derived percentage.
const AMT_FMT = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatSignedAmount(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  const body = AMT_FMT.format(Math.abs(v));
  const sign = v > 0 ? '+' : v < 0 ? '-' : '';
  return `${sign}${body}`;
}

/**
 * Formats an unsigned trade quantity + its unit label, e.g. "12.34 contracts"
 * or "1,432 shares". Sign is conveyed by the Direction column, so the magnitude
 * is shown. Non-finite input → em-dash (guards NaN/Infinity).
 */
export function formatQuantity(qty, unit) {
  if (typeof qty !== 'number' || !Number.isFinite(qty)) return '—';
  const mag = Math.abs(qty);
  const num = (mag >= 1 ? QTY_FMT_LARGE : QTY_FMT_SMALL).format(mag);
  const label = typeof unit === 'string' && unit.trim() ? ` ${unit.trim()}` : '';
  return `${num}${label}`;
}

function priceAtBar(positionsByInputId, inputId, bar) {
  if (bar === null || bar === undefined) return null;
  const pos = positionsByInputId.get(inputId);
  if (!pos || !pos.price || !Array.isArray(pos.price.values)) return null;
  const v = pos.price.values[bar];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * Returns the last finite price value in the position's price series,
 * walking back from the end to skip trailing nulls/NaN.
 * Returns null if no finite value is found.
 */
function lastFinitePrice(positionsByInputId, inputId) {
  const pos = positionsByInputId.get(inputId);
  if (!pos || !pos.price || !Array.isArray(pos.price.values)) return null;
  const values = pos.price.values;
  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return null;
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
  showHoldingColumn = false,
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
      // For open trades (close_bar == null), use the last finite price in the
      // position series as the effective close price for PnL ONLY.
      // The displayed close-price column stays as em-dash for open trades.
      const isOpen = tr.close_bar === null || tr.close_bar === undefined;
      const pnlClosePrice = isOpen
        ? lastFinitePrice(positionsByInputId, tr.input_id)
        : closePrice;
      return {
        ...tr,
        _openTs: openTs,
        _closeTs: closeTs,
        _openPrice: openPrice,
        _closePrice: closePrice,
        _pnlClosePrice: pnlClosePrice,
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
        <div data-testid="pnl-mode-toggle">
          <PillToggle
            options={[
              { value: 'realised', label: 'Realised' },
              { value: 'log', label: 'Log' },
            ]}
            value={pnlMode}
            onChange={setPnlMode}
            ariaLabel="P&L display mode"
          />
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
                    {showHoldingColumn && (
                      <th scope="col" data-testid="holding-col-header">Holding</th>
                    )}
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
                    // Roll rows (continuous / hold-option per-held-contract) carry
                    // a backend DOLLAR `segment_pnl`; show it verbatim (a realised
                    // amount, mode-independent). Every other trade keeps the
                    // frontend-derived percentage under the Realised/Log toggle.
                    const hasSegmentPnl =
                      typeof tr.segment_pnl === 'number' && Number.isFinite(tr.segment_pnl);
                    const pnl = hasSegmentPnl
                      ? tr.segment_pnl
                      : computePnl(pnlMode, tr._openPrice, tr._pnlClosePrice, tr.signed_weight);
                    const pnlClass = pnl === null
                      ? ''
                      : pnl >= 0
                        ? styles.pnlPos
                        : styles.pnlNeg;
                    const pnlText = pnl === null
                      ? '—'
                      : hasSegmentPnl
                        ? formatSignedAmount(pnl)
                        : formatSignedPercent(pnl);

                    // Size cell: "counts mode" is detected by the presence of the
                    // `quantity` KEY on the trade (portfolio trades carry it; the
                    // shared Signals-page usage does NOT). Key present + finite →
                    // fractional count + unit; key present but null/NaN (price/M
                    // uncomputable) → em-dash; key absent entirely → fall back to
                    // the constant target % so the Signals page is unchanged.
                    const hasCounts = 'quantity' in tr;
                    const sizeDisplay = hasCounts
                      ? (Number.isFinite(tr.quantity)
                        ? formatQuantity(tr.quantity, tr.quantity_unit)
                        : '—')
                      : formatSignedPercent(tr.signed_weight);

                    const entryName = tr.entry_block_name || '(unnamed)';
                    const entryTooltip = tr.entry_block_id
                      ? (entryDescriptions[tr.entry_block_id] || '')
                      : '';

                    const exitName = isClosed ? (tr.exit_block_name || '(unnamed)') : 'open';
                    const exitTooltip = isClosed && tr.exit_block_id
                      ? (exitDescriptions[tr.exit_block_id] || '')
                      : '';

                    return (
                      <tr
                        key={`${tr.entry_block_id}|${tr.open_bar}`}
                        data-testid="trade-row"
                        data-open-bar={tr.open_bar}
                      >
                        <td>{formatTs(tr._openTs)}</td>
                        <td>{isClosed ? formatTs(tr._closeTs) : <span className={styles.openTag}>open</span>}</td>
                        <td>{tr.input_id}</td>
                        {showHoldingColumn && (
                          <td data-testid="trade-holding">
                            {tr.holding_name ?? tr.holding_id ?? '—'}
                          </td>
                        )}
                        <td>
                          <span className={`${styles.dirPill} ${directionClass}`}>
                            {tr.direction}
                          </span>
                        </td>
                        <td
                          className={tr.signed_weight >= 0 ? styles.pnlPos : styles.pnlNeg}
                          data-testid="trade-size"
                        >
                          {sizeDisplay}
                        </td>
                        <td>{formatPrice(tr._openPrice)}</td>
                        <td>{isClosed ? formatPrice(tr._closePrice) : <span className={styles.openTag}>—</span>}</td>
                        <td className={pnlClass} data-testid="trade-pnl">
                          {pnlText}
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
