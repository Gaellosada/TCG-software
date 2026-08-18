import { useState, useMemo, useEffect, useRef } from 'react';
import { useSearchParams, useLocation } from 'react-router-dom';
import { useObjectSeriesV2 } from '../../hooks/marketQueries';
import SeriesChartV2 from './SeriesChartV2';
import SeriesFilterPanel, { parseStrikeBound } from './SeriesFilterPanel';
import SeriesResultList from './SeriesResultList';
import ContinuousFuturesChartV2 from './ContinuousFuturesChartV2';
import ContinuousOptionsChartV2 from './ContinuousOptionsChartV2';
import pageStyles from '../Data/DataPage.module.css';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/** Rows per page. The backend defaults to 50 and caps at 500. */
const PAGE_LIMIT = 50;

/**
 * Query-string marker for "a filter is applied, and it narrows nothing".
 *
 * Every other dimension is written to the URL only when it differs from its
 * default, which keeps a shared link legible — but it leaves one state
 * unwritable: Apply pressed with every control at its default. That is a real,
 * bounded query (``limit`` rows of the object's series) and it must survive a
 * reload and a page change, yet an empty query string already means "not
 * applied yet". This marker is the difference between the two.
 */
const APPLIED_MARKER = 'applied';

/**
 * The values each enum dimension may take in the URL.
 *
 * Source of truth: ``_SERIE_TYPE_VALUES`` / ``_FREQ_VALUES`` /
 * ``_OPTION_TYPE_VALUES`` in ``tcg/core/api/data_v2.py`` (the backend rejects
 * anything else with a validation error), mirrored in ``getObjectSeriesV2``'s
 * docstring.
 *
 * A URL is user-editable, so these arrive unvalidated exactly as strike bounds
 * do. An unknown value must be dropped HERE, at the boundary, because the panel
 * cannot display it: a ``<select>`` whose value is absent from its options gets
 * the first option selected by HTML's own reset algorithm, so the panel would
 * read "Any" while the request carried the junk — the panel lying about the
 * applied filter, plus an error banner from the backend instead of a page.
 */
export const URL_ENUMS = Object.freeze({
  option_type: Object.freeze(['call', 'put', 'both']),
  serie_type: Object.freeze(['bar', 'value', 'greeks', 'bbba', 'any']),
  freq: Object.freeze(['1m', 'daily', 'any']),
});

/** One enum dimension from the URL, or '' when absent or not a legal value. */
function urlEnum(params, key) {
  const raw = params.get(key);
  return raw && URL_ENUMS[key].includes(raw) ? raw : '';
}

/**
 * One expiration value from the URL, or '' when absent or not a real ISO date.
 *
 * Unlike the strike bounds (see ``parseStrikeBound``) and the enums (see
 * ``urlEnum``), an expiration is passed to the backend as-is, where
 * ``parse_iso_range`` runs ``date.fromisoformat`` on it: a junk value like
 * ``?expiration_min=notadate`` — or a well-shaped but impossible calendar date
 * like ``2026-13-40`` — raises ValueError → HTTP 400 and takes down the whole
 * tab, while every other hostile dimension degrades to a working page. So a bad
 * value is dropped HERE, at the boundary, exactly as those two are.
 *
 * The check mirrors ``date.fromisoformat``'s own contract: a strict
 * ``YYYY-MM-DD`` shape AND a calendar-valid date. A plain ``Date.parse`` is NOT
 * enough — V8 ROLLS OVER an out-of-range day (``2026-02-30`` becomes Mar 2, and
 * 2026 is not a leap year so Feb has 28 days) and returns a valid time rather
 * than NaN, so it would keep a date the backend rejects and silently shift it.
 * Round-tripping through ``toISOString`` catches that: a rolled-over date no
 * longer equals the input. ``T00:00:00Z`` pins the parse to UTC so it is not
 * shifted by the host time zone.
 */
function urlIsoDate(raw) {
  if (!raw) return '';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return '';
  const d = new Date(`${raw}T00:00:00Z`);
  return (!Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === raw) ? raw : '';
}

/**
 * The query string with the numeric bound VALUES blanked out.
 *
 * Two writes with the same shape differ only in a strike bound's digits, which
 * is what a keystroke in a strike input produces — see ``writeParams``, which
 * replaces the history entry in exactly that case. Adding or removing a bound
 * changes the shape (the key appears or disappears), so it still pushes.
 */
function boundShape(params) {
  const shape = new URLSearchParams(params);
  for (const key of ['strike_min', 'strike_max']) {
    if (shape.has(key)) shape.set(key, '*');
  }
  return shape.toString();
}

/**
 * Heading for the charted series, or ``null`` to mean "the object symbol alone
 * says it" (the caller renders `SYMBOL` rather than `SYMBOL · SYMBOL`).
 *
 * ``contract_code`` is null for an object-level serie — index and rate objects
 * have no contracts, so every contract field comes back null — and there the
 * object symbol is the meaningful name, which is what the flat list this page
 * replaced showed. Only a serie we know by nothing but its id degrades to that
 * id; ``selectedSerie`` works hard (see ``rememberedRow``) to make that rare.
 */
function serieTitle(serie) {
  if (serie.contract_code) return serie.contract_code;
  // ``resolved`` distinguishes "this serie genuinely has no contract" from "we
  // have no metadata for it". Both have ``contract_id == null``, but only the
  // first may be named after the object.
  if (serie.resolved && serie.contract_id == null) return null; // object-level
  return `serie ${serie.serie_id}`;
}

/**
 * Object detail / drill-down. Two tabs:
 *   - "Series": filter this object's series (persistent panel, driven by
 *     ``/facets``) → one bounded page of results → chart the picked series.
 *   - "Continuous" (future / option only): the continuous builder for that kind.
 *
 * Why there is no flat series list any more: an option root has ~200 000
 * series, and ``GET /objects/{id}`` returned every one of them (38 MB, ~38 s)
 * which this component then mounted as one button per series with no
 * virtualisation — the tab froze on "Loading object…". The replacement never
 * asks for an unbounded set: nothing is fetched until the user applies a
 * filter, and each page is ``PAGE_LIMIT`` rows.
 *
 * That is also why ``useObjectDetailV2`` is NOT mounted here (it was, until
 * this change). Everything the header needs — symbol, kind, name, cycle —
 * already arrives on ``object`` from the browser list, and contract metadata now
 * comes joined onto each series row, so the fat endpoint had no remaining
 * reader. Keeping it "just for loading/error" would have re-introduced the
 * whole 38 MB / ~38 s stall this task exists to remove, since its ``loading``
 * gate blocked the entire tab. (``/objects/{id}`` has since been slimmed to
 * metadata only — a few hundred bytes — so it is no longer expensive; it is
 * still not mounted here because there is nothing left for it to supply.)
 *
 * The filter and the page offset are held in the query string
 * (``?expiration_min=…&option_type=put&serie_type=bbba&skip=50``) rather than in
 * component state, so the back button works, a filter state is shareable by
 * link, and a reload loses nothing. The SELECTED OBJECT is deliberately not in
 * the URL: it comes from the browser list, which the page loads wholesale in one
 * cheap call, and nothing in the spec asks for it.
 *
 * That absence is what makes switching objects subtle, and ``DataV2Page`` (see
 * ``handleSelect``) is where it is resolved: the params are cleared on a GENUINE
 * object switch — an object is already selected AND the picked id differs — and
 * only then. So a link recipient's FIRST pick keeps the shared filter (there is
 * no previous object, so nothing is being left behind, and clearing would make
 * every shared link arrive empty), while every later pick clears it, because the
 * panel is rebuilt from the new object's ``/facets`` and cannot display a
 * dimension that object lacks: a ``<select>`` whose value is absent from its
 * options falls back to "Any" by HTML's own reset algorithm, so the panel would
 * report "no filter" while one was still being applied. BOTH directions are
 * pinned, by two separate tests in ``DataV2Page.test.jsx`` — "does not carry an
 * applied filter over to another object" and "keeps a shared link's filter
 * across the recipient's first object pick". Neither is redundant with the
 * other: dropping either one un-guards one half of the rule.
 *
 * The clear is a history PUSH and the object is deliberately not in the URL, so
 * a single back press would restore the previous query string while ``selected``
 * stays on the NEW object. Left unguarded that re-applies the old filter to it
 * (and re-seeds the panel from it, ``urlEpoch`` having remounted the panel) —
 * the panel-lies condition again, reached with one keypress. It is closed by
 * BINDING each applied filter to the object that produced it: the object id is
 * stamped into the history entry's ``state`` (not the query string — the object
 * stays out of shareable links), and the ``filters`` memo IGNORES a restored
 * filter whose stamp names a different object, gating the new object cleanly.
 * Two write sites cover every applied-filter entry: ``writeParams`` here stamps
 * everything THIS component writes, and ``DataV2Page.handleSelect`` stamps the
 * entry a shared link arrived on at first pick (that entry predates any object,
 * so it would otherwise be a stamp-less filter that Back re-applies to the wrong
 * object). A stamp of ``undefined`` (e.g. after a full reload) is read as "for
 * this object", so a link still applies on first pick. Pinned by
 * "does not re-apply the previous object's filter after a Back press" and "does
 * not inherit a shared link's filter after switching object then Back" in
 * ``DataV2Page.test.jsx``.
 */
function ObjectDetail({ object }) {
  const [tab, setTab] = useState('series');
  const [selectedSerieId, setSelectedSerieId] = useState(null);

  /*
   * The row the user actually clicked, kept beside its id.
   *
   * The id alone is enough to FETCH a series but not to NAME one, and the row
   * leaves the current page as soon as the filter narrows past it. Without this,
   * a chart that survives a filter change survives anonymously: its heading
   * degrades from "OPT_SP_500_EW2 · EW2U6 P5500.20260911" to
   * "OPT_SP_500_EW2 · serie 1653693" and any CSV downloaded in that state is
   * named by database id. The metadata was on screen when the user picked the
   * row; there is no reason to throw it away.
   */
  const [rememberedRow, setRememberedRow] = useState(null);

  /*
   * The filter state and the page live in the URL, not in component state.
   * That is what makes the back button work, a filter state shareable by link,
   * and a reload lossless.
   *
   * A URL carrying any filter key is an APPLIED state: the recipient of a link
   * must not have to press Apply to see what the link describes. That is not a
   * hole in the gate the panel enforces — the request the gate exists to
   * prevent is the UNBOUNDED one, and a URL that names filters produces a
   * bounded one. No filter key → ``null`` → ``useObjectSeriesV2`` is disabled
   * and nothing is fetched. Do not "helpfully" default this to {}.
   *
   * Wire vocabulary (snake_case) in, camelCase out — the client allowlists the
   * camelCase keys and throws a TypeError on anything else, so this mapping is
   * the whole translation layer. ``expiration`` is accepted as a synonym for
   * both bounds because the spec's shareable URL is written that way and the
   * panel only ever offers one expiration (a one-day window).
   */
  const [searchParams, setSearchParams] = useSearchParams();

  /*
   * Which object the query string in the URL was produced for.
   *
   * ``writeParams`` stamps the current object id into the history entry's
   * ``state`` on every write. The selected object is NOT in the URL, so a Back
   * press can restore a filter this component wrote for a DIFFERENT object while
   * ``selected`` (held in ``DataV2Page``) stays put — see the header note. This
   * stamp is what lets ``filters`` tell the two apart. ``undefined`` means "no
   * stamp" — an unwritten shared link, or a full reload — and is treated as "for
   * this object", which keeps a shared link's filter working on first pick.
   */
  const location = useLocation();
  const filterObjectId = location.state?.filterObjectId;

  const filters = useMemo(() => {
    // A restored filter that was produced for another object must never be read
    // against this one (the panel cannot always display it, so it would lie).
    if (filterObjectId != null && filterObjectId !== object.object_id) return null;

    const expiration = urlIsoDate(searchParams.get('expiration'));
    const expirationMin = urlIsoDate(searchParams.get('expiration_min')) || expiration || '';
    const expirationMax = urlIsoDate(searchParams.get('expiration_max')) || expiration || '';
    // A URL is user-editable, so a strike bound can arrive as anything.
    // ``parseStrikeBound`` drops a non-finite one: ``?strike_min=abc`` would
    // otherwise reach ``getObjectSeriesV2``, which throws a TypeError on a
    // non-finite bound (rightly — the backend accepts NaN with HTTP 200 and
    // silently matches nothing). A bad URL must degrade, not crash the tab.
    const strikeMin = parseStrikeBound(searchParams.get('strike_min'));
    const strikeMax = parseStrikeBound(searchParams.get('strike_max'));
    // Same treatment for the enums, and for the same reason — see ``urlEnum``.
    const optionType = urlEnum(searchParams, 'option_type');
    const serieType = urlEnum(searchParams, 'serie_type');
    const freq = urlEnum(searchParams, 'freq');

    /*
     * Is a filter applied at all? Any *surviving* filter value says yes, and so
     * does the bare ``applied`` marker, which is how "applied, but narrowing
     * nothing" is written down: pressing Apply with every control at its
     * default is a legitimate (and bounded, ``limit``-capped) query, and an
     * empty query string cannot distinguish it from "not applied yet". A
     * garbage-only URL (``?strike_min=abc``) survives nothing and so lands on
     * the gate rather than on an accidental default query.
     */
    const present = expirationMin !== '' || expirationMax !== ''
      || strikeMin !== undefined || strikeMax !== undefined
      || optionType !== '' || serieType !== '' || freq !== ''
      || searchParams.has(APPLIED_MARKER);
    if (!present) return null;

    return {
      expirationMin: expirationMin || undefined,
      expirationMax: expirationMax || undefined,
      strikeMin,
      strikeMax,
      // Sentinels are sent explicitly and match the backend defaults.
      optionType: optionType || 'both',
      serieType: serieType || 'any',
      freq: freq || 'any',
    };
  }, [searchParams, filterObjectId, object.object_id]);

  // Also user-editable: a negative or non-numeric offset would render a "0-0 of
  // N" range (and NaN is silently dropped by the client, so the page would not
  // even match the URL that produced it).
  const skip = useMemo(() => {
    const raw = Number(searchParams.get('skip'));
    return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  }, [searchParams]);

  /*
   * The panel seeds its fields from ``initialFilters`` once per mount, so on its
   * own it would not follow the URL changing underneath it — after a back
   * button press the list would show the previous filter's results beside a
   * panel still displaying the newer one. Remounting on every filter change
   * instead would interrupt typing in the strike inputs, since the panel causes
   * those changes itself.
   *
   * So: remount the panel only for a URL change this component did NOT write.
   * ``lastWritten`` is seeded from the first render's query string, so mounting
   * is not mistaken for an external change.
   */
  const lastWritten = useRef(searchParams.toString());
  const [urlEpoch, setUrlEpoch] = useState(0);
  /*
   * Shape of the last query string THIS component wrote, with the numeric bound
   * values blanked out — see ``writeParams``. ``null`` means "the URL is not
   * ours any more" (an external change intervened), which forces the next write
   * to push rather than replace the state the user just navigated to.
   */
  const lastWrittenShape = useRef(null);
  useEffect(() => {
    const current = searchParams.toString();
    if (current === lastWritten.current) return;
    lastWritten.current = current;
    lastWrittenShape.current = null;
    setUrlEpoch((n) => n + 1);
  }, [searchParams]);

  // ``skip``/``limit`` ride along with the filters: they are part of the
  // camelCase key set ``getObjectSeriesV2`` allowlists (anything else is a
  // synchronous TypeError there), and the hook's third argument is for query
  // options only.
  const query = useMemo(
    () => (filters ? { ...filters, skip, limit: PAGE_LIMIT } : null),
    [filters, skip],
  );
  const {
    data: page,
    loading: pageLoading,
    error: pageError,
  } = useObjectSeriesV2(object.object_id, query);

  /**
   * Write one filter state + page offset to the query string, which is the
   * single source of truth for both. Only the dimensions that actually narrow
   * anything are written, so a shared link reads as what the user chose rather
   * than as a dump of every default — hence ``APPLIED_MARKER`` for the one case
   * that would otherwise be indistinguishable from "nothing applied".
   */
  function writeParams(next, nextSkip) {
    const p = new URLSearchParams();
    if (next) {
      if (next.expirationMin) p.set('expiration_min', next.expirationMin);
      if (next.expirationMax) p.set('expiration_max', next.expirationMax);
      if (next.strikeMin != null) p.set('strike_min', String(next.strikeMin));
      if (next.strikeMax != null) p.set('strike_max', String(next.strikeMax));
      if (next.optionType && next.optionType !== 'both') p.set('option_type', next.optionType);
      if (next.serieType && next.serieType !== 'any') p.set('serie_type', next.serieType);
      if (next.freq && next.freq !== 'any') p.set('freq', next.freq);
      // Applied, but narrowing nothing. Written on EVERY such write, not just
      // on Apply: without it, paging an unnarrowed filter would produce
      // ``?skip=50`` alone, which reads back as "no filter" and would drop the
      // user from page 2 straight back to the pre-Apply prompt.
      if ([...p.keys()].length === 0) p.set(APPLIED_MARKER, '1');
    }
    if (nextSkip) p.set('skip', String(nextSkip));

    /*
     * Push or replace?
     *
     * A push, normally: each applied filter is a place the back button must be
     * able to return to. But every keystroke in a strike input is a filter
     * change, so typing "6260" pushed FOUR entries — four back presses to undo
     * one bound, and no easy way to press back out of the page at all.
     *
     * So a write that differs from our own previous write only in the VALUE of
     * a numeric bound replaces it: one typing episode costs one entry. Adding
     * or clearing a bound changes the shape, so it still pushes and stays
     * undoable, and so does every enum, expiration and page change.
     * ``lastWrittenShape`` is null when the previous URL was not ours (a
     * back/forward or an edited address bar), which forces a push so we never
     * overwrite a state the user has just navigated to.
     *
     * (Debouncing the panel's number inputs would also collapse the entries and
     * additionally save the fetch-per-keystroke, which predates this change.
     * That belongs in the panel and is left as a follow-up; this keeps the URL
     * honest without touching the panel's emit timing.)
     */
    const shape = boundShape(p);
    const replace = lastWrittenShape.current === shape;
    lastWritten.current = p.toString();
    lastWrittenShape.current = shape;
    // Stamp the object this filter was produced for into the history entry's
    // state, so a Back press can never read it against a different object (see
    // ``filterObjectId``). The stamp rides in ``state``, not the query string —
    // the object stays out of shareable links, as the design intends.
    setSearchParams(p, { replace, state: { filterObjectId: object.object_id } });
  }

  // A new filter starts from the first page; changing pages must not reset it.
  const handleApply = (next) => writeParams(next, 0);
  const handlePageChange = (nextSkip) => writeParams(filters, nextSkip);

  /*
   * Reset means "start this object over", so it un-applies. Without clearing
   * the filter the panel would show blank fields beside the page produced by
   * the filters just cleared, with no refetch to correct it and nothing on
   * screen admitting the list is stale — Reset would look like a no-op.
   *
   * The query string has to be cleared with it, not just the derived state:
   * with the filter left in the URL the very next reload (or a link copied
   * afterwards) would resurrect exactly the filter the user just cleared.
   *
   * Unlike a filter CHANGE (which must never cost the user their chart), Reset
   * is an explicit "clear everything", and leaving a chart mounted next to a
   * "set a filter" prompt would be incoherent. So the selection goes too.
   */
  function handleReset() {
    writeParams(null, 0);
    setSelectedSerieId(null);
    setRememberedRow(null);
  }

  // Capture the row while it is still on screen — see ``rememberedRow``.
  function handleSelect(serieId) {
    setSelectedSerieId(serieId);
    setRememberedRow(
      (page?.items || []).find((s) => s.serie_id === serieId) || null,
    );
  }

  /*
   * Resolve the charted series: from the current page if it is still there,
   * otherwise from the remembered row, otherwise from the bare id. A chart
   * SURVIVES a filter change that excludes it — the serie_id stays valid and
   * chartable, and erasing a user's chart because they moved a filter bound
   * would make the tool tiresome — and thanks to ``rememberedRow`` it survives
   * with its name and type intact, not just its id.
   *
   * ``outsideFilter`` drives the "no longer in this list" notice; ``resolved``
   * records whether we have the row's metadata at all (see ``serieTitle``).
   */
  const selectedSerie = useMemo(() => {
    if (selectedSerieId == null) return null;
    const found = (page?.items || []).find((s) => s.serie_id === selectedSerieId);
    if (found) return { ...found, outsideFilter: false, resolved: true };
    const remembered = rememberedRow?.serie_id === selectedSerieId ? rememberedRow : null;
    return {
      serie_id: selectedSerieId,
      type: null,
      ...remembered,
      outsideFilter: true,
      resolved: remembered != null,
    };
  }, [page, selectedSerieId, rememberedRow]);

  const hasContinuous = object.kind === 'future' || object.kind === 'option';

  const TABS = useMemo(() => {
    const t = [{ key: 'series', label: 'Series' }];
    if (object.kind === 'future') t.push({ key: 'continuous', label: 'Continuous (Futures)' });
    if (object.kind === 'option') t.push({ key: 'continuous', label: 'Continuous (Options)' });
    return t;
  }, [object.kind]);

  const title = selectedSerie ? serieTitle(selectedSerie) : null;

  return (
    <div className={pageStyles.optionsWrapper}>
      {/* Header */}
      <div className={baseStyles.header}>
        <h2 className={baseStyles.title}>{object.symbol}</h2>
        <span className={styles.kindBadge}>{object.kind}</span>
        <span className={baseStyles.meta}>
          {object.name}
          {object.cycle ? ` · cycle ${object.cycle}` : ''}
        </span>
      </div>

      {/* Tab strip */}
      <div className={pageStyles.optionsTabs} role="tablist">
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`${pageStyles.optionsTab}${tab === key ? ` ${pageStyles.optionsTabActive}` : ''}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab body */}
      <div className={pageStyles.optionsTabBody}>
        {tab === 'series' && (
          <div className={styles.seriesLayout}>
            {/* Stays mounted: changing one dimension must not cost re-entering
                the others. The key changes with the object and with an
                EXTERNAL url change (back/forward, an edited address bar) —
                never with a filter change the panel itself caused, which would
                remount it mid-typing. See ``urlEpoch``. */}
            <SeriesFilterPanel
              key={`${object.object_id}:${urlEpoch}`}
              objectId={object.object_id}
              initialFilters={filters}
              onApply={handleApply}
              onReset={handleReset}
            />
            {filters == null ? (
              <div className={styles.seriesEmpty}>
                Set a filter and press Apply to list this object&apos;s series.
              </div>
            ) : (
              <>
                <SeriesResultList
                  items={page?.items || []}
                  total={page?.total || 0}
                  skip={page?.skip ?? skip}
                  limit={page?.limit ?? PAGE_LIMIT}
                  loading={pageLoading}
                  error={pageError}
                  selectedSerieId={selectedSerieId}
                  // Names an object-level serie after its object (index and
                  // rate objects have no contracts), instead of "serie {id}".
                  objectSymbol={object.symbol}
                  onSelect={handleSelect}
                  onPageChange={handlePageChange}
                />
                <div className={styles.seriesChartCol}>
                  {selectedSerie ? (
                    <>
                      {selectedSerie.outsideFilter && (
                        <div className={baseStyles.meta}>
                          This series is outside the current filter.
                        </div>
                      )}
                      <SeriesChartV2
                        key={selectedSerie.serie_id}
                        serieId={selectedSerie.serie_id}
                        serieType={selectedSerie.type}
                        label={title ? `${object.symbol} · ${title}` : object.symbol}
                        downloadFilename={`${object.symbol}-${selectedSerie.serie_id}`}
                      />
                    </>
                  ) : (
                    <div className={styles.seriesEmpty}>
                      Pick a series to chart it.
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {tab === 'continuous' && hasContinuous && object.kind === 'future' && (
          <ContinuousFuturesChartV2 objectId={object.object_id} symbol={object.symbol} />
        )}

        {tab === 'continuous' && hasContinuous && object.kind === 'option' && (
          <ContinuousOptionsChartV2 objectId={object.object_id} symbol={object.symbol} />
        )}
      </div>
    </div>
  );
}

export default ObjectDetail;
