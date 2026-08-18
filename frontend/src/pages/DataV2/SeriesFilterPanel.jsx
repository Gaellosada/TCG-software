import { useState, useMemo, useEffect, useRef, useId } from 'react';
import { useObjectFacetsV2 } from '../../hooks/marketQueries';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/**
 * Parse a strike input into the value ``getObjectSeriesV2`` wants.
 *
 * '' / null / undefined mean "no bound" → ``undefined`` so the key is omitted
 * from the request entirely (NOT 0, which is a real bound that matches nothing
 * useful). Anything that does not parse to a finite number is also treated as
 * "no bound": the client rejects a non-finite bound with a ``TypeError``, and
 * turning a half-typed '1e' into a user-visible error would be a UI bug.
 *
 * With ``<input type="number">`` the non-finite branch is unreachable from the
 * DOM — both jsdom and Blink run the HTML value-sanitisation algorithm and
 * replace '1e' / 'abc' / '-' / '1e999' with '' before React ever sees them
 * (verified in jsdom). It is kept as defence-in-depth for a programmatic
 * caller, a future switch to ``type="text"``, or a URL-restored value once the
 * filter state moves into the query string.
 *
 * Exported for direct testing precisely because that branch cannot be reached
 * through the rendered control.
 */
export function parseStrikeBound(raw) {
  if (raw === undefined || raw === null || raw === '') return undefined;
  const num = Number(raw);
  return Number.isFinite(num) ? num : undefined;
}

const DEFAULTS = Object.freeze({
  expiration: '',
  strikeMin: '',
  strikeMax: '',
  optionType: 'both',
  serieType: 'any',
  freq: 'any',
});

/**
 * Field values for one applied filter set (or the DEFAULTS for "none").
 *
 * Only ``expirationMin`` is read: the panel offers a single expiration choice
 * and emits it as the one-day window [min, max], so min is the round-trip of
 * what the control can express. The strike bounds arrive as numbers and an
 * ``<input>``'s value must be a string, hence ``String(...)`` — a raw number
 * renders as an empty field, i.e. a bound silently dropped from the UI while
 * still being applied to the results.
 */
function seeds(from) {
  if (from == null) return DEFAULTS;
  return {
    expiration: from.expirationMin || '',
    strikeMin: from.strikeMin != null ? String(from.strikeMin) : '',
    strikeMax: from.strikeMax != null ? String(from.strikeMax) : '',
    optionType: from.optionType || DEFAULTS.optionType,
    serieType: from.serieType || DEFAULTS.serieType,
    freq: from.freq || DEFAULTS.freq,
  };
}

/**
 * Persistent filter panel for an object's series.
 *
 * Two behaviours the design turns on:
 *  - Nothing is fetched until the user clicks Apply, so an unbounded series
 *    request can never be issued (the 38 MB payload this page used to send).
 *  - After that first Apply, every field change auto-applies. Each query is
 *    bounded by `limit` and sub-second, so the gate has done its job and
 *    re-clicking Apply would just be friction.
 *
 * The panel STAYS MOUNTED beside the results — changing one dimension must not
 * cost re-entering the others.
 *
 * Controls are driven by `/facets`, so only dimensions that exist for this
 * object are rendered: an index or rate object has no contracts, hence no
 * expiration or strike control.
 *
 * The emitted object uses only the camelCase keys the client allowlists
 * (``expirationMin``, ``expirationMax``, ``strikeMin``, ``strikeMax``,
 * ``optionType``, ``serieType``, ``freq``); any other key is a synchronous
 * ``TypeError`` in ``getObjectSeriesV2``. Pagination (``skip``/``limit``) is
 * NOT this component's business — the result list owns it.
 *
 * ``onReset`` is REQUIRED for Reset to mean anything. Reset re-arms this
 * component's own gate, but the parent holds the applied filters and the results
 * they produced; without being told, it keeps showing the page from the filters
 * that were just cleared, and no fetch is issued to correct it. The user then
 * sees a blank panel next to a stale list and nothing saying so — Reset appears
 * to do nothing. So Reset notifies, and deliberately does NOT emit ``onApply``:
 * clearing the fields is not the same as applying an empty filter, which would
 * be the unbounded request this whole component exists to prevent.
 *
 * ``initialFilters`` (in practice: the filter state read back out of the URL)
 * is a filter set the parent HAS ALREADY APPLIED. So it seeds the fields *and*
 * starts the gate open — the recipient of a shared link must not have to press
 * Apply to see the results the link describes. This does not re-open the
 * unbounded request the gate exists to stop: that request is the one with NO
 * filters, and a URL that carries filter keys is by construction bounded. A URL
 * with no filter keys yields ``null`` here and the gate stays shut.
 */
function SeriesFilterPanel({ objectId, initialFilters = null, onApply, onReset }) {
  const { data: facets, loading, error } = useObjectFacetsV2(objectId);
  const uid = useId();

  const initial = seeds(initialFilters);
  const [expiration, setExpiration] = useState(initial.expiration);
  const [strikeMin, setStrikeMin] = useState(initial.strikeMin);
  const [strikeMax, setStrikeMax] = useState(initial.strikeMax);
  const [optionType, setOptionType] = useState(initial.optionType);
  const [serieType, setSerieType] = useState(initial.serieType);
  const [freq, setFreq] = useState(initial.freq);

  // False until the first explicit Apply; gates auto-application. Reset puts it
  // back to false, so the gate is re-armed rather than being a one-time thing.
  // A restored filter set starts applied — see ``initialFilters`` above.
  const [applied, setApplied] = useState(initialFilters != null);

  /*
   * "The applied state we are currently showing came from the parent, not from
   * the user" — so the auto-apply effect must record it instead of emitting it.
   *
   * Echoing ``initialFilters`` straight back would look to the parent like a
   * fresh application: its "a new filter starts from page 1" rule then fires,
   * and a shared link's ``?skip=50`` lands on page 1 — plus a redundant history
   * entry and refetch. Unlike the general "skip the first effect run" flag this
   * component deliberately avoids (see ``lastEmitted``), this one is armed only
   * when a seed happens and is consumed by the effect run that seed causes, so
   * it can never be left armed to swallow a later change.
   */
  const seeded = useRef(initialFilters != null);

  /*
   * A new object re-arms the gate and clears the fields.
   *
   * Today ``DataV2Page`` renders ``<ObjectDetail key={object_id}>``, so this
   * panel remounts per object and the situation cannot arise. But the gate is
   * the entire reason this component exists, and it must not depend on a
   * ``key=`` in a different file: without this, applying on an option root and
   * then switching to another object would auto-apply the previous object's
   * filters immediately — a fetch the user never asked for, with the wrong
   * strike/expiration window, and the gate silently bypassed for the new
   * object. Adjusting state during render (rather than in an effect) is the
   * documented React pattern here; it re-renders before commit, so the
   * auto-apply effect never observes the stale combination.
   *
   * "Clears the fields" means "back to what the parent says is applied", which
   * with no ``initialFilters`` is the DEFAULTS and the gate shut. If the parent
   * does hold an applied filter set (a URL-restored one), re-seeding from it is
   * what keeps panel and results agreeing — clearing to blank there would
   * reproduce exactly the panel-says-nothing / list-shows-something mismatch
   * that ``onReset`` exists to prevent.
   */
  const prevObjectId = useRef(objectId);
  if (prevObjectId.current !== objectId) {
    prevObjectId.current = objectId;
    setExpiration(initial.expiration);
    setStrikeMin(initial.strikeMin);
    setStrikeMax(initial.strikeMax);
    setOptionType(initial.optionType);
    setSerieType(initial.serieType);
    setFreq(initial.freq);
    setApplied(initialFilters != null);
    seeded.current = initialFilters != null;
  }

  const hasContracts = (facets?.expirations?.length || 0) > 0;
  const hasStrikes = facets?.strike_min != null && facets?.strike_max != null;
  const hasOptionTypes = (facets?.option_types?.length || 0) > 0;

  // Distinct type / freq values actually present on this object.
  const serieTypeValues = useMemo(() => {
    const s = new Set((facets?.serie_types || []).map((t) => t.type));
    return Array.from(s).sort();
  }, [facets]);
  const freqValues = useMemo(() => {
    const s = new Set((facets?.serie_types || []).map((t) => t.freq));
    return Array.from(s).sort();
  }, [facets]);

  const filters = useMemo(() => ({
    // A single expiration choice maps to a closed [min, max] window of one day.
    expirationMin: expiration || undefined,
    expirationMax: expiration || undefined,
    strikeMin: parseStrikeBound(strikeMin),
    strikeMax: parseStrikeBound(strikeMax),
    optionType,
    serieType,
    freq,
  }), [expiration, strikeMin, strikeMax, optionType, serieType, freq]);

  const onApplyRef = useRef(onApply);
  onApplyRef.current = onApply;
  const onResetRef = useRef(onReset);
  onResetRef.current = onReset;

  /*
   * ``lastEmitted`` holds the exact ``filters`` object last handed to onApply.
   * Because ``filters`` is memoised on the six field values, identity equality
   * is precisely "these filters have already been emitted", which is all the
   * auto-apply effect needs to avoid re-emitting on an unrelated re-render.
   *
   * A "skip the first run" flag was the obvious alternative and is subtly
   * wrong: the Apply click emits without changing ``filters``, so the flag has
   * to be re-armed inside the click handler — and then a SECOND (redundant)
   * Apply click leaves it armed, because the effect never re-runs to consume
   * it, and the next genuine field change gets silently swallowed. Comparing
   * identities has no such state to get out of sync.
   */
  const lastEmitted = useRef(null);
  function emit(next) {
    lastEmitted.current = next;
    onApplyRef.current(next);
  }

  useEffect(() => {
    if (!applied) return;                        // gated: nothing fetched yet
    if (seeded.current) {                        // the parent's own applied set
      seeded.current = false;
      lastEmitted.current = filters;             // treat as emitted, don't echo
      return;
    }
    if (lastEmitted.current === filters) return; // already emitted this exact set
    emit(filters);
    // ``emit`` only touches refs, so it is intentionally not a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, applied]);

  function handleApply() {
    setApplied(true);
    emit(filters);
  }

  function handleReset() {
    setExpiration(DEFAULTS.expiration);
    setStrikeMin(DEFAULTS.strikeMin);
    setStrikeMax(DEFAULTS.strikeMax);
    setOptionType(DEFAULTS.optionType);
    setSerieType(DEFAULTS.serieType);
    setFreq(DEFAULTS.freq);
    setApplied(false);         // re-gate: no fetch until Apply again
    // Reset overrides any restored state, including one still awaiting its
    // seeding effect run: after Reset the panel speaks for itself again.
    seeded.current = false;
    // Also clear ``lastEmitted``: the next Apply may well re-emit the DEFAULTS
    // filter set, and if that is still the last-emitted identity the auto-apply
    // effect would treat it as "already emitted". (``handleApply`` emits
    // unconditionally, so this is belt-and-braces, not the primary path.)
    lastEmitted.current = null;
    // Tell the parent, or the results it is showing stay stale — see the note on
    // ``onReset`` above.
    onResetRef.current?.();
  }

  if (loading) {
    return <div className={baseStyles.status}>Loading filters…</div>;
  }
  if (error) {
    return (
      <div className={baseStyles.error}>
        Failed to load filters: {error.message || String(error)}
      </div>
    );
  }

  return (
    <div className={styles.filterPanel}>
      <div className={styles.filterHeader}>
        Filters
        {/* "of N series" — NOT "N series". This is the object's UNFILTERED
            total, and it renders directly beside the result list's filtered
            "N series (from-to)". Without the "of", two unequal counts sit side
            by side (measured: 200 672 next to 1) both looking like the answer
            to "how many series matched?". The preposition is what makes this
            one read as the population being filtered. */}
        {facets?.totals ? (
          <span className={baseStyles.meta}>
            {` · of ${facets.totals.series.toLocaleString()} series`}
          </span>
        ) : null}
      </div>

      {hasContracts && (
        <label className={styles.filterField} htmlFor={`${uid}-exp`}>
          Expiration
          <select
            id={`${uid}-exp`}
            className={baseStyles.select}
            value={expiration}
            onChange={(e) => setExpiration(e.target.value)}
          >
            <option value="">Any</option>
            {facets.expirations.map((e) => (
              <option key={e.expiration} value={e.expiration}>
                {`${e.expiration} · ${e.contracts.toLocaleString()}`}
              </option>
            ))}
          </select>
        </label>
      )}

      {hasStrikes && (
        <>
          <label className={styles.filterField} htmlFor={`${uid}-kmin`}>
            Strike min
            <input
              id={`${uid}-kmin`}
              className={baseStyles.select}
              type="number"
              value={strikeMin}
              placeholder={String(facets.strike_min)}
              onChange={(e) => setStrikeMin(e.target.value)}
            />
          </label>
          <label className={styles.filterField} htmlFor={`${uid}-kmax`}>
            Strike max
            <input
              id={`${uid}-kmax`}
              className={baseStyles.select}
              type="number"
              value={strikeMax}
              placeholder={String(facets.strike_max)}
              onChange={(e) => setStrikeMax(e.target.value)}
            />
          </label>
        </>
      )}

      {hasOptionTypes && (
        <label className={styles.filterField} htmlFor={`${uid}-otype`}>
          Option type
          <select
            id={`${uid}-otype`}
            className={baseStyles.select}
            value={optionType}
            onChange={(e) => setOptionType(e.target.value)}
          >
            <option value="both">Both</option>
            {facets.option_types.map((t) => (
              <option key={t} value={t}>{t === 'call' ? 'Call' : 'Put'}</option>
            ))}
          </select>
        </label>
      )}

      <label className={styles.filterField} htmlFor={`${uid}-stype`}>
        Series type
        <select
          id={`${uid}-stype`}
          className={baseStyles.select}
          value={serieType}
          onChange={(e) => setSerieType(e.target.value)}
        >
          <option value="any">Any</option>
          {serieTypeValues.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </label>

      {/* freq is a first-class user control: it is how the user chooses
          between the minute quotes and the daily marks. */}
      <label className={styles.filterField} htmlFor={`${uid}-freq`}>
        Frequency
        <select
          id={`${uid}-freq`}
          className={baseStyles.select}
          value={freq}
          onChange={(e) => setFreq(e.target.value)}
        >
          <option value="any">Any</option>
          {freqValues.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
      </label>

      <div className={styles.filterActions}>
        <button type="button" className={styles.filterButton} onClick={handleApply}>
          Apply
        </button>
        <button type="button" className={styles.filterButton} onClick={handleReset}>
          Reset
        </button>
      </div>
    </div>
  );
}

export default SeriesFilterPanel;
