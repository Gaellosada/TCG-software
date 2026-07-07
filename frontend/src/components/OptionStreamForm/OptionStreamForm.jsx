import { useMemo, useCallback, useId, useEffect, useRef, useState } from 'react';
import styles from './OptionStreamForm.module.css';
import ImpliedLeverageReadout, { BAND_COLORS } from './ImpliedLeverageReadout';

/**
 * Standalone, side-effect-free form for picking every field needed to
 * construct an OptionStreamRef request. Designed for cross-page reuse — the
 * Continuous tab composes this form by wrapping it with an extra RollRule
 * field, so the prop shape below is locked.
 *
 * Output object emitted via onChange (and read from `value`):
 *   {
 *     type: 'option_stream',
 *     collection: 'OPT_SP_500',
 *     option_type: 'C' | 'P',
 *     cycle: null | 'M' | 'W3 Friday' | 'W' | 'Q',
 *     maturity: { kind, ... },           // discriminated union (kind field)
 *     selection: { kind, ... },          // discriminated union (kind field)
 *     stream: 'mid'|'iv'|'delta'|'gamma'|'vega'|'theta'|'open_interest'|'volume',
 *     roll_offset: { value: <int>, unit: 'days' | 'months' },  // ROLL-EARLY axis:
 *                                               // resolve the maturity as of
 *                                               // (date + offset) so the roll fires
 *                                               // that much earlier; {value:0} = no
 *                                               // shift. Range days 0..365 / months
 *                                               // 0..12. DISTINCT from the maturity's
 *                                               // own month offset (which expiration
 *                                               // to target). "Roll at end of month"
 *                                               // is the EndOfMonth maturity, not a
 *                                               // roll_offset value.
 *   }
 *
 * NOTE: option continuous series carry NO back-adjustment — ratio/difference
 * are conceptually ill-posed for option premia, so (unlike the futures
 * continuous-series picker) there is no `adjustment` field or control here. The
 * emitted series is always the raw stitched stream.
 *
 * Validation:
 *   - greek streams (gamma/vega/theta) require root.has_greeks === true;
 *   - by_delta + stream === 'delta' is tautological.
 * These produce a UI-side hint surface (the `validationError` returned via
 * the form's read-only public field, also signalled via the `disabled`
 * styling on the dependent fields). The backend has the hard guard.
 */

const ALL_OPTION_TYPES = ['C', 'P'];
const ALL_CYCLES = [null, 'M', 'W3 Friday', 'W1 Friday', 'W2 Friday', 'W4 Friday', 'W', 'Q'];
const ALL_STREAMS = ['mid', 'bs_mid', 'iv', 'delta', 'gamma', 'vega', 'theta', 'open_interest', 'volume'];
const GREEK_STREAMS = new Set(['gamma', 'vega', 'theta']);
// NOTE: option continuous series carry NO back-adjustment.  Ratio/difference are
// conceptually ill-posed for option premia (a back-adjusted premium represents
// no tradable instrument), so — unlike the futures continuous-series picker —
// this form has no adjustment control.  The series is always the raw stitched
// stream.
const ALL_MATURITY_KINDS = ['next_third_friday', 'nearest_to_target', 'end_of_month', 'plus_n_days', 'fixed'];
const ALL_SELECTION_KINDS = ['by_moneyness', 'by_delta', 'by_strike'];

// ── nav_times presentation ────────────────────────────────────────────────
// The wire / persisted `nav_times` is a raw MULTIPLIER (factor), default 1.0 =
// unlevered. It is shown and typed VERBATIM — no ×100 / ÷100 conversion — so `1`
// reads as the natural unlevered value, `2` as double, `0.5` as half. The stored
// value is unchanged (NO backend change, NO migration); this file only decides
// what the user sees/types. (Distinct from `delta`, likewise a factor with no
// /100 conversion anywhere.)

const MATURITY_LABELS = {
  next_third_friday: 'Next 3rd Friday',
  nearest_to_target: 'Nearest to Target DTE',
  end_of_month: 'End of Month',
  plus_n_days: '+N Days',
  fixed: 'Fixed Date',
};

const SELECTION_LABELS = {
  by_strike: 'By Strike',
  by_moneyness: 'By Moneyness (K/S)',
  by_delta: 'By Delta',
};

const STREAM_LABELS = {
  mid: 'Mid price',
  bs_mid: 'BS mid (from IV)',
  iv: 'Implied volatility',
  delta: 'Delta',
  gamma: 'Gamma',
  vega: 'Vega',
  theta: 'Theta',
  open_interest: 'Open interest',
  volume: 'Volume',
};

// Tooltip clarifying that the ``mid`` series is the bid-ask midpoint — NOT a
// daily OHLC field. Surfaced on the Series control whenever Mid is reachable.
const MID_TOOLTIP =
  'Mid = (bid + ask) / 2 — the quote midpoint, NOT the daily high/low or last/close.';

const CYCLE_LABELS = {
  _any: 'Any',
  M: 'Standard Monthly (M)',
  'W3 Friday': 'Monthly 3rd Friday (W3)',
  'W1 Friday': '1st Friday (W1)',
  'W2 Friday': '2nd Friday (W2)',
  'W4 Friday': '4th Friday (W4)',
  W: 'Weekly (W)',
  Q: 'Quarterly (Q)',
  D: 'Daily (D)',
};

// Label for the SYNTHESISED generic-weekly entry — distinct from the literal
// 'W' label above (crypto/VIX carry a real 'W' tag; index roots like OPT_SP_500
// carry only per-week 'W# Friday' tags, so we synthesise a "cover all Fridays"
// choice whose wire value is still 'W' — the backend's expand_cycle('W') unions
// all the weekly Friday tags).  Never coexists with a literal-'W' root.
const SYNTHETIC_WEEKLY_LABEL = 'Weekly — all Fridays (W)';

// Matches a per-week Friday tag: 'W1 Friday' … 'W4 Friday' (any digit run).
const WEEK_FRIDAY_RE = /^W\d+ Friday$/;

/**
 * Derive the cycle dropdown options for the SELECTED root from the real
 * ``cycles`` tag-set the backend reports for that root (the ``cycles`` field on
 * ``GET /api/options/roots`` — the distinct ``expiration_cycle`` tags, empty
 * string filtered out, ascending).  This replaces the old static ``ALL_CYCLES``
 * superset that offered phantom 'W'/'Q' for index roots (→ empty chain → HTTP
 * 400).
 *
 * Rules:
 *   - "Any" (wire ``null``) is always offered first (unless a caller-supplied
 *     ``allowedCycles`` restriction drops it).
 *   - Each real tag is offered verbatim in the backend's ascending order.
 *   - A GENERIC weekly entry (wire ``'W'``) is synthesised — IN ADDITION to the
 *     specific 'W# Friday' entries — for any root that has ≥1 'W# Friday' tag but
 *     NO literal 'W' (e.g. OPT_SP_500), so the user can pick "all Fridays" in one
 *     go.  Roots that literally carry 'W' (crypto/VIX) keep their real 'W' and are
 *     NOT given a duplicate.
 *   - ``allowedCycles`` (optional; ``null`` = no restriction) is a FURTHER
 *     restriction applied on top — only cycles present in it survive (kept for
 *     backward compatibility with callers that pin a subset).
 *
 * @param {string[]|undefined|null} rootCycles  the selected root's ``cycles``
 *   (``undefined`` on a legacy/roots-less fixture → fall back to the full static
 *   superset so nothing regresses; ``[]`` is treated the same way).
 * @param {Array<string|null>|null} allowedCycles  optional further restriction.
 * @returns {Array<{value: string|null, label: string}>}
 */
function deriveCycleOptions(rootCycles, allowedCycles = null) {
  // Single source of truth for the two weekly-tag predicates, at function
  // scope so the label loop below can reuse them (a synthetic 'W' is one the
  // root doesn't carry literally → label it as the synthetic weekly).
  const hasBareW = Array.isArray(rootCycles) && rootCycles.includes('W');
  const hasWeekFriday = Array.isArray(rootCycles) && rootCycles.some((c) => WEEK_FRIDAY_RE.test(c));
  let base;
  if (Array.isArray(rootCycles) && rootCycles.length > 0) {
    base = rootCycles.slice();
    if (hasWeekFriday && !hasBareW) base.push('W');
  } else {
    // Legacy / missing cycles → the historical static superset (minus null,
    // which is prepended below as the "Any" sentinel).
    base = ALL_CYCLES.filter((c) => c != null);
  }

  // Optional caller restriction (a pinned subset). null = no restriction.
  let allowsAny = true;
  if (Array.isArray(allowedCycles)) {
    const allowSet = new Set(allowedCycles);
    base = base.filter((c) => allowSet.has(c));
    allowsAny = allowedCycles.includes(null);
  }

  const options = [];
  if (allowsAny) options.push({ value: null, label: CYCLE_LABELS._any });
  for (const c of base) {
    const label = (c === 'W' && !hasBareW)
      ? SYNTHETIC_WEEKLY_LABEL
      : (CYCLE_LABELS[c] || c);
    options.push({ value: c, label });
  }
  return options;
}

/**
 * Pick the default cycle wire-value from a derived option list.  Preference:
 * 'W3 Friday' (the real monthly 3rd-Friday series) → 'M' → "Any" (null) if
 * offered → the first concrete cycle.  Preserves the prior default bias while
 * guaranteeing the chosen value is actually in the root-scoped list.
 */
function pickDefaultCycle(cycleOptions) {
  const values = cycleOptions.map((o) => o.value);
  const nonNull = values.filter((c) => c != null);
  if (nonNull.includes('W3 Friday')) return 'W3 Friday';
  if (nonNull.includes('M')) return 'M';
  if (values.includes(null)) return null;
  return nonNull.length > 0 ? nonNull[0] : null;
}

/**
 * Build a default-shaped MaturityRule for a given kind.
 */
function defaultMaturity(kind) {
  switch (kind) {
    case 'next_third_friday':
      return { kind: 'next_third_friday', offset_months: 0 };
    case 'end_of_month':
      return { kind: 'end_of_month', offset_months: 0 };
    case 'nearest_to_target':
      return { kind: 'nearest_to_target', target_days: 30 };
    case 'plus_n_days':
      return { kind: 'plus_n_days', n: 30 };
    case 'fixed':
      return { kind: 'fixed', date: '' };
    default:
      return { kind: 'next_third_friday', offset_months: 0 };
  }
}

/**
 * Build a default-shaped SelectionCriterion for a given kind.
 * by_delta uses a signed target — sign carries the side.
 */
function defaultSelection(kind, optionType = 'C') {
  switch (kind) {
    case 'by_strike':
      return { kind: 'by_strike', strike: 0 };
    case 'by_moneyness':
      return { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 };
    case 'by_delta': {
      const sign = optionType === 'P' ? -1 : 1;
      return { kind: 'by_delta', target: 0.25 * sign, tolerance: 0.05, strict: false };
    }
    default:
      return { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 };
  }
}

/**
 * Build the full default OptionStreamRef given a roots list (picks the
 * first root) and the active allowed-* lists.
 */
export function buildDefaultOptionStream({
  availableRoots,
  allowedMaturityKinds = ALL_MATURITY_KINDS,
  allowedSelectionKinds = ALL_SELECTION_KINDS,
  allowedStreams = ALL_STREAMS,
  allowedOptionTypes = ALL_OPTION_TYPES,
  allowedCycles = null,
}) {
  const root = availableRoots && availableRoots.length > 0 ? availableRoots[0] : null;
  // Canonical default: prefer W3 Friday (the real monthly cycle — every
  // month's 3rd Friday, PM-settled) over M (quarterly standard, AM-settled,
  // Mar/Jun/Sep/Dec only). Falls back to M, then "Any", then the first cycle.
  // The candidate list is now the ROOT-SCOPED cycle set (from root.cycles) so
  // the default is never a cycle the root does not actually have.
  const defaultCycle = pickDefaultCycle(
    deriveCycleOptions(root ? root.cycles : undefined, allowedCycles),
  );
  return {
    type: 'option_stream',
    collection: root ? root.collection : '',
    option_type: allowedOptionTypes[0] || 'C',
    cycle: defaultCycle,
    maturity: defaultMaturity(allowedMaturityKinds[0] || 'next_third_friday'),
    selection: defaultSelection(allowedSelectionKinds[0] || 'by_moneyness', allowedOptionTypes[0] || 'C'),
    stream: allowedStreams[0] || 'mid',
    // Roll offset — the ROLL-EARLY axis: {value, unit:'days'|'months'}. Resolve
    // the maturity that much earlier so the roll fires sooner. Default value 0 =
    // roll at the maturity rule's natural time. DISTINCT from the maturity's own
    // month offset (the TARGET-month axis — which expiration to aim at).
    // NOTE: "roll at end of month" is the EndOfMonth MATURITY (held monthly),
    // NOT a roll-offset value. Option streams carry no back-adjustment, so there
    // is no `adjustment` field — the series is always the raw stitched stream.
    roll_offset: { value: 0, unit: 'days' },
  };
}

/**
 * Inspect the proposed value + roots and report any UI-side validation
 * issue. Returns null when the form is valid, else { code, message,
 * field } (field is the dom-id-able key the consumer can highlight).
 */
export function validateOptionStream(value, availableRoots) {
  if (!value || !value.collection) {
    return { code: 'NO_ROOT', message: 'Pick a root.', field: 'collection' };
  }
  const root = (availableRoots || []).find((r) => r.collection === value.collection);
  if (root && root.has_greeks === false && GREEK_STREAMS.has(value.stream)) {
    return {
      code: 'STREAM_UNAVAILABLE_FOR_ROOT',
      message: 'This root does not have greeks.',
      field: 'stream',
    };
  }
  if (
    value.selection
    && value.selection.kind === 'by_delta'
    && value.stream === 'delta'
  ) {
    return {
      code: 'TAUTOLOGICAL_OPTION_STREAM',
      message: 'Delta of a delta-targeted contract is tautological.',
      field: 'stream',
    };
  }
  return null;
}

// ── Sizing-mode presentation labels ──────────────────────────────────────
// How the held option quantity is sized (hold-mode $-P&L only). Percentage
// (premium_notional) is the DEFAULT and byte-identical to the shipped behaviour;
// Futures notional sizes the quantity off the corresponding future's dollar
// notional instead of the option premium.
const SIZING_MODE_LABELS = {
  premium_notional: 'Percentage (current)',
  futures_notional: 'Futures notional',
};
// Futures-reference choices offered in the dropdown. Deliberately EXCLUDES
// ``continuous_front`` — the backend raises a loud not-implemented error for it,
// so it must NOT be surfaced yet (see task contract). ``nearest_on_or_after`` is
// the backend default.
const FUTURES_REFERENCE_LABELS = {
  nearest_on_or_after: 'Nearest on/after expiry',
  nearest_abs: 'Nearest (abs distance)',
};
// One-line description of the futures-notional sizing formula, surfaced as a
// helper beneath the reference dropdown.
const FUTURES_NOTIONAL_HELP =
  'Futures-notional sizing: qty = Size × NAV / (F_ref × M_fut) — the held '
  + "quantity is sized off the reference future's dollar notional (F_ref = its "
  + 'price, M_fut = its contract multiplier), not the option premium.';

// ── Size (nav_times multiplier) field labels + tooltips, mode-aware ─────────
// nav_times is a plain FACTOR (default 1 = unlevered), NOT a percentage. The
// label + tooltip read as a multiplier in both sizing modes.
const SIZE_LABEL_FUTURES = 'Size (× futures notional)';
const SIZE_LABEL_PREMIUM = 'Size (× NAV, premium)';
const SIZE_TOOLTIP_FUTURES =
  'Factor between the equivalent number of futures (NAV ÷ (F_ref × contract '
  + 'multiplier)) and the number of option contracts held: '
  + 'qty = Size × NAV/(F_ref×M_fut). 1 = one option per future you could buy; '
  + '2 = 2×; 0.5 = half.';
const SIZE_TOOLTIP_PREMIUM =
  'Multiple of NAV deployed as option premium: qty = Size × NAV/premium. '
  + '1 = the full NAV spent on premium.';

/**
 * Shared "Sizing" mode + "Size" (nav_times multiplier) input, rendered
 * identically by both the ``holdRequired`` (portfolio price-leg) and the
 * ``hold_between_rolls`` (signals-toggle) branches. Extracted to kill the
 * near-verbatim duplication (one future-drift hazard). Test IDs are preserved.
 *
 * The Size field shows the RAW ``nav_times`` factor (default 1 = unlevered) —
 * NOT a percentage — and its label + tooltip are mode-aware (see the SIZE_*
 * constants above).
 *
 * SIZING MODE (Guardrail Sign 5): the implied-leverage readout is
 * premium-notional-specific (leverage = nav_times·strike/premium) and WRONG for
 * futures-notional sizing, so it is shown ONLY in ``premium_notional`` mode. In
 * ``futures_notional`` mode it is replaced by the Futures-reference dropdown +
 * the formula helper (no misleading leverage number). The Size multiplier is
 * exposed in BOTH modes.
 */
function SizeAndLeverage({
  streamValue,
  availableRoots,
  referenceDate,
  navBand,
  setNavBand,
  onNavTimes,
  sizingMode,
  onSizingMode,
  futuresReference,
  onFuturesReference,
  disabled,
}) {
  const navTimes = typeof streamValue.nav_times === 'number' ? streamValue.nav_times : 1.0;
  const isFutures = sizingMode === 'futures_notional';
  const sizeLabel = isFutures ? SIZE_LABEL_FUTURES : SIZE_LABEL_PREMIUM;
  const sizeTooltip = isFutures ? SIZE_TOOLTIP_FUTURES : SIZE_TOOLTIP_PREMIUM;
  return (
    <>
      {/* Sizing mode — Percentage (premium notional, default) vs Futures notional. */}
      <label
        className={styles.fieldInline}
        title="How the held quantity is sized. Premium notional: qty = Size × NAV / premium (default). Futures notional: qty = Size × NAV / (reference future's price × its contract multiplier)."
      >
        Sizing
        <select
          className={styles.input}
          value={sizingMode}
          onChange={(e) => onSizingMode(e.target.value)}
          disabled={disabled}
          aria-label="Sizing mode"
          data-testid="sizing-mode"
        >
          <option value="premium_notional">{SIZING_MODE_LABELS.premium_notional}</option>
          <option value="futures_notional">{SIZING_MODE_LABELS.futures_notional}</option>
        </select>
      </label>
      <label className={styles.fieldInline} title={sizeTooltip}>
        {sizeLabel}
        <input
          type="number"
          className={styles.input}
          min={0}
          step="any"
          placeholder="1"
          value={navTimes}
          onChange={(e) => onNavTimes(e.target.value)}
          disabled={disabled}
          aria-label={sizeLabel}
          data-testid="nav-times"
          style={navBand && !isFutures ? { borderColor: BAND_COLORS[navBand], borderWidth: 2 } : undefined}
        />
      </label>
      {isFutures ? (
        <>
          <label
            className={styles.fieldInline}
            title="Which listed future to size the notional against. Nearest on/after expiry: the nearest future expiring on or after the option's expiry (the root's real cycle). Nearest (abs distance): the future whose expiry is closest in absolute time."
          >
            Futures reference
            <select
              className={styles.input}
              value={futuresReference}
              onChange={(e) => onFuturesReference(e.target.value)}
              disabled={disabled}
              aria-label="Futures reference"
              data-testid="futures-reference"
            >
              <option value="nearest_on_or_after">{FUTURES_REFERENCE_LABELS.nearest_on_or_after}</option>
              <option value="nearest_abs">{FUTURES_REFERENCE_LABELS.nearest_abs}</option>
            </select>
          </label>
          <span
            data-testid="futures-notional-help"
            style={{ fontSize: '0.85em', opacity: 0.8 }}
          >
            {FUTURES_NOTIONAL_HELP}
          </span>
        </>
      ) : (
        <ImpliedLeverageReadout
          streamValue={streamValue}
          navFraction={navTimes}
          availableRoots={availableRoots}
          referenceDate={referenceDate}
          onBand={setNavBand}
        />
      )}
    </>
  );
}

/**
 * Standalone form. Reads everything from props; emits the next value via
 * `onChange`. Does no fetching. The parent owns the value/state.
 */
export default function OptionStreamForm({
  value,
  onChange,
  availableRoots,
  allowedMaturityKinds = ALL_MATURITY_KINDS,
  allowedSelectionKinds = ALL_SELECTION_KINDS,
  allowedStreams = ALL_STREAMS,
  allowedOptionTypes = ALL_OPTION_TYPES,
  // Optional FURTHER restriction of the cycle dropdown. Default null = derive
  // the offered cycles purely from the SELECTED root's real ``cycles`` tag-set
  // (see deriveCycleOptions). A non-null array pins the dropdown to that subset.
  allowedCycles = null,
  disabled = false,
  // SIGNALS-only: surface the backtest "Hold contract between rolls
  // (fixed-contract P&L)" toggle + its ``nav_times`` premium-notional multiple.
  // Default false so the Data-page chart and Portfolio holdings pickers (where a
  // backtest-P&L knob is meaningless) are unchanged.  When a delta/moneyness-
  // selected option signal enables it, the backend freezes the contract between
  // rolls and books fixed-contract dollar P&L instead of a %-return (which
  // explodes as a held premium decays toward zero).
  showHoldControls = false,
  // PORTFOLIO option price legs: hold-mode is ON ONLY (the backend REQUIRES it —
  // a rolled option's daily-reselect %-return is not a valid equity series). When
  // set: render NO on/off toggle; force hold on + default cycle 'M' once, and
  // always show the nav_times input + a wipeout hint. Signals keep the toggle.
  holdRequired = false,
  // EDIT mode: a saved config was pre-filled into ``value`` (derived upstream as
  // ``initialConfig != null``). Suppresses the CREATE-only cycle nudge below so
  // an edited leg's stored ``cycle`` (incl. null "Any" / 'W3 Friday') is never
  // silently rewritten to 'M'. Note ``value`` alone can't signal edit here — the
  // create flow ALSO seeds a non-null default (buildDefaultOptionStream).
  editMode = false,
  // Optional reference date (YYYY-MM-DD string or Date) at which to probe the
  // representative (strike, premium) for the implied-leverage readout on the
  // hold form. When omitted, the readout falls back to the selected root's
  // last_trade_date. Read-only side-effect (a GET) — never mutates the value.
  referenceDate = null,
}) {
  // Per-instance stable id used to scope the option-type radio group's
  // `name` attribute.  Without this, two simultaneously-mounted forms
  // (e.g. an option basket composer with two option legs) would share a
  // single browser-level radio group named "option-type", causing
  // clicking "Put" on one form to visually deselect "Call" on the
  // sibling — see Bug 1 regression in InstrumentPickerModal.test.jsx.
  const formId = useId();

  // Colour band for the implied-leverage readout (green/amber/red). Set by the
  // <ImpliedLeverageReadout> child so the Size input can be tinted to match.
  const [navBand, setNavBand] = useState(null);

  // Resolve a usable value: if the parent supplies null we still render
  // safely against a sensible default. Exposing onChange below means the
  // parent will adopt the default on first interaction.
  const v = useMemo(() => (
    value || buildDefaultOptionStream({
      availableRoots,
      allowedMaturityKinds,
      allowedSelectionKinds,
      allowedStreams,
      allowedOptionTypes,
      allowedCycles,
    })
  ), [value, availableRoots, allowedMaturityKinds, allowedSelectionKinds, allowedStreams, allowedOptionTypes, allowedCycles]);

  const validation = useMemo(() => validateOptionStream(v, availableRoots), [v, availableRoots]);

  const emit = useCallback((patch) => {
    onChange({ ...v, ...patch });
  }, [v, onChange]);

  // When the consumer restricts the form to a single stream (the portfolio
  // add-holding flow pins option legs to the option PRICE = 'mid'; iv/greeks/
  // volume are SIGNAL-level operands, not a portfolio concern), there is no
  // stream choice to make: coerce a stale/mismatched value back to the only
  // allowed stream so the emitted ref is always correct, and the selector is
  // hidden below.
  const singleStream = allowedStreams.length === 1;
  useEffect(() => {
    if (singleStream && v.stream !== allowedStreams[0]) {
      onChange({ ...v, stream: allowedStreams[0] });
    }
    // Re-run when the restriction or current stream changes.
  }, [singleStream, allowedStreams, v, onChange]);

  // PORTFOLIO hold-required flow: option price legs are ALWAYS held (no toggle).
  // The hold flag itself is forced on by AddHoldingModal at leg-build time (the
  // SINGLE authority for it — the backend also rejects hold-off), so this one-shot
  // only defaults the cycle to 'M' (the backend's expand_cycle broadens 'M' to the
  // monthly 3rd-Friday series, reproducing a monthly option roll). One-shot so the
  // user can still change the cycle afterwards.
  // CREATE-only: gated off in ``editMode`` (a saved config was pre-filled) so an
  // edited leg's stored cycle is never silently rewritten to 'M' (BLOCKER-1).
  const heldInit = useRef(false);
  useEffect(() => {
    if (!holdRequired || editMode || heldInit.current) return;
    heldInit.current = true;
    if (v.cycle == null || v.cycle === 'W3 Friday') onChange({ ...v, cycle: 'M' });
  }, [holdRequired, editMode, v, onChange]);

  // Changing root must also keep ``cycle`` valid: the new root's real cycle
  // tag-set may not contain the current cycle (e.g. picking 'Q' on OPT_BTC then
  // switching to OPT_GOLD which only has 'M'). If the current cycle is no longer
  // offered, snap to the root-scoped default; otherwise keep it.
  const setRoot = useCallback((collection) => {
    const nextRoot = (availableRoots || []).find((r) => r.collection === collection);
    const opts = deriveCycleOptions(nextRoot ? nextRoot.cycles : undefined, allowedCycles);
    const validValues = opts.map((o) => o.value);
    const curCycle = v.cycle ?? null;
    const nextCycle = validValues.includes(curCycle) ? curCycle : pickDefaultCycle(opts);
    emit({ collection, cycle: nextCycle });
  }, [availableRoots, allowedCycles, v.cycle, emit]);

  const setOptionType = useCallback((option_type) => {
    // When the side flips, also flip the sign of by_delta target so it
    // stays meaningful (positive for calls, negative for puts).
    let nextSelection = v.selection;
    if (v.selection && v.selection.kind === 'by_delta') {
      const sign = option_type === 'P' ? -1 : 1;
      const mag = Math.abs(v.selection.target);
      nextSelection = { ...v.selection, target: mag * sign };
    }
    onChange({ ...v, option_type, selection: nextSelection });
  }, [v, onChange]);

  const setCycle = useCallback((rawCycle) => {
    const cycle = rawCycle === '' || rawCycle === '_any' ? null : rawCycle;
    emit({ cycle });
  }, [emit]);

  const setStream = useCallback((stream) => emit({ stream }), [emit]);

  // SELECT-AND-HOLD (fixed-contract dollar P&L) — used both by a SIGNALS
  // backtest (optional toggle below) and by a PORTFOLIO option price leg (the
  // ``holdRequired`` branch, where hold is always on).
  // ``hold_between_rolls`` freezes the contract between maturity rolls; when on,
  // ``nav_times`` is the premium-notional multiple used to size the held quantity
  // (direction stays the block WEIGHT SIGN, so nav_times is the SIZE — it can
  // exceed 1, which a weight ∈ [-100,100] cannot express).
  const setHoldBetweenRolls = useCallback((checked) => {
    // Seed a sensible nav_times default when turning hold on for the first time
    // (so the emitted ref always carries a valid multiple once hold is enabled).
    const patch = { hold_between_rolls: !!checked };
    if (checked && !(typeof v.nav_times === 'number' && v.nav_times > 0)) {
      patch.nav_times = 1.0;
    }
    // Reset the leverage tint on hold-off: the readout unmounts without
    // clearing the parent-held band, so a stale colour would flash on the
    // Size input's border for one frame when hold is re-enabled.
    if (!checked) setNavBand(null);
    emit(patch);
  }, [emit, v.nav_times]);

  const setNavTimes = useCallback((raw) => {
    // The control is the raw nav_times MULTIPLIER (factor, default 1 =
    // unlevered) — stored VERBATIM, no ×100 / ÷100. Clamp to a positive number
    // (the backend validator also enforces finite > 0); an empty / non-numeric /
    // non-positive entry falls back to 1.0 so the emitted ref stays valid.
    const parsed = parseFloat(raw);
    const value = Number.isFinite(parsed) && parsed > 0 ? parsed : 1.0;
    emit({ nav_times: value });
  }, [emit]);

  // ── Sizing mode (hold-mode $-P&L only) ────────────────────────────────────
  // Derived DEFAULT-safe view of the wire fields. An untouched leg carries
  // neither key → these resolve to the backend defaults (premium_notional /
  // nearest_on_or_after) WITHOUT emitting them, so a never-touched leg still
  // serialises byte-identically to today.  ``continuous_front`` is intentionally
  // NOT offered (backend not-implemented) — a persisted leg carrying it falls
  // back to the offered default so the <select> value stays valid.
  const sizingMode = v.sizing_mode === 'futures_notional' ? 'futures_notional' : 'premium_notional';
  const futuresReference = v.futures_reference === 'nearest_abs' ? 'nearest_abs' : 'nearest_on_or_after';

  const setSizingMode = useCallback((raw) => {
    const mode = raw === 'futures_notional' ? 'futures_notional' : 'premium_notional';
    const patch = { sizing_mode: mode };
    if (mode === 'futures_notional') {
      // Seed a valid reference (never continuous_front) so the emitted ref is
      // explicit + runnable; clear any stale premium-mode leverage tint (the
      // readout is hidden in futures mode and never fires onBand → null).
      if (v.futures_reference !== 'nearest_on_or_after' && v.futures_reference !== 'nearest_abs') {
        patch.futures_reference = 'nearest_on_or_after';
      }
      setNavBand(null);
    }
    emit(patch);
  }, [emit, v.futures_reference]);

  const setFuturesReference = useCallback((raw) => {
    const ref = raw === 'nearest_abs' ? 'nearest_abs' : 'nearest_on_or_after';
    emit({ futures_reference: ref });
  }, [emit]);

  // Roll offset is the unified {value, unit}. A legacy int (days-only) is read
  // as {value:int, unit:'days'}. Per-unit cap: days 0..365, months 0..12.
  const _normOffset = (ro) => {
    if (typeof ro === 'number') return { value: ro, unit: 'days' };
    if (ro && typeof ro === 'object') {
      return { value: Number.isFinite(ro.value) ? ro.value : 0, unit: ro.unit === 'months' ? 'months' : 'days' };
    }
    return { value: 0, unit: 'days' };
  };
  const _capFor = (unit) => (unit === 'months' ? 12 : 365);

  const setRollOffsetValue = useCallback((raw) => {
    const cur = _normOffset(v.roll_offset);
    const parsed = parseInt(raw, 10);
    const cap = _capFor(cur.unit);
    const value = Number.isNaN(parsed) ? 0 : Math.min(cap, Math.max(0, parsed));
    emit({ roll_offset: { value, unit: cur.unit } });
  }, [v.roll_offset, emit]);

  const setRollOffsetUnit = useCallback((unit) => {
    const cur = _normOffset(v.roll_offset);
    const nextUnit = unit === 'months' ? 'months' : 'days';
    // Re-clamp the existing value into the new unit's range when switching.
    const value = Math.min(_capFor(nextUnit), Math.max(0, cur.value));
    emit({ roll_offset: { value, unit: nextUnit } });
  }, [v.roll_offset, emit]);

  const setMaturityKind = useCallback((kind) => {
    emit({ maturity: defaultMaturity(kind) });
  }, [emit]);

  const setMaturityField = useCallback((field, raw) => {
    const next = { ...v.maturity };
    if (field === 'offset_months' || field === 'n' || field === 'target_days') {
      const parsed = parseInt(raw, 10);
      next[field] = Number.isNaN(parsed) ? 0 : parsed;
    } else {
      next[field] = raw;
    }
    emit({ maturity: next });
  }, [v.maturity, emit]);

  const setSelectionKind = useCallback((kind) => {
    emit({ selection: defaultSelection(kind, v.option_type) });
  }, [emit, v.option_type]);

  const setSelectionField = useCallback((field, raw) => {
    const next = { ...v.selection };
    if (field === 'strict') {
      next.strict = !!raw;
    } else {
      const parsed = parseFloat(raw);
      next[field] = Number.isNaN(parsed) ? 0 : parsed;
    }
    emit({ selection: next });
  }, [v.selection, emit]);

  const cycleSelectValue = v.cycle == null ? '_any' : v.cycle;
  // Cycle dropdown is scoped to the SELECTED root's real ``cycles`` tag-set
  // (see deriveCycleOptions), NOT the static superset — so a root never offers a
  // cycle it has no contracts for (which built an empty chain → HTTP 400).
  const selectedRoot = (availableRoots || []).find((r) => r.collection === v.collection);
  const cycleOptions = useMemo(
    () => deriveCycleOptions(selectedRoot ? selectedRoot.cycles : undefined, allowedCycles),
    [selectedRoot, allowedCycles],
  );
  // Truthful display of a stale / out-of-list persisted cycle: if the current
  // (persisted) cycle isn't among the derived options — e.g. a legacy signal
  // saved with 'Q' on OPT_SP_500, which no longer offers 'Q' — surface it as an
  // extra, clearly-labelled "(unavailable)" option so the <select> shows EXACTLY
  // what was saved. This is a RENDER-ONLY augmentation: it never mutates v.cycle
  // (no mount-time coercion), so it is uniformly safe in editable AND read-only
  // (disabled) mode. In editable mode the user consciously re-picks; in the
  // locked-signal read-only view the saved value is preserved verbatim. It is
  // deliberately NOT folded into deriveCycleOptions (which feeds the root-switch
  // coercion + default logic) — those must still treat the value as invalid.
  // (Root SWITCH still coerces via setRoot — a deliberate user edit, not a mount.)
  const renderedCycleOptions = useMemo(() => {
    const cur = v.cycle ?? null;
    if (cur == null) return cycleOptions;
    if (cycleOptions.some((o) => o.value === cur)) return cycleOptions;
    const label = `${CYCLE_LABELS[cur] || cur} (unavailable)`;
    return [...cycleOptions, { value: cur, label }];
  }, [cycleOptions, v.cycle]);
  // Legacy/absent roll_offset → {value:0, unit:'days'} (handles a shipped int).
  const rollOffset = _normOffset(v.roll_offset);

  return (
    <div className={styles.form} data-testid="option-stream-form" aria-disabled={disabled}>
      {/* Root */}
      <label className={styles.row}>
        <span className={styles.label}>Root</span>
        <select
          className={styles.input}
          value={v.collection}
          onChange={(e) => setRoot(e.target.value)}
          disabled={disabled}
          aria-label="Root"
        >
          {(availableRoots || []).length === 0 ? (
            <option value="">(no roots loaded)</option>
          ) : (
            (availableRoots || []).map((r) => (
              <option key={r.collection} value={r.collection}>
                {r.root_label || r.name || r.collection}
                {r.has_greeks === false ? ' (no greeks)' : ''}
              </option>
            ))
          )}
        </select>
      </label>

      {/* Option type */}
      <fieldset className={styles.row} disabled={disabled}>
        <legend className={styles.label}>Type</legend>
        <div className={styles.radioGroup}>
          {allowedOptionTypes.map((t) => (
            <label key={t} className={styles.radio}>
              <input
                type="radio"
                name={`option-type-${formId}`}
                value={t}
                checked={v.option_type === t}
                onChange={() => setOptionType(t)}
                disabled={disabled}
              />
              <span>{t === 'C' ? 'Call' : 'Put'}</span>
            </label>
          ))}
        </div>
      </fieldset>

      {/* Cycle */}
      <label className={styles.row}>
        <span className={styles.label}>Cycle</span>
        <select
          className={styles.input}
          value={cycleSelectValue}
          onChange={(e) => setCycle(e.target.value)}
          disabled={disabled}
          aria-label="Cycle"
        >
          {renderedCycleOptions.map((opt) => {
            const optValue = opt.value == null ? '_any' : opt.value;
            return <option key={optValue} value={optValue}>{opt.label}</option>;
          })}
        </select>
      </label>

      {/* Maturity rule */}
      <div className={styles.row}>
        <span className={styles.label}>Maturity</span>
        <div className={styles.subgroup}>
          <select
            className={styles.input}
            value={v.maturity.kind}
            onChange={(e) => setMaturityKind(e.target.value)}
            disabled={disabled}
            aria-label="Maturity rule"
          >
            {allowedMaturityKinds.map((k) => (
              <option key={k} value={k}>{MATURITY_LABELS[k] || k}</option>
            ))}
          </select>
          {v.maturity.kind === 'next_third_friday' && (
            <label className={styles.fieldInline}>
              Offset (months)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.offset_months}
                onChange={(e) => setMaturityField('offset_months', e.target.value)}
                disabled={disabled}
                aria-label="Offset months"
              />
            </label>
          )}
          {v.maturity.kind === 'nearest_to_target' && (
            <label className={styles.fieldInline}>
              Target DTE (days)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.target_days}
                onChange={(e) => setMaturityField('target_days', e.target.value)}
                disabled={disabled}
                aria-label="Target DTE days"
              />
            </label>
          )}
          {v.maturity.kind === 'end_of_month' && (
            <label className={styles.fieldInline}>
              Offset (months)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.offset_months}
                onChange={(e) => setMaturityField('offset_months', e.target.value)}
                disabled={disabled}
                aria-label="Offset months"
              />
            </label>
          )}
          {v.maturity.kind === 'plus_n_days' && (
            <label className={styles.fieldInline}>
              N (days)
              <input
                type="number"
                className={styles.input}
                value={v.maturity.n}
                onChange={(e) => setMaturityField('n', e.target.value)}
                disabled={disabled}
                aria-label="Plus N days"
              />
            </label>
          )}
          {v.maturity.kind === 'fixed' && (
            <label className={styles.fieldInline}>
              Date
              <input
                type="date"
                className={styles.input}
                value={v.maturity.date || ''}
                onChange={(e) => setMaturityField('date', e.target.value)}
                disabled={disabled}
                aria-label="Fixed date"
              />
            </label>
          )}
        </div>
      </div>

      {/* Roll early by — the ROLL-EARLY axis (value + unit). DISTINCT from the
          maturity's own month offset (the TARGET-month axis above). "Roll at end
          of month" is the End of Month MATURITY, not a roll-offset value. */}
      <label className={styles.row}>
        <span className={styles.label}>Roll early by</span>
        <div className={styles.subgroup}>
          <input
            type="number"
            className={styles.input}
            min={0}
            max={rollOffset.unit === 'months' ? 12 : 365}
            step={1}
            value={rollOffset.value}
            onChange={(e) => setRollOffsetValue(e.target.value)}
            disabled={disabled}
            aria-label="Roll offset value"
            title="Roll this much earlier — the maturity rule is resolved as of (date + offset), so the roll fires sooner. 0 = roll at the rule's natural time. This is separate from the maturity's own month offset (which expiration to target)."
          />
          <select
            className={styles.input}
            value={rollOffset.unit}
            onChange={(e) => setRollOffsetUnit(e.target.value)}
            disabled={disabled}
            aria-label="Roll offset unit"
          >
            <option value="days">days</option>
            <option value="months">months</option>
          </select>
        </div>
      </label>

      {/* Selection criterion */}
      <div className={styles.row}>
        <span className={styles.label}>Selection</span>
        <div className={styles.subgroup}>
          <select
            className={styles.input}
            value={v.selection.kind}
            onChange={(e) => setSelectionKind(e.target.value)}
            disabled={disabled}
            aria-label="Selection criterion"
          >
            {allowedSelectionKinds.map((k) => (
              <option key={k} value={k}>{SELECTION_LABELS[k] || k}</option>
            ))}
          </select>
          {v.selection.kind === 'by_strike' && (
            <label className={styles.fieldInline}>
              Strike
              <input
                type="number"
                className={styles.input}
                step="any"
                value={v.selection.strike}
                onChange={(e) => setSelectionField('strike', e.target.value)}
                disabled={disabled}
                aria-label="Strike"
              />
            </label>
          )}
          {v.selection.kind === 'by_moneyness' && (
            <>
              <label className={styles.fieldInline}>
                Target K/S
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.target}
                  onChange={(e) => setSelectionField('target', e.target.value)}
                  disabled={disabled}
                  aria-label="Moneyness target"
                />
              </label>
              <label className={styles.fieldInline}>
                Tolerance
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.tolerance}
                  onChange={(e) => setSelectionField('tolerance', e.target.value)}
                  disabled={disabled}
                  aria-label="Moneyness tolerance"
                />
              </label>
            </>
          )}
          {v.selection.kind === 'by_delta' && (
            <>
              <label className={styles.fieldInline}>
                Delta
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.target}
                  onChange={(e) => setSelectionField('target', e.target.value)}
                  disabled={disabled}
                  aria-label="Delta target"
                />
              </label>
              <label className={styles.fieldInline}>
                Tolerance
                <input
                  type="number"
                  className={styles.input}
                  step="any"
                  value={v.selection.tolerance}
                  onChange={(e) => setSelectionField('tolerance', e.target.value)}
                  disabled={disabled}
                  aria-label="Delta tolerance"
                />
              </label>
              <label className={styles.fieldInline} title="When checked, reject dates where no contract is within tolerance (NaN). When unchecked, use the closest match.">
                <input
                  type="checkbox"
                  checked={!!v.selection.strict}
                  onChange={(e) => setSelectionField('strict', e.target.checked)}
                  disabled={disabled}
                  aria-label="Strict"
                />
                Strict (NaN if no match, closest otherwise)
              </label>
            </>
          )}
        </div>
      </div>

      {/* Series — plainly labelled (no longer hidden behind a disclosure).
          Defaults to `mid` (the bid-ask midpoint — the option premium mark);
          the user can extract iv / a greek / volume / open interest instead.
          "Mid price" is explicitly the BID-ASK MID (see the help glyph +
          tooltip), NOT a daily OHLC field.

          Hidden entirely when the form is restricted to a single stream (the
          portfolio price-only flow): there is no choice to surface, so a
          1-item dropdown would be pointless noise. The stream is pinned by the
          coercion effect above. */}
      {!singleStream && (
        <label className={styles.row}>
          <span className={styles.label}>
            Series:
            {/* Help glyph: always present so the Mid tooltip is discoverable
                regardless of the current selection. */}
            <span
              className={styles.help}
              data-testid="mid-tooltip"
              role="img"
              aria-label={MID_TOOLTIP}
              title={MID_TOOLTIP}
            >
              ⓘ
            </span>
          </span>
          <select
            className={styles.input}
            value={v.stream}
            onChange={(e) => setStream(e.target.value)}
            disabled={disabled}
            aria-label="Series"
          >
            {allowedStreams.map((s) => (
              <option key={s} value={s}>{STREAM_LABELS[s] || s}</option>
            ))}
          </select>
        </label>
      )}

      {/* No adjustment control: option continuous series carry no
          back-adjustment (ratio/difference are ill-posed for option premia).
          The series is always the raw stitched stream. */}

      {/* SELECT-AND-HOLD (fixed-contract dollar P&L) — serves both a SIGNALS
          backtest (optional toggle) and a PORTFOLIO option price leg (the
          ``holdRequired`` branch just below, where hold is always required).
          Freezes the contract between maturity rolls so a delta/moneyness-selected
          option's P&L is a proper fixed-contract dollar P&L (qty·Δpremium, sized
          off NAV at each roll) instead of a %-return that explodes as a held
          premium decays.  nav_times (the premium-notional SIZE) shows only when
          hold is on; direction stays the block weight sign. */}
      {holdRequired ? (
        /* PORTFOLIO option price leg: hold ON only — no on/off toggle. Static
           note + always-visible nav_times + wipeout hint. The backend REQUIRES
           hold for an option price leg (mid/bs_mid), so there is no valid "off". */
        <label className={styles.row}>
          <span className={styles.label}>Backtest P&amp;L</span>
          <div className={styles.subgroup}>
            <span className={styles.fieldInline} data-testid="hold-required-note">
              Held between rolls — fixed-contract $-P&amp;L (required for option legs)
            </span>
            <SizeAndLeverage
              streamValue={v}
              availableRoots={availableRoots}
              referenceDate={referenceDate}
              navBand={navBand}
              setNavBand={setNavBand}
              onNavTimes={setNavTimes}
              sizingMode={sizingMode}
              onSizingMode={setSizingMode}
              futuresReference={futuresReference}
              onFuturesReference={setFuturesReference}
              disabled={disabled}
            />
          </div>
        </label>
      ) : showHoldControls ? (
        <label className={styles.row}>
          <span className={styles.label}>Backtest P&amp;L</span>
          <div className={styles.subgroup}>
            <label
              className={styles.fieldInline}
              title="Hold the selected contract between maturity rolls and book fixed-contract dollar P&L (qty·Δpremium, quantity sized off NAV at each roll). Off = the daily-reselected mid %-return, which is meaningless for a delta/moneyness-selected option (it explodes as a held premium decays toward zero)."
            >
              <input
                type="checkbox"
                checked={!!v.hold_between_rolls}
                onChange={(e) => setHoldBetweenRolls(e.target.checked)}
                disabled={disabled}
                aria-label="Hold contract between rolls (fixed-contract P&L)"
                data-testid="hold-between-rolls"
              />
              Hold contract between rolls (fixed-contract P&amp;L)
            </label>
            {v.hold_between_rolls && (
              <SizeAndLeverage
                streamValue={v}
                availableRoots={availableRoots}
                referenceDate={referenceDate}
                navBand={navBand}
                setNavBand={setNavBand}
                onNavTimes={setNavTimes}
                sizingMode={sizingMode}
                onSizingMode={setSizingMode}
                futuresReference={futuresReference}
                onFuturesReference={setFuturesReference}
                disabled={disabled}
              />
            )}
          </div>
        </label>
      ) : null}

      {validation && (
        <div
          className={styles.validation}
          role="alert"
          data-testid="option-stream-validation"
          data-error-code={validation.code}
        >
          {validation.message}
        </div>
      )}
    </div>
  );
}

// Public helpers — used by composing components (the Continuous tab reuses
// these when it builds its own initial value).
export {
  ALL_OPTION_TYPES,
  ALL_CYCLES,
  ALL_STREAMS,
  ALL_MATURITY_KINDS,
  ALL_SELECTION_KINDS,
  GREEK_STREAMS,
  MID_TOOLTIP,
  SYNTHETIC_WEEKLY_LABEL,
  CYCLE_LABELS,
  SIZING_MODE_LABELS,
  FUTURES_REFERENCE_LABELS,
  FUTURES_NOTIONAL_HELP,
  SIZE_LABEL_FUTURES,
  SIZE_LABEL_PREMIUM,
  defaultMaturity,
  defaultSelection,
  deriveCycleOptions,
  pickDefaultCycle,
};
