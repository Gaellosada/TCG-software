import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import { formatCurrency, formatNumber, formatPercent } from '../../utils/format';
import {
  getIntradayBacktestMeta,
  getIntradayEventCalendar,
  startIntradayBacktest,
  getIntradayBacktestProgress,
  getIntradayBacktestCachedResult,
} from '../../api/intradayBacktest';
import {
  DEFAULT_FORM,
  ALLOWLIST_EVENT_TYPES,
  serializeConfig,
  deepEqual,
  listSims,
  saveSim,
  loadSim,
  deleteSim,
} from './storage';
import EntryExitModule, {
  defaultEntryModule,
  defaultExitModule,
  emptyPartialModule,
  serializeModule,
  serializePartialModule,
} from './EntryExitModule';
import HedgeModule, {
  defaultHedgeModule,
  serializeHedge,
  hedgeTriggersAllOff,
  isBlank,
} from './HedgeModule';
import WeekdayAttributionView from './WeekdayAttributionView';
import RegimeSensitivityView from './RegimeSensitivityView';
import EventAttributionView from './EventAttributionView';
import LadderEntriesView from './LadderEntriesView';
import styles from './IntradayBacktestPage.module.css';

// Progress poll cadence (ms). Kept small so the "X / N days" readout tracks the
// backend job closely without hammering it.
const POLL_INTERVAL_MS = 400;

// A single transient /progress failure (502/timeout/network blip) must NOT tear
// down a run whose backend job is still alive: tolerate this many CONSECUTIVE
// poll errors before giving up (any successful poll resets the count).
const MAX_CONSECUTIVE_POLL_ERRORS = 4;

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

// The flat entry_time / exit_time / snap_tolerance_minutes are gone (DESIGN.md
// v2), and the flat hedge fields are gone (DESIGN.md v4). Entry, Exit and Hedge
// are now full "rule modules" held in their own state; the form keeps only the
// scalar params below. DEFAULT_FORM now lives in ``storage.js`` (single source
// of truth for both the seed and the load-time sanitiser).

// Build the PINNED v2 request payload (DESIGN.md) from a config snapshot. Pure
// (module scope) so it can be called with EITHER the live page state (via
// buildPayload) OR a just-loaded saved config, without waiting for a setState
// to flush. Entry/exit are objects { time, snap_tolerance_minutes, conditions };
// custom_days carry full per-day overrides.
function buildRunPayload({ form, hedge, entry, exit, customDays }) {
  const payload = {
    start_date: form.start_date,
    end_date: form.end_date,
    expiry_mode: form.expiry_mode,
    dte: Number(form.dte) || 0,
    straddle_side: form.straddle_side,
    cost: {
      enabled: Boolean(form.cost_enabled),
      fallback_cost_pts: Number(form.cost_fallback_pts) || 0,
    },
    hedge: serializeHedge(hedge),
    entry: serializeModule(entry),
    exit: serializeModule(exit),
    custom_days: customDays.map((c) => {
      if (c.exclude) return { date: c.date, exclude: true };
      const out = { date: c.date };
      const e = serializePartialModule(c.entry);
      const x = serializePartialModule(c.exit);
      if (e) out.entry = e;
      if (x) out.exit = x;
      return out;
    }),
  };
  // F2.2: only attach the regime block when regime-driven side is enabled, so a
  // default (regime-off) payload stays BYTE-IDENTICAL to the pre-feature body
  // (the backend defaults regime to off; an absent block hashes identically).
  if (form.regime_side_enabled) {
    payload.regime = {
      side_mode: 'regime_driven',
      hvol_tolerance: Number(form.regime_hvol_tolerance) || 0,
      extremely_low_h20: Number(form.regime_extremely_low_h20) || 0,
      gates: form.regime_vvix_gate_enabled
        ? [{
            enabled: true,
            signal: 'vvix',
            above: Number(form.regime_vvix_gate_level) || 0,
            action: 'flat',
          }]
        : [],
    };
  }
  // F3.2: only attach the allowlist block when the date-allowlist mode is on, so
  // a default payload stays BYTE-IDENTICAL to the pre-feature body (the backend
  // defaults allowlist to off; an absent block hashes identically).
  if (form.allowlist_enabled) {
    payload.allowlist = {
      mode: 'allowlist',
      dates: [...(form.allowlist_dates || [])],
      event_types: [...(form.allowlist_event_types || [])],
    };
  }
  // F4.1: only attach the ladder block when enabled, so a default payload stays
  // BYTE-IDENTICAL to the pre-feature body (the backend defaults ladder to off;
  // an absent block hashes identically). Blank time fields map to null (backend
  // default = the entry / exit time).
  if (form.ladder_enabled) {
    payload.ladder = {
      enabled: true,
      interval_minutes: Number(form.ladder_interval_minutes) || 30,
      first_entry: form.ladder_first_entry ? form.ladder_first_entry : null,
      last_entry_cutoff: form.ladder_last_entry_cutoff
        ? form.ladder_last_entry_cutoff : null,
      max_concurrent: Number(form.ladder_max_concurrent) || 0,
      sizing: {
        mode: form.ladder_sizing_mode,
        contracts: Number(form.ladder_contracts) || 1,
        notional_per_entry_usd: Number(form.ladder_notional_per_entry_usd) || 0,
      },
    };
  }
  return payload;
}

// Short, safe display of the ISO savedAt timestamp for a saved-sim row.
function formatSavedAt(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleString();
  } catch {
    return '';
  }
}

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
  // Entry conditions never qualified (v2): a distinct skip outcome from a raw
  // data gap. Accept it on either status or skip_reason.
  if (data.status === 'entry_conditions_unmet' || data.skip_reason === 'entry_conditions_unmet') {
    return 'unmet';
  }
  // Regime decided FLAT (F2.2): a deliberate no-trade (side=flat), distinct from
  // a data-gap skip. Backend tags status="skipped", skip_reason="regime_flat".
  if (data.skip_reason === 'regime_flat') return 'regime_flat';
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
  unmet: styles.cellUnmet,
  // Regime flat reuses the neutral excluded styling (a deliberate no-trade).
  regime_flat: styles.cellExcluded,
};

// A YYYYMMDD int (the regime ``asof`` date) rendered as an ISO date, or ''.
function isoFromInt(dateInt) {
  if (typeof dateInt !== 'number' || !Number.isFinite(dateInt)) return '';
  const s = String(dateInt);
  if (s.length !== 8) return '';
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

// One-line regime-decision summary for a day (F2.2): the chosen side, why
// (state), any firing gate, and the as-of signal date the decision used.
// ``null`` when the day carries no regime decision (regime-driven side off).
function regimeSummary(data) {
  const r = data && data.regime;
  if (!r || typeof r !== 'object' || !r.side) return null;
  const gate = r.gate ? `, gate ${r.gate}` : '';
  const asof = r.asof ? `, as-of ${isoFromInt(r.asof)}` : '';
  return `regime: ${r.side} (${r.state}${gate}${asof})`;
}

// Short HH:MM (UTC) extracted from an ISO timestamp for the compact tooltip.
function tsTime(ts) {
  if (!ts) return '—';
  const m = /T(\d{2}:\d{2})/.exec(String(ts));
  return m ? `${m[1]}Z` : String(ts);
}

// One-line per-leg fill summary for the day tooltip (entry/exit ts + price and
// whether the exit conditions were actually met vs a nearest-bar fallback).
function legSummary(name, leg) {
  if (!leg) return null;
  const inTxt = `in ${tsTime(leg.entry_ts)} @${formatNumber(leg.entry_price)}`;
  const outTxt = `out ${tsTime(leg.exit_ts)} @${formatNumber(leg.exit_price)}`;
  const met = leg.exit_conditions_met === false ? 'exit=fallback' : 'exit=ok';
  return `${name}: ${inTxt} → ${outTxt} (${met})`;
}

// One-line early-exit summary for the day tooltip (v3). `exit_trigger` is the
// firing trigger { type, ts, value } or null (null = normal time exit).
function triggerSummary(trig) {
  if (!trig || !trig.type) return null;
  return `exited early: ${trig.type} @ ${tsTime(trig.ts)}`;
}

// Full detail for a cell's tooltip — preserves everything the old table row
// showed (status, strike, option/hedge P&L, USD, skip reason).
function cellTitle(iso, data) {
  if (!data) return `${iso} — no data (non-trading day)`;
  if (data.status === 'excluded' || data.skip_reason === 'excluded') {
    return `${iso} — excluded (no trade)`;
  }
  if (data.status === 'entry_conditions_unmet' || data.skip_reason === 'entry_conditions_unmet') {
    return `${iso} — skipped: entry_conditions_unmet (no bar met the entry conditions in the snap window)`;
  }
  if (data.skip_reason === 'regime_flat') {
    const rs = regimeSummary(data);
    return `${iso} — flat (regime)${rs ? `  •  ${rs}` : ''}`;
  }
  if (data.status === 'skipped') {
    return `${iso} — skipped: ${data.skip_reason || 'skipped'}`;
  }
  const p = data.pnl || {};
  const legs = data.legs || null;
  const parts = [
    `${iso} — ${data.status}`,
    regimeSummary(data),
    data.strike != null ? `strike ${formatNumber(data.strike, 0)}` : null,
    p.option_pnl_pts != null ? `option ${formatNumber(p.option_pnl_pts)} pts` : null,
    p.hedge_pnl_pts != null ? `hedge ${formatNumber(p.hedge_pnl_pts)} pts` : null,
    p.total_pnl_usd != null ? `Day P&L ${formatCurrency(p.total_pnl_usd)}` : null,
    // Per-leg fills (v2 independent legs) — surfaces the asymmetric quote
    // arrival so the leg-timing gap is visible.
    legs ? legSummary('call', legs.call) : null,
    legs ? legSummary('put', legs.put) : null,
    // Early-exit trigger (v3) — which trigger fired, and when.
    triggerSummary(data.exit_trigger),
  ].filter(Boolean);
  return parts.join('  •  ');
}

// Params the backend requires strictly > 0 (Field(gt=0)); a blank field
// serializes via Number('')→0 and would 422. Keyed by condition/trigger type.
const POSITIVE_CONDITION_FIELDS = {
  max_spread: [['pct', 'Max spread %']],
  min_quote_size: [['size', 'Min quote size']],
  min_premium: [['points', 'Min premium']],
  max_underlying_move: [['pct', 'Max underlying move %']],
  min_rehedge_delta: [['threshold', 'Min rehedge delta']],
};
const POSITIVE_TRIGGER_FIELDS = {
  underlying_move: [['amount', 'Underlying move amount']],
  sigma_move: [['n', 'Sigma move n']],
  net_delta: [['threshold', 'Net delta threshold']],
  pnl: [['amount', 'P&L amount']],
};

function isPositive(v) {
  return !isBlank(v) && Number(v) > 0;
}

// First blank/≤0 numeric param across the entry, exit and hedge modules (and
// their custom-day overrides), or null when all are valid. Mirrors the backend
// Field(gt=0) / ratio in (0,1] constraints so Run is blocked with a specific,
// param-naming reason instead of a raw 422.
function firstBadNumericParam(entry, exit, hedge, customDays) {
  const scanModule = (label, mod) => {
    if (!mod) return null;
    for (const c of mod.conditions || []) {
      for (const [f, fl] of POSITIVE_CONDITION_FIELDS[c.type] || []) {
        if (!isPositive(c[f])) return `${label} ${fl} must be greater than 0`;
      }
    }
    for (const t of mod.triggers || []) {
      for (const [f, fl] of POSITIVE_TRIGGER_FIELDS[t.type] || []) {
        if (!isPositive(t[f])) return `${label} ${fl} must be greater than 0`;
      }
    }
    return null;
  };

  const core = scanModule('Entry', entry) || scanModule('Exit', exit);
  if (core) return core;

  for (const c of (hedge && hedge.conditions) || []) {
    for (const [f, fl] of POSITIVE_CONDITION_FIELDS[c.type] || []) {
      if (!isPositive(c[f])) return `Hedge ${fl} must be greater than 0`;
    }
  }
  if (hedge && hedge.enabled) {
    const sig = (hedge.triggers && hedge.triggers.sigma_move) || {};
    if (sig.enabled && !isPositive(sig.n)) return 'Hedge σ-move n must be greater than 0';
    const tgt = hedge.target || {};
    if (tgt.mode === 'ratio' && (!isPositive(tgt.ratio) || Number(tgt.ratio) > 1)) {
      return 'Hedge ratio must be in (0, 1]';
    }
    // Timing gates (F1.1/F1.2) — mirror the backend Field constraints so Run is
    // blocked with a named reason rather than a raw 422. Blank F1.1 = OFF (fine).
    const tim = hedge.timing || {};
    if (!isBlank(tim.only_within_minutes_before_close)
        && !isPositive(tim.only_within_minutes_before_close)) {
      return 'Hedge "only within minutes before close" must be greater than 0';
    }
    const ext = tim.skip_near_extremum || {};
    if (ext.enabled) {
      if (!isPositive(ext.window_minutes)) {
        return 'Hedge skip-extremum window must be greater than 0';
      }
      const tol = Number(ext.tolerance);
      if (isBlank(ext.tolerance) || Number.isNaN(tol) || tol < 0) {
        return 'Hedge skip-extremum tolerance must be 0 or greater';
      }
    }
  }

  for (const c of customDays || []) {
    if (c.exclude) continue;
    const bad = scanModule(`Custom day ${c.date} entry`, c.entry)
      || scanModule(`Custom day ${c.date} exit`, c.exit);
    if (bad) return bad;
  }
  return null;
}

export default function IntradayBacktestPage() {
  const [meta, setMeta] = useState(null);
  const [metaError, setMetaError] = useState(null);

  const [form, setForm] = useState(DEFAULT_FORM);
  // Entry & Exit rule modules (v2): each = time + own snap tolerance + a
  // conditions array. Held separately from the scalar form fields.
  const [entry, setEntry] = useState(defaultEntryModule);
  const [exit, setExit] = useState(defaultExitModule);
  // Hedge rule module (v4): enable + instrument + triggers (interval, band,
  // σ-move) + conditions + target. Replaces the flat hedge fields.
  const [hedge, setHedge] = useState(defaultHedgeModule);
  // Unified "Custom days" control (supersedes the old exception_dates +
  // date_overrides). Each row: exclude the day, OR expand to override entry
  // and/or exit via the SAME EntryExitModule (partial — only set fields sent).
  // Shape: { date, exclude, expanded, entry:{partial}, exit:{partial} }.
  const [customDays, setCustomDays] = useState([]);
  const [customDayDraft, setCustomDayDraft] = useState('');
  // F3.1 curated event calendar (FOMC/NFP/CPI) for the date-allowlist control
  // (and the A3 view). Fetched once on mount; the toggle + event-type checkboxes
  // work even if this fails to load (the 3 types are a fixed enum).
  const [eventCalendar, setEventCalendar] = useState(null);
  const [allowlistDateDraft, setAllowlistDateDraft] = useState('');

  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);
  const [progress, setProgress] = useState(null); // { days_done, total_days }
  // Soft "reconnecting…" state: a transient poll error is being tolerated while
  // the backend job (presumed alive) is retried. Cleared on the next good poll.
  const [reconnecting, setReconnecting] = useState(false);

  // Saved simulations (config only) — persisted in localStorage via ./storage.
  const [savedSims, setSavedSims] = useState(() => listSims());
  const [simName, setSimName] = useState('');
  // Canonical config snapshot captured when a sim is loaded or saved. null =
  // nothing loaded → no dirty marker. The unsaved-changes indicator compares the
  // live config to this by VALUE (order-independent), never by identity.
  const [loadedSnapshot, setLoadedSnapshot] = useState(null);
  const [activeSimId, setActiveSimId] = useState(null);

  // Poll bookkeeping: the active interval id and an in-flight guard so a slow
  // poll can never overlap the next tick.
  const pollRef = useRef(null);
  const inFlightRef = useRef(false);
  // Consecutive failed polls; reset to 0 on any success. Only after
  // MAX_CONSECUTIVE_POLL_ERRORS do we treat the run as failed.
  const consecutiveErrorsRef = useRef(0);

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

  // Load the curated event calendar (F3.1) once on mount for the allowlist
  // control. Best-effort: a failure leaves eventCalendar null (the control still
  // works — the 3 event types are a fixed enum; only per-type counts are hidden).
  useEffect(() => {
    const controller = new AbortController();
    getIntradayEventCalendar({ signal: controller.signal })
      .then((c) => setEventCalendar(c))
      .catch(() => { /* non-fatal — control degrades to no counts */ });
    return () => controller.abort();
  }, []);

  const win = meta ? meta.window : null;
  const expiryModes = (meta && meta.expiry_modes) || ['0DTE', 'NDTE'];

  const setField = useCallback((key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
  }, []);

  // Add a custom-day row for the drafted date. New rows default to "not
  // excluded", collapsed, with blank (inherit) partial entry/exit overrides —
  // the user expands to override only what differs.
  const addCustomDay = useCallback(() => {
    const d = customDayDraft;
    if (!d) return;
    setCustomDays((prev) => {
      if (prev.some((c) => c.date === d)) return prev; // no duplicates
      const row = {
        date: d,
        exclude: false,
        expanded: false,
        entry: emptyPartialModule(),
        exit: emptyPartialModule(),
      };
      return [...prev, row].sort((a, b) => a.date.localeCompare(b.date));
    });
    setCustomDayDraft('');
  }, [customDayDraft]);

  const updateCustomDay = useCallback((date, patch) => {
    setCustomDays((prev) => prev.map((c) => (c.date === date ? { ...c, ...patch } : c)));
  }, []);

  const removeCustomDay = useCallback((date) => {
    setCustomDays((prev) => prev.filter((c) => c.date !== date));
  }, []);

  // Build the PINNED v2 request payload (DESIGN.md) from the live page state.
  const buildPayload = useCallback(
    () => buildRunPayload({ form, hedge, entry, exit, customDays }),
    [form, hedge, entry, exit, customDays],
  );

  // Canonical snapshot of the LIVE config — the single value the save/dirty
  // machinery compares against. Recomputed only when an input changes.
  const currentConfig = useMemo(
    () => serializeConfig({ form, entry, exit, hedge, customDays }),
    [form, entry, exit, hedge, customDays],
  );

  // Dirty when a sim is loaded/saved AND the live config differs from the
  // captured snapshot (by value — robust to key ordering / object identity).
  const isDirty = loadedSnapshot !== null && !deepEqual(currentConfig, loadedSnapshot);

  // Save (IMMEDIATE, confirmed localStorage write — never a debounce). On an
  // existing name, confirm-overwrite. After the write, re-read the list so the
  // UI reflects exactly what is persisted, and reset the dirty snapshot.
  const onSaveSim = useCallback(() => {
    const name = simName.trim();
    if (!name) return;
    const dup = savedSims.find((s) => s.name === name);
    if (dup
        && typeof window !== 'undefined'
        && typeof window.confirm === 'function'
        && !window.confirm(`Overwrite the saved simulation "${name}"?`)) {
      return;
    }
    const saved = saveSim(name, { form, entry, exit, hedge, customDays });
    setSavedSims(listSims());
    setActiveSimId(saved.id);
    setLoadedSnapshot(serializeConfig({ form, entry, exit, hedge, customDays }));
  }, [simName, savedSims, form, entry, exit, hedge, customDays]);

  // Load: restore all inputs EXACTLY from the saved config, then read-only
  // cache-get the results for the SAME payload. HIT → render instantly; MISS or
  // any error → leave results empty (the user can Run).
  const onLoadSim = useCallback(async (id) => {
    const sim = loadSim(id);
    if (!sim) return;
    const cfg = sim.config; // already sanitised by storage
    // Cancel any in-flight run before swapping the inputs out from under it.
    stopPolling();
    setRunning(false);
    setProgress(null);
    setReconnecting(false);
    setRunError(null);
    setResult(null);

    setForm(cfg.form);
    setEntry(cfg.entry);
    setExit(cfg.exit);
    setHedge(cfg.hedge);
    setCustomDays(cfg.customDays);
    setSimName(sim.name);
    setActiveSimId(sim.id);
    setLoadedSnapshot(serializeConfig(cfg));

    // Cache-get uses the payload built from the LOADED config directly (state
    // has not flushed yet). A HIT carries the full result shape (aggregate +
    // days); anything else (cached:false / missing) is a miss.
    try {
      const payload = buildRunPayload(cfg);
      const resp = await getIntradayBacktestCachedResult(payload);
      const hit = resp && resp.cached !== false
        && resp.aggregate && Array.isArray(resp.days);
      setResult(hit ? resp : null);
    } catch {
      // Cache-get is best-effort — a failure just leaves results empty.
      setResult(null);
    }
  }, [stopPolling]);

  const onDeleteSim = useCallback((id) => {
    deleteSim(id);
    setSavedSims(listSims());
    setActiveSimId((cur) => {
      if (cur === id) setLoadedSnapshot(null);
      return cur === id ? null : cur;
    });
  }, []);

  const runDisabledReason = useMemo(() => {
    if (running) return 'Running…';
    if (!form.start_date || !form.end_date) return 'Pick a date range';
    if (form.end_date < form.start_date) return 'End date is before start date';
    if (entry.time && exit.time && exit.time <= entry.time) {
      return 'Exit time must be after entry time';
    }
    // Guard (v4): a hedge that is enabled but has no armed trigger can never
    // rehedge — block Run rather than send an un-triggerable hedge.
    if (hedge.enabled && hedgeTriggersAllOff(hedge)) {
      return 'Enable at least one hedge trigger (interval, band, or σ-move)';
    }
    // Mirror the backend HedgeConfig invariant: band_edge target needs a band.
    if (hedge.enabled && hedge.target && hedge.target.mode === 'band_edge'
        && isBlank(hedge.triggers && hedge.triggers.delta_band)) {
      return 'Hedge to band edge requires a Delta band';
    }
    // Mirror the backend Field(gt=0) / ratio-in-(0,1] param constraints so a
    // blank/≤0 numeric param blocks Run with a named reason, not a raw 422.
    const badParam = firstBadNumericParam(entry, exit, hedge, customDays);
    if (badParam) return badParam;
    return null;
  }, [running, form, entry, exit, hedge, customDays]);

  // Async run: start a background job, then poll its progress until done/error.
  // Validation failures (400) surface synchronously from the start call.
  const onRun = useCallback(async () => {
    setRunError(null);
    setResult(null);
    setProgress({ days_done: 0, total_days: 0 });
    setReconnecting(false);
    consecutiveErrorsRef.current = 0;
    setRunning(true);

    const fail = (err) => {
      stopPolling();
      setRunError(err && err.message ? err.message : 'Backtest failed.');
      setRunning(false);
      setProgress(null);
      setReconnecting(false);
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
        // A good poll: clear any transient-error state and reset the counter.
        consecutiveErrorsRef.current = 0;
        setReconnecting(false);
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
        // Tolerate transient failures: the backend job keeps running, so a
        // single blip must not discard it. Only give up after N consecutive
        // misses; meanwhile show a soft "reconnecting…" state and keep polling.
        consecutiveErrorsRef.current += 1;
        if (consecutiveErrorsRef.current >= MAX_CONSECUTIVE_POLL_ERRORS) {
          fail(err);
        } else {
          setReconnecting(true);
        }
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

      <Card title="Saved simulations" bodyClassName={styles.cardBody}>
        <div className={styles.saveRow}>
          <input
            type="text"
            className={styles.simNameInput}
            aria-label="Simulation name"
            data-testid="sim-name-input"
            placeholder="Name this simulation"
            value={simName}
            onChange={(e) => setSimName(e.target.value)}
          />
          <button
            type="button"
            className={styles.smallBtn}
            data-testid="save-sim"
            disabled={!simName.trim()}
            onClick={onSaveSim}
          >
            Save
          </button>
          {loadedSnapshot !== null && (
            <span
              className={isDirty ? styles.dirtyIndicator : styles.cleanIndicator}
              data-testid="dirty-indicator"
              data-dirty={isDirty ? 'true' : 'false'}
              title={isDirty
                ? 'Unsaved changes since the loaded simulation'
                : 'No changes since the loaded simulation'}
            >
              {isDirty ? '● Unsaved changes' : '✓ Saved'}
            </span>
          )}
        </div>
        {savedSims.length === 0 ? (
          <p className={styles.conditionsEmpty} data-testid="saved-sims-empty">
            No saved simulations yet — configure the parameters below, name it, and Save.
          </p>
        ) : (
          <ul className={styles.savedSimsList} data-testid="saved-sims-list">
            {savedSims.map((s) => (
              <li
                key={s.id}
                className={`${styles.savedSimRow} ${s.id === activeSimId ? styles.savedSimActive : ''}`}
                data-testid="saved-sim-row"
                data-active={s.id === activeSimId ? 'true' : 'false'}
              >
                <span className={styles.savedSimName}>{s.name}</span>
                {s.savedAt && (
                  <span className={styles.savedSimStamp}>{formatSavedAt(s.savedAt)}</span>
                )}
                <button
                  type="button"
                  className={styles.smallBtn}
                  aria-label={`Load ${s.name}`}
                  onClick={() => onLoadSim(s.id)}
                >
                  Load
                </button>
                <button
                  type="button"
                  className={styles.chipRemove}
                  aria-label={`Delete ${s.name}`}
                  onClick={() => onDeleteSim(s.id)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

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

          {/* Transaction cost (P0.2): adverse half-spread crossing on the option
              legs + ES hedge, default OFF (mid fills). When on, a fixed per-side
              fallback (points) covers fills with no two-sided quote. */}
          <label className={`${styles.field} ${styles.checkboxRow}`}>
            <input
              type="checkbox"
              aria-label="Enable transaction cost"
              checked={Boolean(form.cost_enabled)}
              onChange={(e) => setField('cost_enabled', e.target.checked)}
            />
            <span>Half-spread transaction cost</span>
          </label>
          {form.cost_enabled && (
            <label className={styles.field}>
              <span>One-sided fallback cost (pts/side)</span>
              <input
                type="number"
                min={0}
                step="0.05"
                aria-label="One-sided fallback cost points"
                value={form.cost_fallback_pts}
                onChange={(e) => setField('cost_fallback_pts', e.target.value)}
              />
            </label>
          )}

          {/* Regime-driven side (F2.2): resolve each day's side from the vol
              regime (RV H20>H30>H100 backwardation ladder -> long, else short;
              extremely-low H20 floor -> flat; VVIX gate veto) as-of the PRIOR
              daily close. Default OFF => the static Straddle side above is used
              every day. All thresholds configurable — none hardcoded. */}
          <label className={`${styles.field} ${styles.checkboxRow}`}>
            <input
              type="checkbox"
              aria-label="Enable regime-driven side"
              checked={Boolean(form.regime_side_enabled)}
              onChange={(e) => setField('regime_side_enabled', e.target.checked)}
            />
            <span>Regime-driven side</span>
          </label>
          {form.regime_side_enabled && (
            <>
              <label className={styles.field}>
                <span>HVOL ladder tolerance (0 = strict)</span>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  aria-label="HVOL ladder tolerance"
                  value={form.regime_hvol_tolerance}
                  onChange={(e) => setField('regime_hvol_tolerance', e.target.value)}
                />
              </label>
              <label className={styles.field}>
                <span>Extremely-low H20 floor (0 = off)</span>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  aria-label="Extremely-low H20 floor"
                  value={form.regime_extremely_low_h20}
                  onChange={(e) => setField('regime_extremely_low_h20', e.target.value)}
                />
              </label>
              <label className={`${styles.field} ${styles.checkboxRow}`}>
                <input
                  type="checkbox"
                  aria-label="Enable VVIX gate"
                  checked={Boolean(form.regime_vvix_gate_enabled)}
                  onChange={(e) => setField('regime_vvix_gate_enabled', e.target.checked)}
                />
                <span>VVIX gate (veto to flat above level)</span>
              </label>
              {form.regime_vvix_gate_enabled && (
                <label className={styles.field}>
                  <span>VVIX gate level</span>
                  <input
                    type="number"
                    min={0}
                    step="1"
                    aria-label="VVIX gate level"
                    value={form.regime_vvix_gate_level}
                    onChange={(e) => setField('regime_vvix_gate_level', e.target.value)}
                  />
                </label>
              )}
            </>
          )}

        </div>

        {/* Hedge rule module (v4): enable + instrument + triggers (interval,
            delta band, σ-move) + a Conditions builder (hedge types) + target
            selector. Replaces the old flat hedge controls. */}
        <div className={styles.modulesRow} data-testid="hedge-module-row">
          <div className={styles.moduleColumn}>
            <div className={styles.moduleHeading}>Hedge rule</div>
            <HedgeModule value={hedge} onChange={setHedge} />
          </div>
        </div>

        {/* Entry & Exit rule modules (v2): time + own snap tolerance + a
            Conditions builder. Reusable EntryExitModule component. */}
        <div className={styles.modulesRow} data-testid="entry-exit-modules">
          <div className={styles.moduleColumn}>
            <div className={styles.moduleHeading}>Entry rule</div>
            <EntryExitModule
              title="Entry"
              idPrefix="entry"
              value={entry}
              onChange={setEntry}
            />
          </div>
          <div className={styles.moduleColumn}>
            <div className={styles.moduleHeading}>Exit rule</div>
            <EntryExitModule
              title="Exit"
              idPrefix="exit"
              value={exit}
              onChange={setExit}
              showTriggers
            />
          </div>
        </div>

        {/* F3.2 date-allowlist entry mode — trade ONLY the resolved days (the
            DISTINCT OPPOSITE of Custom days' exclude). Resolved = the union of
            the selected event types (curated FOMC/NFP/CPI) and any explicit
            dates. Default OFF => every eligible day trades. custom_days exclude
            still removes from the allowlisted set; regime side still decides the
            side on the days that remain. */}
        <div className={styles.field} style={{ marginTop: '1rem' }}>
          <span className={styles.fieldLabel}>Date allowlist (trade only these days)</span>
          <label className={`${styles.field} ${styles.checkboxRow}`}>
            <input
              type="checkbox"
              aria-label="Enable date allowlist"
              data-testid="allowlist-enable"
              checked={Boolean(form.allowlist_enabled)}
              onChange={(e) => setField('allowlist_enabled', e.target.checked)}
            />
            <span>Restrict trading to an allowlist of days</span>
          </label>
          {form.allowlist_enabled && (
            <div data-testid="allowlist-controls">
              <span className={styles.fieldLabel}>Event days</span>
              <div className={styles.inlineRow}>
                {ALLOWLIST_EVENT_TYPES.map((t) => {
                  const count = eventCalendar && eventCalendar.events
                    && eventCalendar.events[t] ? eventCalendar.events[t].length : null;
                  return (
                    <label key={t} className={styles.checkboxRow}>
                      <input
                        type="checkbox"
                        aria-label={`Allowlist event type ${t}`}
                        data-testid={`allowlist-type-${t}`}
                        checked={form.allowlist_event_types.includes(t)}
                        onChange={() => setForm((f) => {
                          const has = f.allowlist_event_types.includes(t);
                          return {
                            ...f,
                            allowlist_event_types: has
                              ? f.allowlist_event_types.filter((x) => x !== t)
                              : [...f.allowlist_event_types, t],
                          };
                        })}
                      />
                      <span>{t}{count != null ? ` (${count})` : ''}</span>
                    </label>
                  );
                })}
              </div>
              <span className={styles.fieldLabel}>Explicit dates</span>
              <div className={styles.inlineRow}>
                <input
                  type="date"
                  aria-label="Allowlist date"
                  data-testid="allowlist-date-input"
                  min={win ? win.min_date : undefined}
                  max={win ? win.max_date : undefined}
                  value={allowlistDateDraft}
                  onChange={(e) => setAllowlistDateDraft(e.target.value)}
                />
                <button
                  type="button"
                  className={styles.smallBtn}
                  data-testid="allowlist-add-date"
                  onClick={() => {
                    const d = allowlistDateDraft;
                    if (!d) return;
                    setForm((f) => (f.allowlist_dates.includes(d)
                      ? f
                      : { ...f, allowlist_dates: [...f.allowlist_dates, d].sort() }));
                    setAllowlistDateDraft('');
                  }}
                >
                  Add date
                </button>
              </div>
              {form.allowlist_dates.length > 0 && (
                <ul className={styles.customDaysList} data-testid="allowlist-dates-list">
                  {form.allowlist_dates.map((d) => (
                    <li key={d} className={styles.customDayRow}>
                      <span className={styles.customDayDate}>{d}</span>
                      <button
                        type="button"
                        className={styles.chipRemove}
                        aria-label={`Remove allowlist date ${d}`}
                        onClick={() => setForm((f) => ({
                          ...f,
                          allowlist_dates: f.allowlist_dates.filter((x) => x !== d),
                        }))}
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {eventCalendar && eventCalendar.tentative_dates
                && eventCalendar.tentative_dates.length > 0 && (
                <p className={styles.conditionsEmpty} data-testid="allowlist-tentative-note">
                  Some curated event dates are provisional (tentative):{' '}
                  {eventCalendar.tentative_dates.join(', ')}.
                </p>
              )}
            </div>
          )}
        </div>

        {/* F4.1 laddered multi-entry — open a straddle at each rung of a
            fixed-interval ladder and HOLD EACH TO SETTLEMENT. Default OFF => one
            entry/day (baseline). max_concurrent caps rungs that open per day
            (hold-to-settlement => an open straddle never closes intraday). */}
        <div className={styles.field} style={{ marginTop: '1rem' }}>
          <span className={styles.fieldLabel}>Laddered multi-entry (hold each to settlement)</span>
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              aria-label="Enable laddered multi-entry"
              data-testid="ladder-enable"
              checked={Boolean(form.ladder_enabled)}
              onChange={(e) => setField('ladder_enabled', e.target.checked)}
            />
            <span>Enter every N minutes; hold each straddle to settlement</span>
          </label>
          {form.ladder_enabled && (
            <div data-testid="ladder-controls">
              <label className={styles.field}>
                <span>Interval (minutes, ≥ 1)</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  aria-label="Ladder interval minutes"
                  data-testid="ladder-interval"
                  value={form.ladder_interval_minutes}
                  onChange={(e) => setField('ladder_interval_minutes', e.target.value)}
                />
              </label>
              <label className={styles.field}>
                <span>First entry (HH:MM ET, blank = entry time)</span>
                <input
                  type="text"
                  placeholder="entry time"
                  aria-label="Ladder first entry"
                  data-testid="ladder-first-entry"
                  value={form.ladder_first_entry}
                  onChange={(e) => setField('ladder_first_entry', e.target.value)}
                />
              </label>
              <label className={styles.field}>
                <span>Last entry cutoff (HH:MM ET, blank = exit time)</span>
                <input
                  type="text"
                  placeholder="exit time"
                  aria-label="Ladder last entry cutoff"
                  data-testid="ladder-cutoff"
                  value={form.ladder_last_entry_cutoff}
                  onChange={(e) => setField('ladder_last_entry_cutoff', e.target.value)}
                />
              </label>
              <label className={styles.field}>
                <span>Max concurrent (0 = unlimited)</span>
                <input
                  type="number"
                  min="0"
                  step="1"
                  aria-label="Ladder max concurrent"
                  data-testid="ladder-max-concurrent"
                  value={form.ladder_max_concurrent}
                  onChange={(e) => setField('ladder_max_concurrent', e.target.value)}
                />
              </label>
              <label className={styles.field}>
                <span>Sizing</span>
                <select
                  aria-label="Ladder sizing mode"
                  data-testid="ladder-sizing-mode"
                  value={form.ladder_sizing_mode}
                  onChange={(e) => setField('ladder_sizing_mode', e.target.value)}
                >
                  <option value="equal_contracts">Equal contracts</option>
                  <option value="equal_notional">Equal notional</option>
                </select>
              </label>
              {form.ladder_sizing_mode === 'equal_contracts' ? (
                <label className={styles.field}>
                  <span>Contracts per rung</span>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    aria-label="Ladder contracts per rung"
                    data-testid="ladder-contracts"
                    value={form.ladder_contracts}
                    onChange={(e) => setField('ladder_contracts', e.target.value)}
                  />
                </label>
              ) : (
                <label className={styles.field}>
                  <span>Notional per rung (USD, 0 = auto)</span>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    aria-label="Ladder notional per entry"
                    data-testid="ladder-notional"
                    value={form.ladder_notional_per_entry_usd}
                    onChange={(e) => setField('ladder_notional_per_entry_usd', e.target.value)}
                  />
                </label>
              )}
            </div>
          )}
        </div>

        {/* Custom days — one unified control. Add a date, then per row either
            exclude it (no trade) or expand to override entry/exit via the SAME
            EntryExitModule (partial). */}
        <div className={styles.field} style={{ marginTop: '1rem' }}>
          <span className={styles.fieldLabel}>Custom days (exclude or override entry/exit)</span>
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
                  <div className={styles.customDayHead}>
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
                    {!c.exclude && (
                      <button
                        type="button"
                        className={styles.smallBtn}
                        data-testid={`custom-day-toggle-${c.date}`}
                        aria-expanded={Boolean(c.expanded)}
                        aria-label={`${c.expanded ? 'Hide' : 'Override'} entry/exit for ${c.date}`}
                        onClick={() => updateCustomDay(c.date, { expanded: !c.expanded })}
                      >
                        {c.expanded ? 'Hide override ▾' : 'Override entry/exit ▸'}
                      </button>
                    )}
                    <button
                      type="button"
                      className={styles.chipRemove}
                      aria-label={`Remove custom day ${c.date}`}
                      onClick={() => removeCustomDay(c.date)}
                    >
                      ×
                    </button>
                  </div>
                  {!c.exclude && c.expanded && (
                    <div
                      className={styles.customDayOverride}
                      data-testid={`custom-day-override-${c.date}`}
                    >
                      <div className={styles.moduleColumn}>
                        <div className={styles.moduleHeading}>Entry override</div>
                        <EntryExitModule
                          title={`Entry ${c.date}`}
                          idPrefix={`cd-${c.date}-entry`}
                          value={c.entry}
                          partial
                          onChange={(next) => updateCustomDay(c.date, { entry: next })}
                        />
                      </div>
                      <div className={styles.moduleColumn}>
                        <div className={styles.moduleHeading}>Exit override</div>
                        <EntryExitModule
                          title={`Exit ${c.date}`}
                          idPrefix={`cd-${c.date}-exit`}
                          value={c.exit}
                          partial
                          showTriggers
                          onChange={(next) => updateCustomDay(c.date, { exit: next })}
                        />
                      </div>
                    </div>
                  )}
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
              {reconnecting && (
                <span className={styles.statLabel} data-testid="run-reconnecting">
                  {' '}· reconnecting…
                </span>
              )}
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

      {days.length > 0 && <WeekdayAttributionView days={days} />}

      {days.length > 0 && <RegimeSensitivityView days={days} />}

      {days.length > 0 && <EventAttributionView days={days} eventCalendar={eventCalendar} />}

      {/* F4.1: per-rung readout — renders only on laddered runs (days carrying
          an ``entries[]``); inert (null) otherwise, so it never disturbs the
          day-level calendar or the aggregate-keyed attribution views above. */}
      {days.length > 0 && <LadderEntriesView days={days} />}

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
                    const unmet = outcome === 'unmet';
                    const regimeFlat = outcome === 'regime_flat';
                    const noTrade = outcome === 'skipped' || excluded || unmet || regimeFlat;
                    const tagText = excluded ? 'no trade'
                      : unmet ? 'no entry'
                      : regimeFlat ? 'flat'
                      : 'skipped';
                    const pnl = slot.data.pnl || null;
                    // F2.2: the resolved regime side (long/short) on a traded day,
                    // as a compact L/S badge so the WHY is visible at a glance.
                    const regimeSide = slot.data.regime && slot.data.regime.side;
                    // Early-exit trigger (v3): a traded day that closed on a
                    // trigger carries a small marker + a data attribute.
                    const trig = slot.data.exit_trigger || null;
                    return (
                      <div
                        key={slot.iso}
                        className={`${styles.dayCell} ${OUTCOME_CLASS[outcome] || ''} ${trig ? styles.cellTriggered : ''}`}
                        data-testid="day-cell"
                        data-date={slot.iso}
                        data-status={slot.data.status}
                        data-outcome={outcome}
                        data-exit-trigger={trig ? trig.type : undefined}
                        data-regime-side={regimeSide || undefined}
                        title={cellTitle(slot.iso, slot.data)}
                      >
                        <span className={styles.domRow}>
                          {regimeSide && !noTrade && (
                            <span
                              className={styles.regimeBadge}
                              data-testid="regime-side-badge"
                              aria-label={`regime side: ${regimeSide}`}
                              title={`regime side: ${regimeSide}`}
                            >
                              {regimeSide === 'long' ? 'L' : regimeSide === 'short' ? 'S' : ''}
                            </span>
                          )}
                          {trig && !noTrade && (
                            <span
                              className={styles.triggerBadge}
                              data-testid="trigger-marker"
                              aria-label={`early exit: ${trig.type}`}
                              title={`exited early on ${trig.type}`}
                            >
                              ⚡
                            </span>
                          )}
                          <span className={styles.dom}>{slot.dom}</span>
                        </span>
                        {noTrade ? (
                          <span className={styles.cellTag}>
                            {tagText}
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
