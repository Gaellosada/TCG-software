import { useEffect, useRef, useState } from 'react';
import { selectOption } from '../../api/options';
import {
  computeImpliedLeverage,
  formatLeverage,
  leverageBand,
  premiumPctOfStrike,
  selectionLabel,
  wipeoutFactor,
} from './leverage';

/**
 * Live implied-leverage readout for an option-leg "hold" form.
 *
 * Surfaces the UNDERLYING notional the ``nav_times`` (Size %) sizing controls
 * as a concrete multiple of NAV — turning the qualitative wipeout warning into
 * a number.  It DEBOUNCED-probes ONE representative (strike, premium_mid) for
 * the currently-configured contract via GET /api/options/select, then computes
 * leverage = navFraction x strike / premium_mid entirely client-side.  Changing
 * Size% recomputes the number WITHOUT refetching (navFraction is not a fetch
 * dependency); only the contract-defining fields (root / type / criterion /
 * maturity) + the reference date trigger a new probe.
 *
 * Graceful degradation: while loading, on any error, on an unresolvable
 * selection, or when the premium is missing/zero, it renders the existing
 * qualitative wipeout hint (``data-testid="nav-hint"``) instead of a bogus
 * number.  It never mutates the form value (probe is a read-only GET), so it is
 * safe under ``disabled`` (locked / read-only) legs.
 *
 * Props:
 *   streamValue    the OptionStreamForm value (collection / option_type /
 *                  cycle / maturity / selection).
 *   navFraction    the current nav_times fraction (1.0 = 100% of NAV).
 *   availableRoots roots list (for the last_trade_date reference-date fallback).
 *   referenceDate  optional YYYY-MM-DD string (or Date) — the date at which to
 *                  probe; falls back to the selected root's last_trade_date.
 *   onBand         optional (band|null) => void — lets the parent tint the
 *                  Size% input by the leverage band.
 */

export const BAND_COLORS = { green: '#1a7f37', amber: '#9a6700', red: '#cf222e' };
const BAND_DOTS = { green: '🟢', amber: '🟠', red: '🔴' };
const DEBOUNCE_MS = 300;

// Normalise a reference date to a YYYY-MM-DD string (or null). Accepts a
// Date or an already-formatted string; anything else → null (→ fallback).
function normalizeDate(d) {
  if (!d) return null;
  if (d instanceof Date && !Number.isNaN(d.getTime())) {
    // Format from LOCAL calendar components (not toISOString/UTC) so a Date near
    // midnight in a negative-UTC timezone can't shift the probe date ±1 day.
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }
  if (typeof d === 'string' && /^\d{4}-\d{2}-\d{2}/.test(d)) {
    return d.slice(0, 10);
  }
  return null;
}

// Build the /select SelectQuery from the form value + reference date. The
// form's `selection`/`maturity` shapes are already wire-compatible with the
// backend SelectionCriterion/MaturityRule (pydantic populate_by_name aliases:
// selection.target → target_delta/target_K_over_S; maturity.target_days →
// target_dte_days). Returns null when the leg cannot resolve a contract yet.
function buildSelectQuery(streamValue, refDate) {
  if (!streamValue || !streamValue.collection || !refDate) return null;
  if (!streamValue.selection || !streamValue.maturity || !streamValue.option_type) {
    return null;
  }
  // A fixed-date maturity with no date can't resolve — skip → fallback hint.
  if (streamValue.maturity.kind === 'fixed' && !streamValue.maturity.date) return null;
  return {
    root: streamValue.collection,
    date: refDate,
    type: streamValue.option_type,
    criterion: streamValue.selection,
    maturity: streamValue.maturity,
  };
}

function formatFactor(f) {
  if (typeof f !== 'number' || !Number.isFinite(f)) return null;
  return `${f.toFixed(1)}×`;
}

export default function ImpliedLeverageReadout({
  streamValue,
  navFraction,
  availableRoots,
  referenceDate = null,
  onBand = null,
}) {
  const [probe, setProbe] = useState({ status: 'idle', strike: null, premiumMid: null });
  const abortRef = useRef(null);

  const collection = streamValue && streamValue.collection;
  const selectedRoot = (availableRoots || []).find((r) => r.collection === collection);
  const refDate = normalizeDate(referenceDate)
    || (selectedRoot ? normalizeDate(selectedRoot.last_trade_date) : null);

  const query = buildSelectQuery(streamValue, refDate);
  // Stable dependency for the debounced fetch: only the contract-defining
  // fields (NOT navFraction, which recomputes client-side).
  const queryKey = query ? JSON.stringify(query) : null;

  useEffect(() => {
    if (!queryKey) {
      setProbe({ status: 'idle', strike: null, premiumMid: null });
      return undefined;
    }
    let cancelled = false;
    setProbe((p) => ({ ...p, status: 'loading' }));
    const timer = setTimeout(async () => {
      if (abortRef.current) abortRef.current.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const resp = await selectOption(JSON.parse(queryKey), { signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        const strike = resp && resp.contract ? resp.contract.strike : null;
        const premiumMid = resp ? resp.premium_mid : null;
        setProbe({ status: 'ok', strike, premiumMid });
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        if (!cancelled) setProbe({ status: 'error', strike: null, premiumMid: null });
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [queryKey]);

  // Abort any in-flight probe on unmount.
  useEffect(() => () => {
    if (abortRef.current) abortRef.current.abort();
  }, []);

  // Client-side leverage from the probed (strike, premium) + CURRENT Size%.
  const leverage = computeImpliedLeverage({
    navFraction,
    strike: probe.strike,
    premiumMid: probe.premiumMid,
  });
  const band = leverageBand(leverage);
  const levText = formatLeverage(leverage);

  // Notify the parent of the band (for input tinting). Effect so we don't call
  // a setState-bearing callback during render.
  useEffect(() => {
    if (onBand) onBand(band);
  }, [onBand, band]);

  // Fallback: no usable quantitative data → the qualitative wipeout hint.
  if (levText == null || band == null) {
    return (
      <span data-testid="nav-hint" style={{ fontSize: '0.85em', opacity: 0.8 }}>
        {probe.status === 'loading' ? 'Estimating leverage… ' : ''}
        A short/naked option at full notional (100%) can wipe out (a 10Δ put
        premium can triple on a selloff → &gt;100% loss). Use a small percentage
        to size the premium notional.
      </span>
    );
  }

  const pct = premiumPctOfStrike(probe.strike, probe.premiumMid);
  const factor = wipeoutFactor(navFraction);
  const sel = selectionLabel(streamValue.selection);
  const putCall = streamValue.option_type === 'P' ? 'put' : 'call';

  return (
    <div data-testid="lev-readout-group" style={{ fontSize: '0.85em' }}>
      <div
        data-testid="lev-readout"
        data-band={band}
        style={{ color: BAND_COLORS[band], fontWeight: 600 }}
      >
        {BAND_DOTS[band]} ≈ {levText} underlying notional
        {refDate && (
          <span
            data-testid="lev-date"
            style={{ fontWeight: 400, opacity: 0.7, marginLeft: '0.4em' }}
          >
            (at {refDate})
          </span>
        )}
      </div>
      {pct != null && (
        <div data-testid="lev-subline" style={{ opacity: 0.8 }}>
          (premium ≈ {pct.toFixed(2)}% of strike for this {sel} {putCall})
        </div>
      )}
      {factor != null && (
        <div data-testid="lev-caution" style={{ opacity: 0.9 }}>
          ⚠ If sold/written, a ~{formatFactor(factor)} premium spike wipes equity
          (a bought leg risks only the premium).
        </div>
      )}
    </div>
  );
}
