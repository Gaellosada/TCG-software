import styles from './IntradayBacktestPage.module.css';
import ConditionEditor, {
  CONDITION_TYPES,
  defaultCondition,
  serializeCondition,
} from './ConditionEditor';

// ---------------------------------------------------------------------------
// EntryExitModule — one reusable "rule module" for either the Entry or the Exit
// leg (DESIGN.md v2). A module is: a time (ET) + its OWN snap tolerance + a
// Conditions builder (an "Add condition" dropdown → the chosen type's param
// inputs, each removable). The SAME component is reused inside a custom-day row
// as a partial override (empty fields inherit the global module).
//
// Value shape: { time, snap_tolerance_minutes, conditions:[{type,...}] }.
//  - Global module: all fields populated; serialized in full.
//  - Partial override (partial=true): any empty field means "inherit global";
//    only the fields the user set are serialized (see serializePartialModule).
// ---------------------------------------------------------------------------

// In-app help for the snap-tolerance field (Gael copy). Now lives on EACH
// module (DESIGN.md v2) since entry and exit each own a snap tolerance. It also
// bounds how far apart the two legs may fill — see the leg-timing note.
export const SNAP_HELP = 'Intraday option quotes are sparse. If your exact '
  + 'entry/exit minute has no quote, the engine uses the nearest one within this '
  + 'many minutes; if none exists in that window, the day is skipped. It also '
  + 'bounds how far apart the call and put legs may fill. Higher = fewer skipped '
  + 'days but looser fills; lower = tighter timing but more skips.';

// Default global entry/exit modules.
export function defaultEntryModule() {
  return { time: '10:00', snap_tolerance_minutes: 10, conditions: [] };
}
export function defaultExitModule() {
  return { time: '15:45', snap_tolerance_minutes: 10, conditions: [] };
}
// A blank partial override (all fields inherit until the user sets them).
export function emptyPartialModule() {
  return { time: '', snap_tolerance_minutes: '', conditions: [] };
}

// Serialize a full module to the PINNED wire shape.
export function serializeModule(mod) {
  const m = mod || {};
  return {
    time: m.time,
    snap_tolerance_minutes: Number(m.snap_tolerance_minutes),
    conditions: (m.conditions || []).map(serializeCondition),
  };
}

// Serialize a PARTIAL override: only include fields the user actually set. If
// nothing was set, return null (the custom-day row then omits this override so
// the day inherits the global module).
export function serializePartialModule(mod) {
  if (!mod) return null;
  const out = {};
  if (mod.time) out.time = mod.time;
  if (mod.snap_tolerance_minutes !== '' && mod.snap_tolerance_minutes != null) {
    out.snap_tolerance_minutes = Number(mod.snap_tolerance_minutes);
  }
  if (mod.conditions && mod.conditions.length) {
    out.conditions = mod.conditions.map(serializeCondition);
  }
  return Object.keys(out).length ? out : null;
}

export default function EntryExitModule({
  title,
  idPrefix,
  value,
  onChange,
  partial = false,
}) {
  const v = value || {};
  const conditions = v.conditions || [];
  const set = (patch) => onChange({ ...v, ...patch });

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

  return (
    <div className={styles.module} data-testid={`${idPrefix}-module`}>
      <div className={styles.moduleFields}>
        <label className={styles.field}>
          <span>Time (ET)</span>
          <input
            type="time"
            aria-label={`${title} time (ET)`}
            value={v.time || ''}
            onChange={(e) => set({ time: e.target.value })}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.labelRow}>
            Snap tolerance (min)
            <span
              className={styles.help}
              data-testid={`${idPrefix}-snap-help`}
              role="img"
              aria-label={SNAP_HELP}
              title={SNAP_HELP}
            >
              ⓘ
            </span>
          </span>
          <input
            type="number"
            min="0"
            aria-label={`${title} snap tolerance (minutes)`}
            placeholder={partial ? 'inherit' : undefined}
            value={v.snap_tolerance_minutes ?? ''}
            onChange={(e) => set({ snap_tolerance_minutes: e.target.value })}
          />
        </label>
      </div>

      <div className={styles.conditionsBlock}>
        <div className={styles.conditionsHeader}>
          <span className={styles.conditionsTitle}>
            Conditions{partial ? ' (override)' : ''}
          </span>
          {/* "Add condition" dropdown: picking a type appends its row, then the
              select snaps back to the placeholder (value is fixed to ""). */}
          <select
            className={styles.addCondition}
            data-testid={`${idPrefix}-add-condition`}
            aria-label={`${title} add condition`}
            value=""
            onChange={(e) => addCondition(e.target.value)}
          >
            <option value="" disabled>Add condition…</option>
            {CONDITION_TYPES.map((t) => (
              <option key={t.type} value={t.type}>{t.label}</option>
            ))}
          </select>
        </div>

        {conditions.length === 0 ? (
          <p
            className={styles.conditionsEmpty}
            data-testid={`${idPrefix}-conditions-empty`}
          >
            No conditions — fills at the first available quote in the snap window.
          </p>
        ) : (
          <div className={styles.conditionsList} data-testid={`${idPrefix}-conditions`}>
            {conditions.map((c, i) => (
              <ConditionEditor
                key={c._id || i}
                idPrefix={idPrefix}
                condition={c}
                onChange={(next) => updateCondition(i, next)}
                onRemove={() => removeCondition(i)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
