import styles from './IntradayBacktestPage.module.css';
import ConditionEditor, {
  HEDGE_CONDITION_TYPES,
  defaultCondition,
  serializeCondition,
} from './ConditionEditor';

// ---------------------------------------------------------------------------
// HedgeModule — the delta-hedge "rule module" (DESIGN.md v4). Replaces the flat
// hedge:{enabled,interval_minutes,delta_band} controls with a configurable
// module mirroring the Entry/Exit modules:
//
//   Enable toggle
//   "Hedge with" instrument dropdown (es_future only for v1; extensible)
//   Triggers (OR): interval (min) · delta band · σ-move {enable, n}
//   Conditions (AND): an "Add condition" dropdown limited to the HEDGE types
//     (max_spread, min_quote_size, min_rehedge_delta) → ConditionEditor rows
//   Target: zero | band_edge | ratio {ratio shown only for ratio}
//
// Value shape (PINNED, DESIGN.md v4):
//   { enabled, instrument,
//     triggers: { interval_minutes, delta_band, sigma_move: { enabled, n } },
//     conditions: [{type,...}],
//     target: { mode, ratio } }
// ---------------------------------------------------------------------------

// v1 instrument catalogue for "Hedge with". One entry today; the selector +
// field exist so more hedging instruments can be added later without a schema
// change. Value is the PINNED wire token.
export const HEDGE_INSTRUMENTS = [
  { value: 'es_future', label: 'ES future' },
];

// Target-mode catalogue: how much delta a rehedge removes (DESIGN.md v4).
export const HEDGE_TARGET_MODES = [
  { value: 'zero', label: 'Hedge to neutral (0)' },
  { value: 'band_edge', label: 'Hedge to band edge' },
  { value: 'ratio', label: 'Partial (ratio)' },
];

// In-app help for the interval trigger. Copy mirrors what the engine does
// (tcg/engine/intraday_backtest.py): the straddle's net delta is re-hedged with
// the ES future on a fixed clock OR when a trigger fires — whichever first.
export const HEDGE_INTERVAL_HELP = "How often the straddle's delta is re-hedged with "
  + 'the ES future on a fixed clock: every N minutes the position is brought back '
  + 'toward delta-neutral, no matter how little it has drifted. Smaller = more frequent '
  + 'hedging (tighter neutrality, more hedge trades); larger = looser. A rehedge is also '
  + 'considered early if the delta drifts past the band or the σ-move trigger fires.';

// In-app help for the delta-band trigger.
export const DELTA_BAND_HELP = 'Consider a rehedge as soon as the open (unhedged) net '
  + 'delta drifts past this threshold, in ES-future-equivalent units (~0–1 for one ATM '
  + 'straddle), without waiting for the clock. Smaller = react to smaller moves '
  + '(tighter neutrality, more trades); larger = tolerate more drift; 0 considers a '
  + 'rehedge on every bar. Whichever fires first — the timed rehedge, the band, or the '
  + 'σ-move — triggers a rehedge.';

// In-app help for the σ-move trigger (Gael copy from the brief).
export const SIGMA_MOVE_HELP = 'Rehedge when the underlying has moved ≥ n×σ since the '
  + 'last hedge — e.g. n=1 to wait for a 1σ move. σ is the implied move-to-expiry, so '
  + 'it shrinks intraday as expiry nears. Enable this and turn the timed and band '
  + 'triggers off to "wait for a 1σ move before hedging".';

// Default hedge module (matches the prior flat defaults: enabled, 15-min
// interval, 0.10 band; σ-move off; hedge-to-neutral).
export function defaultHedgeModule() {
  return {
    enabled: true,
    instrument: 'es_future',
    triggers: {
      interval_minutes: 15,
      delta_band: 0.10,
      sigma_move: { enabled: false, n: 1.0 },
    },
    conditions: [],
    target: { mode: 'zero', ratio: 1.0 },
  };
}

// A trigger field is BLANK (cleared) when empty/undefined/null. A blank
// interval or delta band means the trigger is OFF and must serialize to `null`
// — NOT 0. This matters for the band: the backend treats `delta_band: 0` as
// "fire every bar", so blank→0 would silently pre-empt the σ trigger. Blank→null
// keeps "off" (blank) distinct from "every bar" (explicit 0).
export function isBlank(v) {
  return v === '' || v === null || v === undefined;
}

// A blank field → null (OFF); otherwise the numeric value (0 stays 0).
function numOrNull(v) {
  if (isBlank(v)) return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
}

// True when NO rehedge trigger is armed while hedging is enabled: interval OFF
// (blank or 0 — contract: 0/null = off for interval) AND band OFF (blank only —
// band 0 = fire-every-bar = ON) AND σ-move disabled. Such a hedge could never
// rehedge, so the page blocks Run and the module shows a hint.
export function hedgeTriggersAllOff(h) {
  const t = (h && h.triggers) || {};
  const intervalOff = isBlank(t.interval_minutes) || Number(t.interval_minutes) === 0;
  const bandOff = isBlank(t.delta_band);
  const sig = t.sigma_move || {};
  return intervalOff && bandOff && !sig.enabled;
}

// Serialize the hedge module to the PINNED v4 wire shape. Coerces numbers,
// whitelists conditions per type, and always emits the nested triggers/target
// objects. A cleared interval/band serializes as `null` (OFF), not 0. The OLD
// flat interval_minutes/delta_band top-level fields are gone.
export function serializeHedge(h) {
  const m = h || {};
  const trig = m.triggers || {};
  const sig = trig.sigma_move || {};
  const tgt = m.target || {};
  return {
    enabled: Boolean(m.enabled),
    instrument: m.instrument || 'es_future',
    triggers: {
      interval_minutes: numOrNull(trig.interval_minutes),
      delta_band: numOrNull(trig.delta_band),
      sigma_move: {
        enabled: Boolean(sig.enabled),
        n: Number(sig.n),
      },
    },
    conditions: (m.conditions || []).map(serializeCondition),
    target: {
      mode: tgt.mode || 'zero',
      ratio: Number(tgt.ratio),
    },
  };
}

export default function HedgeModule({ value, onChange }) {
  const v = value || {};
  const triggers = v.triggers || {};
  const sigma = triggers.sigma_move || {};
  const conditions = v.conditions || [];
  const target = v.target || {};
  const enabled = Boolean(v.enabled);

  const set = (patch) => onChange({ ...v, ...patch });
  const setTrigger = (patch) => set({ triggers: { ...triggers, ...patch } });
  const setSigma = (patch) => setTrigger({ sigma_move: { ...sigma, ...patch } });
  const setTarget = (patch) => set({ target: { ...target, ...patch } });

  const addCondition = (type) => {
    if (!type) return;
    set({ conditions: [...conditions, defaultCondition(type)] });
  };
  const updateCondition = (i, next) => {
    set({ conditions: conditions.map((c, idx) => (idx === i ? next : c)) });
  };
  const removeCondition = (i) => {
    set({ conditions: conditions.filter((_, idx) => idx !== i) });
  };

  const sub = !enabled; // sub-controls disabled when the module is off

  return (
    <div className={styles.module} data-testid="hedge-module">
      <div className={styles.moduleFields}>
        {/* Enable — same affordance as before (aria-label preserved). */}
        <label className={`${styles.field} ${styles.checkboxRow}`}>
          <input
            type="checkbox"
            aria-label="Delta-hedge enabled"
            checked={enabled}
            onChange={(e) => set({ enabled: e.target.checked })}
          />
          <span>Delta-hedge</span>
        </label>

        {/* "Hedge with" instrument — one option today, architected for more. */}
        <label className={styles.field}>
          <span>Hedge with</span>
          <select
            aria-label="Hedge with instrument"
            data-testid="hedge-instrument"
            value={v.instrument || 'es_future'}
            disabled={sub}
            onChange={(e) => set({ instrument: e.target.value })}
          >
            {HEDGE_INSTRUMENTS.map((it) => (
              <option key={it.value} value={it.value}>{it.label}</option>
            ))}
          </select>
        </label>
      </div>

      {/* Triggers (OR): interval · delta band · σ-move. */}
      <div className={styles.conditionsBlock} data-testid="hedge-triggers-block">
        <div className={styles.conditionsHeader}>
          <span className={styles.conditionsTitle}>Triggers (rehedge when ANY fires)</span>
        </div>
        <div className={styles.moduleFields}>
          <label className={styles.field}>
            <span className={styles.labelRow}>
              Hedge interval (min)
              {isBlank(triggers.interval_minutes) && (
                <span className={styles.statLabel} data-testid="hedge-interval-off"> · off</span>
              )}
              <span
                className={styles.help}
                data-testid="hedge-interval-help"
                role="img"
                aria-label={HEDGE_INTERVAL_HELP}
                title={HEDGE_INTERVAL_HELP}
              >
                ⓘ
              </span>
            </span>
            <input
              type="number"
              min={0}
              aria-label="Hedge interval minutes"
              placeholder="off (blank)"
              value={triggers.interval_minutes ?? ''}
              disabled={sub}
              onChange={(e) => setTrigger({ interval_minutes: e.target.value })}
            />
          </label>

          <label className={styles.field}>
            <span className={styles.labelRow}>
              Delta band
              {isBlank(triggers.delta_band) && (
                <span className={styles.statLabel} data-testid="hedge-band-off"> · off</span>
              )}
              <span
                className={styles.help}
                data-testid="delta-band-help"
                role="img"
                aria-label={DELTA_BAND_HELP}
                title={DELTA_BAND_HELP}
              >
                ⓘ
              </span>
            </span>
            <input
              type="number"
              step="0.01"
              min={0}
              aria-label="Delta band"
              placeholder="off (blank)"
              value={triggers.delta_band ?? ''}
              disabled={sub}
              onChange={(e) => setTrigger({ delta_band: e.target.value })}
            />
          </label>

          {/* σ-move: enable toggle + n. */}
          <label className={`${styles.field} ${styles.checkboxRow}`}>
            <input
              type="checkbox"
              aria-label="Hedge sigma-move enabled"
              data-testid="hedge-sigma-enable"
              checked={Boolean(sigma.enabled)}
              disabled={sub}
              onChange={(e) => setSigma({ enabled: e.target.checked })}
            />
            <span className={styles.labelRow}>
              σ-move
              <span
                className={styles.help}
                data-testid="hedge-sigma-help"
                role="img"
                aria-label={SIGMA_MOVE_HELP}
                title={SIGMA_MOVE_HELP}
              >
                ⓘ
              </span>
            </span>
          </label>
          <label className={styles.field}>
            <span>σ-move n</span>
            <input
              type="number"
              step="0.1"
              min={0}
              aria-label="Hedge sigma-move n"
              data-testid="hedge-sigma-n"
              value={sigma.n ?? ''}
              disabled={sub || !sigma.enabled}
              onChange={(e) => setSigma({ n: e.target.value })}
            />
          </label>
        </div>
        {enabled && hedgeTriggersAllOff(v) && (
          <p
            className={styles.error}
            data-testid="hedge-no-trigger-hint"
            role="alert"
          >
            No rehedge trigger is armed — turn on the interval, the delta band, or σ-move
            (or disable hedging). The hedge can never fire otherwise.
          </p>
        )}
      </div>

      {/* Conditions (AND) — reuses ConditionEditor; the "Add condition" dropdown
          is limited to the HEDGE condition types (no entry/exit types leak in). */}
      <div className={styles.conditionsBlock}>
        <div className={styles.conditionsHeader}>
          <span className={styles.conditionsTitle}>Conditions (execute only if ALL pass)</span>
          <select
            className={styles.addCondition}
            data-testid="hedge-add-condition"
            aria-label="Hedge add condition"
            value=""
            disabled={sub}
            onChange={(e) => addCondition(e.target.value)}
          >
            <option value="" disabled>Add condition…</option>
            {HEDGE_CONDITION_TYPES.map((t) => (
              <option key={t.type} value={t.type}>{t.label}</option>
            ))}
          </select>
        </div>

        {conditions.length === 0 ? (
          <p className={styles.conditionsEmpty} data-testid="hedge-conditions-empty">
            No conditions — a considered rehedge always executes.
          </p>
        ) : (
          <div className={styles.conditionsList} data-testid="hedge-conditions">
            {conditions.map((c, i) => (
              <ConditionEditor
                key={c._id || i}
                idPrefix="hedge"
                condition={c}
                onChange={(next) => updateCondition(i, next)}
                onRemove={() => removeCondition(i)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Target: how much delta a rehedge removes. Ratio input only for ratio. */}
      <div className={styles.moduleFields}>
        <label className={styles.field}>
          <span>Hedge target</span>
          <select
            aria-label="Hedge target"
            data-testid="hedge-target"
            value={target.mode || 'zero'}
            disabled={sub}
            onChange={(e) => setTarget({ mode: e.target.value })}
          >
            {HEDGE_TARGET_MODES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </label>
        {target.mode === 'ratio' && (
          <label className={styles.field}>
            <span>Ratio</span>
            <input
              type="number"
              step="0.05"
              min={0}
              max={1}
              aria-label="Hedge ratio"
              data-testid="hedge-ratio"
              value={target.ratio ?? ''}
              disabled={sub}
              onChange={(e) => setTarget({ ratio: e.target.value })}
            />
          </label>
        )}
      </div>
    </div>
  );
}
