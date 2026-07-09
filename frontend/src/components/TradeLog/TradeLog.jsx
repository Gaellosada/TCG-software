import { useMemo, useState } from 'react';
import styles from './TradeLog.module.css';

/**
 * Collapsible Trades panel. Reads `response.trades` and joins each row
 * with the matching position's price series for open/close prices.
 * P&L is realised, derived frontend-side: (close/open - 1) * signed_weight.
 * Roll rows (rolling direct legs) instead carry a backend DOLLAR `segment_pnl`
 * and never use the frontend percentage (see below).
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

export function formatPrice(p) {
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return p.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

// ── Option `close`-stream mid-fallback surfacing ──────────────────────────────
// The backend option `close` (settlement) stream falls back to the row quote
// mid = (bid+ask)/2 on dates with no settlement print. Option roll rows carry
// two sibling booleans, ``open_price_fallback`` / ``close_price_fallback``, true
// on the specific price that came from that fallback. Wording is kept consistent
// with OptionStreamForm's CLOSE_TOOLTIP (the static help at selection time).

// Dynamic marker tooltip — on a specific price cell where the fallback fired.
export const FALLBACK_MARKER_TITLE =
  'Mid fallback — no settlement close print on this date; the quote mid '
  + '(bid + ask) / 2 was used.';

// Static hint on the Input cell of an option `close`-series leg — tells a log
// reader the leg's close series has a mid fallback even on rows where it did not
// fire.
export const CLOSE_INPUT_HINT =
  'Series = close (settlement). On dates with no settlement print the value '
  + 'falls back to the quote mid = (bid + ask) / 2 (marked * on the affected price).';

// True for an OPTION roll row whose Input label denotes the `close` series. The
// fallback sibling keys are present ONLY on option roll rows (absent on
// continuous/spot rows), so their presence identifies an option leg; the default
// portfolio label is ``"<collection> <type> <stream>"`` (e.g. "OPT_SP_500 P
// close"), so a trailing "close" token pins the close series specifically (a
// mid/bs_mid leg — whose fallback flags are always false — is NOT falsely
// hinted). A user-renamed leg that drops the token simply loses the static hint;
// the selection-time help + the dynamic markers still cover it.
function isOptionCloseInput(tr) {
  const hasFallbackKeys =
    'open_price_fallback' in tr || 'close_price_fallback' in tr;
  if (!hasFallbackKeys) return false;
  return /(^|\s)close$/i.test(String(tr.input_id ?? '').trim());
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

// Subtle superscript asterisk marking a price that came from the mid fallback.
// ``which`` distinguishes the open vs close cell in the test id.
function FallbackMark({ which }) {
  return (
    <sup
      className={styles.fallbackMark}
      role="img"
      aria-label={FALLBACK_MARKER_TITLE}
      title={FALLBACK_MARKER_TITLE}
      data-testid={`fallback-mark-${which}`}
    >
      *
    </sup>
  );
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

function computePnl(openPrice, closePrice, signedWeight) {
  if (openPrice === null || closePrice === null || openPrice <= 0 || closePrice <= 0) {
    return null;
  }
  const w = signedWeight ?? 0;
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
      // An option roll row carries an explicit `open_price`/`close_price` (the
      // contract PREMIUM), because its position series is the base-100 synthetic
      // equity — not a price. Prefer those when present; otherwise (instrument /
      // continuous rows) read the real price from the position series.
      const openPrice =
        'open_price' in tr ? tr.open_price : priceAtBar(positionsByInputId, tr.input_id, tr.open_bar);
      const closePrice =
        'close_price' in tr ? tr.close_price : priceAtBar(positionsByInputId, tr.input_id, tr.close_bar);
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
                    <th scope="col" data-testid="pnl-col-header">Realised P&L</th>
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
                    // Roll rows (continuous / hold-option per-held-contract) carry a
                    // backend DOLLAR `segment_pnl`; show it verbatim (a realised
                    // amount). A roll row must NEVER fall back to `computePnl`: that
                    // reads the leg SYNTHETIC equity (direction already baked in), so
                    // multiplying by signed_weight would double-invert (a profitable
                    // short shown negative). A roll row with a non-finite segment_pnl
                    // renders em-dash instead. Every other trade keeps the
                    // frontend-derived realised percentage.
                    const isRollRow =
                      (typeof tr.entry_block_id === 'string'
                        && tr.entry_block_id.startsWith('roll:'))
                      || 'segment_pnl' in tr
                      || typeof tr.roll_hover === 'string';
                    const hasSegmentPnl =
                      typeof tr.segment_pnl === 'number' && Number.isFinite(tr.segment_pnl);
                    const pnl = hasSegmentPnl
                      ? tr.segment_pnl
                      : isRollRow
                        ? null
                        : computePnl(tr._openPrice, tr._pnlClosePrice, tr.signed_weight);
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
                        <td>
                          {isOptionCloseInput(tr) ? (
                            <span
                              className={styles.reason}
                              title={CLOSE_INPUT_HINT}
                              data-testid="input-close-hint"
                            >
                              {tr.input_id}
                            </span>
                          ) : (
                            tr.input_id
                          )}
                        </td>
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
                        <td data-testid="trade-open-price">
                          {formatPrice(tr._openPrice)}
                          {tr.open_price_fallback && Number.isFinite(tr._openPrice) && (
                            <FallbackMark which="open" />
                          )}
                        </td>
                        <td data-testid="trade-close-price">
                          {isClosed ? (
                            <>
                              {formatPrice(tr._closePrice)}
                              {tr.close_price_fallback && Number.isFinite(tr._closePrice) && (
                                <FallbackMark which="close" />
                              )}
                            </>
                          ) : (
                            <span className={styles.openTag}>—</span>
                          )}
                        </td>
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
