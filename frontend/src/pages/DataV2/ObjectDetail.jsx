import { useState, useMemo } from 'react';
import { useObjectSeriesV2 } from '../../hooks/marketQueries';
import SeriesChartV2 from './SeriesChartV2';
import SeriesFilterPanel from './SeriesFilterPanel';
import SeriesResultList from './SeriesResultList';
import ContinuousFuturesChartV2 from './ContinuousFuturesChartV2';
import ContinuousOptionsChartV2 from './ContinuousOptionsChartV2';
import pageStyles from '../Data/DataPage.module.css';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/** Rows per page. The backend defaults to 50 and caps at 500. */
const PAGE_LIMIT = 50;

/**
 * Heading for the charted series.
 *
 * ``contract_code`` is null for an object-level serie — index and rate objects
 * have no contracts, so every contract field comes back null — and there the
 * object symbol is the meaningful name (what the old flat list showed). A row
 * we know only by id (see ``outsideFilter``) falls back to that id.
 */
function serieTitle(serie, object) {
  if (serie.contract_code) return serie.contract_code;
  if (serie.contract_id == null && !serie.outsideFilter) return null; // object-level
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
 * gate blocked the entire tab. (``/objects/{id}`` itself is unchanged and still
 * works; a later task slims its payload.)
 */
function ObjectDetail({ object }) {
  const [tab, setTab] = useState('series');
  const [selectedSerieId, setSelectedSerieId] = useState(null);

  // null until the user applies a filter — this is what gates the first fetch.
  // ``useObjectSeriesV2`` is disabled while it is null, so no unbounded series
  // request can be issued. Do not "helpfully" default this to {}.
  const [filters, setFilters] = useState(null);
  const [skip, setSkip] = useState(0);

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

  // A new filter starts from the first page; changing pages must not reset it.
  function handleApply(next) {
    setSkip(0);
    setFilters(next);
  }

  /*
   * Resolve the charted series from the current page, falling back to the bare
   * id so a chart SURVIVES a filter change that excludes it: the serie_id
   * remains valid and chartable, and erasing a user's chart because they moved
   * a filter bound would make the tool tiresome. The fallback is flagged so the
   * UI can say why the row is no longer in the list.
   */
  const selectedSerie = useMemo(() => {
    if (selectedSerieId == null) return null;
    const found = (page?.items || []).find((s) => s.serie_id === selectedSerieId);
    if (found) return { ...found, outsideFilter: false };
    return { serie_id: selectedSerieId, type: null, outsideFilter: true };
  }, [page, selectedSerieId]);

  const hasContinuous = object.kind === 'future' || object.kind === 'option';

  const TABS = useMemo(() => {
    const t = [{ key: 'series', label: 'Series' }];
    if (object.kind === 'future') t.push({ key: 'continuous', label: 'Continuous (Futures)' });
    if (object.kind === 'option') t.push({ key: 'continuous', label: 'Continuous (Options)' });
    return t;
  }, [object.kind]);

  const title = selectedSerie ? serieTitle(selectedSerie, object) : null;

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
                the others. */}
            <SeriesFilterPanel
              objectId={object.object_id}
              onApply={handleApply}
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
                  onSelect={setSelectedSerieId}
                  onPageChange={setSkip}
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
