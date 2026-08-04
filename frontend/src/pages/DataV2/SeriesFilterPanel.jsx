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
 */
function SeriesFilterPanel({ objectId, onApply }) {
  const { data: facets, loading, error } = useObjectFacetsV2(objectId);
  const uid = useId();

  const [expiration, setExpiration] = useState(DEFAULTS.expiration);
  const [strikeMin, setStrikeMin] = useState(DEFAULTS.strikeMin);
  const [strikeMax, setStrikeMax] = useState(DEFAULTS.strikeMax);
  const [optionType, setOptionType] = useState(DEFAULTS.optionType);
  const [serieType, setSerieType] = useState(DEFAULTS.serieType);
  const [freq, setFreq] = useState(DEFAULTS.freq);

  // False until the first explicit Apply; gates auto-application. Reset puts it
  // back to false, so the gate is re-armed rather than being a one-time thing.
  const [applied, setApplied] = useState(false);

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
   */
  const prevObjectId = useRef(objectId);
  if (prevObjectId.current !== objectId) {
    prevObjectId.current = objectId;
    setExpiration(DEFAULTS.expiration);
    setStrikeMin(DEFAULTS.strikeMin);
    setStrikeMax(DEFAULTS.strikeMax);
    setOptionType(DEFAULTS.optionType);
    setSerieType(DEFAULTS.serieType);
    setFreq(DEFAULTS.freq);
    setApplied(false);
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
        {facets?.totals ? (
          <span className={baseStyles.meta}>
            {` · ${facets.totals.series.toLocaleString()} series`}
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
