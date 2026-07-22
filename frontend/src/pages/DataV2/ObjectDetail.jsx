import { useState, useMemo } from 'react';
import { useObjectDetailV2 } from '../../hooks/marketQueries';
import SeriesChartV2 from './SeriesChartV2';
import ContinuousFuturesChartV2 from './ContinuousFuturesChartV2';
import ContinuousOptionsChartV2 from './ContinuousOptionsChartV2';
import pageStyles from '../Data/DataPage.module.css';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/**
 * Object detail / drill-down. Fetches the object's contracts + series and
 * offers:
 *   - a "Series" tab: pick an individual series → chart it (type-dispatched)
 *     via the shared Chart component.
 *   - a "Continuous" tab (future / option only): the continuous builder for
 *     that kind.
 */
function ObjectDetail({ object }) {
  const { data, loading, error } = useObjectDetailV2(object.object_id);
  const [tab, setTab] = useState('series');
  const [selectedSerieId, setSelectedSerieId] = useState(null);

  // Map contract_id → contract for series labels.
  const contractsById = useMemo(() => {
    const m = new Map();
    for (const c of data?.contracts || []) m.set(c.contract_id, c);
    return m;
  }, [data]);

  // Build a display list of series. Object-level series (contract_id == null,
  // e.g. rate/index) first, then per-contract series sorted by contract code.
  const seriesList = useMemo(() => {
    const rows = (data?.series || []).map((s) => {
      const contract = s.contract_id != null ? contractsById.get(s.contract_id) : null;
      const primary = contract
        ? (contract.contract_code || `contract ${s.contract_id}`)
        : (object.symbol || `serie ${s.serie_id}`);
      const meta = [s.type, s.freq].filter(Boolean).join(' · ');
      return { ...s, primary, meta, _isObjectLevel: contract == null };
    });
    rows.sort((a, b) => {
      if (a._isObjectLevel !== b._isObjectLevel) return a._isObjectLevel ? -1 : 1;
      return String(a.primary).localeCompare(String(b.primary));
    });
    return rows;
  }, [data, contractsById, object.symbol]);

  const selectedSerie = useMemo(
    () => seriesList.find((s) => s.serie_id === selectedSerieId) || null,
    [seriesList, selectedSerieId],
  );

  const hasContinuous = object.kind === 'future' || object.kind === 'option';

  const TABS = useMemo(() => {
    const t = [{ key: 'series', label: 'Series' }];
    if (object.kind === 'future') t.push({ key: 'continuous', label: 'Continuous (Futures)' });
    if (object.kind === 'option') t.push({ key: 'continuous', label: 'Continuous (Options)' });
    return t;
  }, [object.kind]);

  if (loading) {
    return (
      <div className={baseStyles.container}>
        <div className={baseStyles.status}>Loading object…</div>
      </div>
    );
  }
  if (error) {
    return (
      <div className={baseStyles.container}>
        <div className={baseStyles.error}>Failed to load object: {error.message || String(error)}</div>
      </div>
    );
  }

  return (
    <div className={pageStyles.optionsWrapper}>
      {/* Header */}
      <div className={baseStyles.header}>
        <h2 className={baseStyles.title}>{object.symbol}</h2>
        <span className={styles.kindBadge}>{object.kind}</span>
        <span className={baseStyles.meta}>
          {object.name}
          {data?.contracts?.length ? ` · ${data.contracts.length.toLocaleString()} contracts` : ''}
          {data?.series?.length ? ` · ${data.series.length.toLocaleString()} series` : ''}
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
            <div className={styles.seriesList}>
              <div className={styles.seriesListHeader}>
                Series ({seriesList.length.toLocaleString()})
              </div>
              {seriesList.length === 0 ? (
                <div className={baseStyles.status} style={{ padding: 16 }}>No series</div>
              ) : (
                seriesList.map((s) => (
                  <button
                    key={s.serie_id}
                    className={`${styles.seriesItem} ${
                      s.serie_id === selectedSerieId ? styles.seriesItemActive : ''
                    }`}
                    onClick={() => setSelectedSerieId(s.serie_id)}
                    title={`${s.primary} — ${s.meta}${s.source ? ` (${s.source})` : ''}`}
                  >
                    <span className={styles.seriesItemPrimary}>{s.primary}</span>
                    <span className={styles.seriesItemMeta}>{s.meta}</span>
                  </button>
                ))
              )}
            </div>
            <div className={styles.seriesChartCol}>
              {selectedSerie ? (
                <SeriesChartV2
                  key={selectedSerie.serie_id}
                  serieId={selectedSerie.serie_id}
                  serieType={selectedSerie.type}
                  label={`${object.symbol} · ${selectedSerie.primary}`}
                  downloadFilename={`${object.symbol}-${selectedSerie.primary}-${selectedSerie.type}`}
                />
              ) : (
                <div className={styles.seriesEmpty}>
                  Pick a series on the left to chart it.
                </div>
              )}
            </div>
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
