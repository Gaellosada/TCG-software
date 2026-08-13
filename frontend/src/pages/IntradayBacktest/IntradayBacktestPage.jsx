import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import { formatCurrency, formatNumber, formatPercent } from '../../utils/format';
import {
  getIntradayBacktestMeta,
  startIntradayBacktest,
  getIntradayBacktestProgress,
} from '../../api/intradayBacktest';
import styles from './IntradayBacktestPage.module.css';

// Progress poll cadence (ms). Kept small so the "X / N days" readout tracks the
// backend job closely without hammering it.
const POLL_INTERVAL_MS = 400;

// ---------------------------------------------------------------------------
// Intraday Options Backtesting page (v1).
//
// One round-trip per trading day: open an ATM straddle at T1 (ET), delta-hedge
// with the ES future on an interval + delta-band, close at T2. Flat overnight.
// Controls feed POST /api/intraday-backtest/run; the window + roots come from
// GET /api/intraday-backtest/meta. Wire shapes are PINNED in DESIGN.md.
//
// The aggregate panel renders the backend's already-computed stats directly
// (sharpe / max_drawdown_usd / win_rate). We deliberately do NOT reuse the
// shared ``Statistics`` component here: it expects an EQUITY curve (capital
// base) to derive percentage returns, but our series is a cumulative P&L in
// dollars that starts near zero — percentage returns would divide by ~0 and be
// meaningless. The equity curve is still rendered through the shared ``Chart``
// component so CSV export + theming come for free.
// ---------------------------------------------------------------------------

const DEFAULT_FORM = {
  start_date: '',
  end_date: '',
  entry_time: '10:00',
  exit_time: '15:45',
  expiry_mode: '0DTE',
  dte: 0,
  straddle_side: 'long',
  hedge_enabled: true,
  interval_minutes: 15,
  delta_band: 0.10,
  snap_tolerance_minutes: 10,
};

function clampToWindow(value, win) {
  if (!value || !win) return value;
  if (win.min_date && value < win.min_date) return win.min_date;
  if (win.max_date && value > win.max_date) return win.max_date;
  return value;
}

function signClass(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  if (v > 0) return styles.positive;
  if (v < 0) return styles.negative;
  return '';
}

// In-app help for the snap-tolerance field (Gael copy). Surfaced via an ⓘ
// affordance next to the label, matching the app's tooltip pattern.
const SNAP_HELP = 'Intraday option quotes are sparse. If your exact entry/exit '
  + 'minute has no quote, the engine uses the nearest one within this many '
  + 'minutes; if none exists in that window, the day is skipped. Higher = fewer '
  + 'skipped days but looser fills; lower = tighter timing but more skips.';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];

// Compact USD for the tight calendar cells: whole dollars with a sign, e.g.
// -$228 / $100. Full precision stays in the cell tooltip.
function compactUsd(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  const sign = v < 0 ? '-' : '';
  return `${sign}$${Math.round(Math.abs(v)).toLocaleString('en-US')}`;
}

// Group the flat days list into calendar months, laid out on a Mon–Fri
// (trading-week) grid. Each month is an ordered list of "weeks"; every week is
// exactly 5 slots (Mon..Fri). A slot is null (alignment blank) or { dom, iso,
// data } where data is the matching day record or null for an in-range weekday
// with no result (e.g. a market holiday). This is what lets a reader situate a
// day under the correct weekday at a glance.
function groupDaysByMonth(days) {
  const byMonth = new Map();
  for (const d of days) {
    if (!d || !d.date) continue;
    const key = d.date.slice(0, 7); // YYYY-MM
    if (!byMonth.has(key)) byMonth.set(key, new Map());
    byMonth.get(key).set(d.date, d);
  }

  return [...byMonth.keys()].sort().map((key) => {
    const [year, month] = key.split('-').map(Number);
    const label = new Date(Date.UTC(year, month - 1, 1))
      .toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
    const dayMap = byMonth.get(key);
    const lastDom = new Date(Date.UTC(year, month, 0)).getUTCDate();

    const weeks = [];
    let week = [null, null, null, null, null];
    let filled = false;
    for (let dom = 1; dom <= lastDom; dom += 1) {
      const dow = new Date(Date.UTC(year, month - 1, dom)).getUTCDay(); // 0 Sun..6 Sat
      if (dow === 0 || dow === 6) continue; // weekends are not columns
      const col = dow - 1; // Mon=0 .. Fri=4
      // A new Monday starts a fresh week once the current one has any content.
      if (col === 0 && filled) {
        weeks.push(week);
        week = [null, null, null, null, null];
        filled = false;
      }
      const iso = `${key}-${String(dom).padStart(2, '0')}`;
      week[col] = { dom, iso, data: dayMap.get(iso) || null };
      filled = true;
    }
    if (filled) weeks.push(week);

    return { key, label, weeks };
  });
}

function dayOutcome(data) {
  if (!data) return 'gap';
  // Excluded (user opted the day out) is a distinct, neutral outcome — not a
  // data-gap "skipped". The backend tags it status="excluded"; we also accept
  // skip_reason="excluded" for robustness against wire variance.
  if (data.status === 'excluded' || data.skip_reason === 'excluded') return 'excluded';
  if (data.status === 'skipped') return 'skipped';
  const usd = data.pnl ? data.pnl.total_pnl_usd : null;
  if (typeof usd === 'number' && Number.isFinite(usd)) {
    if (usd > 0) return 'profit';
    if (usd < 0) return 'loss';
  }
  return 'flat';
}

const OUTCOME_CLASS = {
  profit: styles.cellProfit,
  loss: styles.cellLoss,
  flat: styles.cellFlat,
  skipped: styles.cellSkipped,
  excluded: styles.cellExcluded,
};

// Full detail for a cell's tooltip — preserves everything the old table row
// showed (status, strike, option/hedge P&L, USD, skip reason).
function cellTitle(iso, data) {
  if (!data) return `${iso} — no data (non-trading day)`;
  if (data.status === 'excluded' || data.skip_reason === 'excluded') {
    return `${iso} — excluded (no trade)`;
  }
  if (data.status === 'skipped') {
    return `${iso} — skipped: ${data.skip_reason || 'skipped'}`;
  }
  const p = data.pnl || {};
  const parts = [
    `${iso} — ${data.status}`,
    data.strike != null ? `strike ${formatNumber(data.strike, 0)}` : null,
    p.option_pnl_pts != null ? `option ${formatNumber(p.option_pnl_pts)} pts` : null,
    p.hedge_pnl_pts != null ? `hedge ${formatNumber(p.hedge_pnl_pts)} pts` : null,
    p.total_pnl_usd != null ? `Day P&L ${formatCurrency(p.total_pnl_usd)}` : null,
  ].filter(Boolean);
  return parts.join('  •  ');
}

export default function IntradayBacktestPage() {
  const [meta, setMeta] = useState(null);
  const [metaError, setMetaError] = useState(null);

  const [form, setForm] = useState(DEFAULT_FORM);
  // Unified "Custom days" control (supersedes the old exception_dates +
  // date_overrides). Each row fully describes one date: exclude it, or override
  // its entry/exit times. Shape: { date, exclude, entry_time, exit_time }.
  const [customDays, setCustomDays] = useState([]);
  const [customDayDraft, setCustomDayDraft] = useState('');

  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);
  const [progress, setProgress] = useState(null); // { days_done, total_days }

  // Poll bookkeeping: the active interval id and an in-flight guard so a slow
  // poll can never overlap the next tick.
  const pollRef = useRef(null);
  const inFlightRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    inFlightRef.current = false;
  }, []);

  // Clear any live poll if the page unmounts mid-run.
  useEffect(() => stopPolling, [stopPolling]);

  // Load meta (window / roots) once on mount and seed the default date range.
  useEffect(() => {
    const controller = new AbortController();
    getIntradayBacktestMeta({ signal: controller.signal })
      .then((m) => {
        setMeta(m);
        setForm((f) => ({
          ...f,
          start_date: f.start_date || (m.window ? m.window.min_date : ''),
          end_date: f.end_date || (m.window ? m.window.max_date : ''),
        }));
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setMetaError(err && err.message ? err.message : 'Failed to load backtest metadata.');
      });
    return () => controller.abort();
  }, []);

  const win = meta ? meta.window : null;
  const expiryModes = (meta && meta.expiry_modes) || ['0DTE', 'NDTE'];

  const setField = useCallback((key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
  }, []);

  // Add a custom-day row for the drafted date. New rows default to "not
  // excluded" and seed their times from the current default entry/exit so the
  // user only has to change what differs.
  const addCustomDay = useCallback(() => {
    const d = customDayDraft;
    if (!d) return;
    setCustomDays((prev) => {
      if (prev.some((c) => c.date === d)) return prev; // no duplicates
      const row = {
        date: d,
        exclude: false,
        entry_time: form.entry_time,
        exit_time: form.exit_time,
      };
      return [...prev, row].sort((a, b) => a.date.localeCompare(b.date));
    });
    setCustomDayDraft('');
  }, [customDayDraft, form.entry_time, form.exit_time]);

  const updateCustomDay = useCallback((date, patch) => {
    setCustomDays((prev) => prev.map((c) => (c.date === date ? { ...c, ...patch } : c)));
  }, []);

  const removeCustomDay = useCallback((date) => {
    setCustomDays((prev) => prev.filter((c) => c.date !== date));
  }, []);

  // Build the PINNED request payload (DESIGN.md).
  const buildPayload = useCallback(() => ({
    start_date: form.start_date,
    end_date: form.end_date,
    entry_time: form.entry_time,
    exit_time: form.exit_time,
    expiry_mode: form.expiry_mode,
    dte: Number(form.dte) || 0,
    straddle_side: form.straddle_side,
    hedge: {
      enabled: Boolean(form.hedge_enabled),
      interval_minutes: Number(form.interval_minutes),
      delta_band: Number(form.delta_band),
    },
    snap_tolerance_minutes: Number(form.snap_tolerance_minutes),
    // Unified custom-days payload (DESIGN.md PIN): excluded rows carry only the
    // date; override rows carry their entry/exit times.
    custom_days: customDays.map((c) => (c.exclude
      ? { date: c.date, exclude: true }
      : { date: c.date, exclude: false, entry_time: c.entry_time, exit_time: c.exit_time })),
  }), [form, customDays]);

  const runDisabledReason = useMemo(() => {
    if (running) return 'Running…';
    if (!form.start_date || !form.end_date) return 'Pick a date range';
    if (form.end_date < form.start_date) return 'End date is before start date';
    if (form.exit_time <= form.entry_time) return 'Exit time must be after entry time';
    return null;
  }, [running, form]);

  // Async run: start a background job, then poll its progress until done/error.
  // Validation failures (400) surface synchronously from the start call.
  const onRun = useCallback(async () => {
    setRunError(null);
    setResult(null);
    setProgress({ days_done: 0, total_days: 0 });
    setRunning(true);

    const fail = (err) => {
      stopPolling();
      setRunError(err && err.message ? err.message : 'Backtest failed.');
      setRunning(false);
      setProgress(null);
    };

    let jobId;
    try {
      const started = await startIntradayBacktest(buildPayload());
      jobId = started && started.job_id;
    } catch (err) {
      fail(err);
      return;
    }
    if (!jobId) {
      fail(new Error('Backtest failed to start.'));
      return;
    }

    pollRef.current = setInterval(async () => {
      if (inFlightRef.current) return; // guard against overlapping polls
      inFlightRef.current = true;
      try {
        const p = await getIntradayBacktestProgress(jobId);
        setProgress({ days_done: p.days_done, total_days: p.total_days });
        if (p.status === 'done') {
          stopPolling();
          setResult(p.result);
          setRunning(false);
          setProgress(null);
        } else if (p.status === 'error') {
          stopPolling();
          setRunError(p.error || 'Backtest failed.');
          setRunning(false);
          setProgress(null);
        }
      } catch (err) {
        fail(err);
      } finally {
        inFlightRef.current = false;
      }
    }, POLL_INTERVAL_MS);
  }, [buildPayload, stopPolling]);

  // Equity-curve trace for the shared Chart (cumulative P&L in USD).
  const equityTraces = useMemo(() => {
    const curve = result && result.aggregate && result.aggregate.equity_curve;
    if (!Array.isArray(curve) || curve.length === 0) return null;
    return [{
      x: curve.map((p) => p.date),
      y: curve.map((p) => p.cum_pnl_usd),
      type: 'scatter',
      mode: 'lines',
      name: 'Cumulative P&L (USD)',
      line: { width: 2 },
    }];
  }, [result]);

  const agg = result ? result.aggregate : null;
  const days = (result && result.days) || [];
  const warnings = (result && result.warnings) || [];
  const months = useMemo(() => groupDaysByMonth(days), [days]);

  if (metaError) {
    return (
      <div className={styles.page}>
        <div className={styles.error} role="alert">{metaError}</div>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <h1>Intraday Options Backtesting</h1>
        <p>
          One round-trip per trading day — open an ATM straddle at the entry time,
          delta-hedge with the {meta ? meta.hedge_instrument : 'ES'} future, close at the exit time.
          All times are Eastern (ET).
        </p>
      </header>

      <Card title="Parameters" bodyClassName={styles.cardBody}>
        <div className={styles.controlsGrid}>
          {/* Date range — bounded by the /meta window. */}
          <label className={styles.field}>
            <span>Start date (ET)</span>
            <input
              type="date"
              aria-label="Start date"
              value={form.start_date}
              min={win ? win.min_date : undefined}
              max={win ? win.max_date : undefined}
              onChange={(e) => setField('start_date', clampToWindow(e.target.value, win))}
            />
          </label>
          <label className={styles.field}>
            <span>End date (ET)</span>
            <input
              type="date"
              aria-label="End date"
              value={form.end_date}
              min={win ? win.min_date : undefined}
              max={win ? win.max_date : undefined}
              onChange={(e) => setField('end_date', clampToWindow(e.target.value, win))}
            />
          </label>

          {/* Entry / exit times (ET). */}
          <label className={styles.field}>
            <span>Entry time (ET)</span>
            <input
              type="time"
              aria-label="Entry time (ET)"
              value={form.entry_time}
              onChange={(e) => setField('entry_time', e.target.value)}
            />
          </label>
          <label className={styles.field}>
            <span>Exit time (ET)</span>
            <input
              type="time"
              aria-label="Exit time (ET)"
              value={form.exit_time}
              onChange={(e) => setField('exit_time', e.target.value)}
            />
          </label>

          {/* Expiry mode + DTE. */}
          <label className={styles.field}>
            <span>Expiry mode</span>
            <select
              aria-label="Expiry mode"
              value={form.expiry_mode}
              onChange={(e) => setField('expiry_mode', e.target.value)}
            >
              {expiryModes.map((m) => (
                <option key={m} value={m}>{m === '0DTE' ? '0DTE (same-day expiry)' : `${m} (N days to expiry)`}</option>
              ))}
            </select>
          </label>
          {form.expiry_mode !== '0DTE' && (
            <label className={styles.field}>
              <span>Days to expiry (DTE)</span>
              <input
                type="number"
                min={0}
                aria-label="Days to expiry"
                value={form.dte}
                onChange={(e) => setField('dte', e.target.value)}
              />
            </label>
          )}

          {/* Straddle side. */}
          <label className={styles.field}>
            <span>Straddle side</span>
            <select
              aria-label="Straddle side"
              value={form.straddle_side}
              onChange={(e) => setField('straddle_side', e.target.value)}
            >
              <option value="long">Long (pay premium)</option>
              <option value="short">Short (collect premium)</option>
            </select>
          </label>

          {/* Snap tolerance. */}
          <label className={styles.field}>
            <span className={styles.labelRow}>
              Snap tolerance (min)
              <span
                className={styles.help}
                data-testid="snap-help"
                role="img"
                aria-label={SNAP_HELP}
                title={SNAP_HELP}
              >
                ⓘ
              </span>
            </span>
            <input
              type="number"
              min={0}
              aria-label="Snap tolerance (minutes)"
              value={form.snap_tolerance_minutes}
              onChange={(e) => setField('snap_tolerance_minutes', e.target.value)}
            />
          </label>

          {/* Delta hedge. */}
          <label className={`${styles.field} ${styles.checkboxRow}`}>
            <input
              type="checkbox"
              aria-label="Delta-hedge enabled"
              checked={form.hedge_enabled}
              onChange={(e) => setField('hedge_enabled', e.target.checked)}
            />
            <span>Delta-hedge</span>
          </label>
          <label className={styles.field}>
            <span>Hedge interval (min)</span>
            <input
              type="number"
              min={1}
              aria-label="Hedge interval minutes"
              value={form.interval_minutes}
              disabled={!form.hedge_enabled}
              onChange={(e) => setField('interval_minutes', e.target.value)}
            />
          </label>
          <label className={styles.field}>
            <span>Delta band</span>
            <input
              type="number"
              step="0.01"
              min={0}
              aria-label="Delta band"
              value={form.delta_band}
              disabled={!form.hedge_enabled}
              onChange={(e) => setField('delta_band', e.target.value)}
            />
          </label>
        </div>

        {/* Custom days — one unified control. Add a date, then per row either
            exclude it (no trade) or override its entry/exit times. */}
        <div className={styles.field} style={{ marginTop: '1rem' }}>
          <span className={styles.fieldLabel}>Custom days (exclude or override times)</span>
          <div className={styles.inlineRow}>
            <input
              type="date"
              aria-label="Custom day date"
              data-testid="custom-day-input"
              min={win ? win.min_date : undefined}
              max={win ? win.max_date : undefined}
              value={customDayDraft}
              onChange={(e) => setCustomDayDraft(e.target.value)}
            />
            <button
              type="button"
              className={styles.smallBtn}
              data-testid="add-custom-day"
              onClick={addCustomDay}
            >
              Add custom day
            </button>
          </div>
          {customDays.length > 0 && (
            <ul className={styles.customDaysList} data-testid="custom-days-list">
              {customDays.map((c) => (
                <li
                  key={c.date}
                  className={styles.customDayRow}
                  data-testid={`custom-day-row-${c.date}`}
                >
                  <span className={styles.customDayDate}>{c.date}</span>
                  <label className={styles.customDayExclude}>
                    <input
                      type="checkbox"
                      aria-label={`Exclude ${c.date}`}
                      checked={c.exclude}
                      onChange={(e) => updateCustomDay(c.date, { exclude: e.target.checked })}
                    />
                    <span>Exclude (don&apos;t trade)</span>
                  </label>
                  <input
                    type="time"
                    aria-label={`Entry time for ${c.date}`}
                    className={styles.customDayTime}
                    value={c.entry_time}
                    disabled={c.exclude}
                    onChange={(e) => updateCustomDay(c.date, { entry_time: e.target.value })}
                  />
                  <span className={styles.customDayDash} aria-hidden="true">–</span>
                  <input
                    type="time"
                    aria-label={`Exit time for ${c.date}`}
                    className={styles.customDayTime}
                    value={c.exit_time}
                    disabled={c.exclude}
                    onChange={(e) => updateCustomDay(c.date, { exit_time: e.target.value })}
                  />
                  <span className={styles.customDayEt} aria-hidden="true">ET</span>
                  <button
                    type="button"
                    className={styles.chipRemove}
                    aria-label={`Remove custom day ${c.date}`}
                    onClick={() => removeCustomDay(c.date)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className={styles.runRow} style={{ marginTop: '1rem' }}>
          <button
            type="button"
            className={styles.runBtn}
            disabled={Boolean(runDisabledReason)}
            title={runDisabledReason || undefined}
            onClick={onRun}
          >
            {running ? 'Running…' : 'Run backtest'}
          </button>
          {runDisabledReason && !running && (
            <span className={styles.statLabel}>{runDisabledReason}</span>
          )}
          {runError && <span className={styles.error} role="alert">{runError}</span>}
        </div>

        {running && progress && (
          <div className={styles.progressBox} data-testid="run-progress" role="status">
            <span className={styles.progressText}>
              Running… {progress.days_done} / {progress.total_days} days
            </span>
            <div
              className={styles.progressTrack}
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={progress.total_days || 0}
              aria-valuenow={progress.days_done}
            >
              <div
                className={styles.progressFill}
                data-testid="run-progress-fill"
                style={{
                  width: `${progress.total_days > 0
                    ? Math.min(100, (progress.days_done / progress.total_days) * 100)
                    : 0}%`,
                }}
              />
            </div>
          </div>
        )}
      </Card>

      {warnings.length > 0 && (
        <div className={styles.warnings} data-testid="warnings" role="status">
          <strong>Warnings</strong>
          <ul>
            {warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}

      {agg && (
        <Card
          title="Aggregate"
          className={styles.resultCard}
          bodyClassName={styles.cardBody}
          data-result-card="true"
        >
          <div className={styles.statsGrid} data-testid="aggregate-stats">
            <div className={styles.stat}>
              <span className={styles.statLabel}>Days</span>
              <span className={styles.statValue}>{agg.n_days}</span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Traded</span>
              <span className={styles.statValue}>{agg.n_traded}</span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Skipped</span>
              <span className={styles.statValue}>{agg.n_skipped}</span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Total P&L</span>
              <span className={`${styles.statValue} ${signClass(agg.total_pnl_usd)}`}>
                {formatCurrency(agg.total_pnl_usd)}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Mean daily P&L</span>
              <span className={`${styles.statValue} ${signClass(agg.mean_daily_pnl_usd)}`}>
                {formatCurrency(agg.mean_daily_pnl_usd)}
              </span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Win rate</span>
              <span className={styles.statValue}>{formatPercent(agg.win_rate)}</span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Sharpe</span>
              <span className={styles.statValue}>{formatNumber(agg.sharpe)}</span>
            </div>
            <div className={styles.stat}>
              <span className={styles.statLabel}>Max drawdown</span>
              <span className={`${styles.statValue} ${signClass(agg.max_drawdown_usd)}`}>
                {formatCurrency(agg.max_drawdown_usd)}
              </span>
            </div>
          </div>
        </Card>
      )}

      {equityTraces && (
        <Card
          title="Equity curve (cumulative P&L)"
          className={styles.resultCard}
          bodyClassName={styles.cardBody}
          data-result-card="true"
        >
          <div className={styles.chartBox}>
            {/* Fill the fixed-height chartBox so Plotly sizes to it (height:100%
                resolves against 340px) instead of falling back to its default
                450px, which the Card's overflow:hidden would clip at the bottom
                axis. */}
            <Chart
              traces={equityTraces}
              style={{ width: '100%', height: '100%' }}
              downloadFilename="intraday-backtest-equity"
              layoutOverrides={{ yaxis: { title: 'Cumulative P&L (USD)' } }}
            />
          </div>
        </Card>
      )}

      {days.length > 0 && (
        <Card
          title="Backtest days"
          className={styles.resultCard}
          bodyClassName={styles.cardBody}
          data-result-card="true"
        >
          <div className={styles.calendar} data-testid="days-grid">
            {months.map((m) => (
              <div className={styles.monthBlock} data-testid="month-block" key={m.key}>
                <div className={styles.monthHeader}>{m.label}</div>
                <div className={styles.weekdayRow} aria-hidden="true">
                  {WEEKDAYS.map((w) => (
                    <span className={styles.weekday} key={w}>{w}</span>
                  ))}
                </div>
                <div className={styles.monthGrid}>
                  {m.weeks.flatMap((week, wi) => week.map((slot, ci) => {
                    // Alignment blank (leading pad before the month's first weekday).
                    if (!slot) {
                      return <div className={styles.emptyCell} key={`${wi}-${ci}`} aria-hidden="true" />;
                    }
                    // In-range weekday with no result — a non-trading gap (holiday).
                    if (!slot.data) {
                      return (
                        <div
                          className={styles.gapCell}
                          key={slot.iso}
                          title={cellTitle(slot.iso, null)}
                        >
                          {slot.dom}
                        </div>
                      );
                    }
                    const outcome = dayOutcome(slot.data);
                    const excluded = outcome === 'excluded';
                    const noTrade = outcome === 'skipped' || excluded;
                    const pnl = slot.data.pnl || null;
                    return (
                      <div
                        key={slot.iso}
                        className={`${styles.dayCell} ${OUTCOME_CLASS[outcome] || ''}`}
                        data-testid="day-cell"
                        data-date={slot.iso}
                        data-status={slot.data.status}
                        data-outcome={outcome}
                        title={cellTitle(slot.iso, slot.data)}
                      >
                        <span className={styles.dom}>{slot.dom}</span>
                        {noTrade ? (
                          <span className={styles.cellTag}>
                            {excluded ? 'no trade' : 'skipped'}
                          </span>
                        ) : (
                          <span className={styles.cellPnl}>
                            {pnl ? compactUsd(pnl.total_pnl_usd) : '—'}
                          </span>
                        )}
                      </div>
                    );
                  }))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
