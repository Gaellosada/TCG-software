import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

/**
 * Row label. ``contract_code`` is null for an object-level serie (index and
 * rate objects have no contracts — every contract field comes back null), so a
 * label that assumes it exists renders "undefined" for exactly those rows.
 *
 * For those rows the object's own symbol is the meaningful name — that is what
 * the flat list this component replaced showed — so the caller may supply it as
 * ``objectSymbol``. Without it the row degrades to ``serie {id}``, which is a
 * bare database id in the UI for exactly the index and rate objects.
 *
 * The symbol is used ONLY when ``contract_id`` is null. A *contract* row whose
 * ``contract_code`` happens to be missing is a data defect, and labelling it
 * with the object symbol would hide that behind a plausible-looking name.
 */
function serieLabel(s, objectSymbol) {
  if (s.contract_code) return s.contract_code;
  if (s.contract_id == null && objectSymbol) return objectSymbol;
  return `serie ${s.serie_id}`;
}

/** Secondary line: whichever of type / freq the row actually has. */
function serieMeta(s) {
  return [s.type, s.freq].filter(Boolean).join(' · ');
}

/**
 * Paginated list of filtered series.
 *
 * Presentational: the caller owns the query and the filter state. This
 * component receives one page and reports clicks — it never fetches.
 *
 * No virtualisation, deliberately: a page is `limit` rows (50 by default), not
 * the 200 672 buttons this page used to mount at once. Windowing would be the
 * wrong lesson from that bug; bounding the page is the right one.
 *
 * An empty page is a normal outcome of a narrow filter, so it renders an empty
 * state. A failed fetch renders as an error (``role="alert"``) with no list and
 * no pager. The two are structurally distinct, not two shades of the same
 * message.
 *
 * Paging moves by `limit` and cannot walk off either end: Prev is disabled on
 * the first page, Next on the last.
 */
function SeriesResultList({
  items = [],
  total = 0,
  skip = 0,
  limit = 50,
  loading = false,
  error = null,
  selectedSerieId = null,
  objectSymbol = null,
  onSelect,
  onPageChange,
}) {
  // The displayed range describes the rows actually rendered, so it stays
  // honest if the caller hands over a short page.
  const shown = items.length;
  const from = shown === 0 ? 0 : skip + 1;
  const to = Math.min(skip + shown, total);

  // Bounds are computed from `limit`, because paging moves by `limit`.
  const hasPrev = skip > 0;
  const hasNext = skip + limit < total;

  if (error) {
    return (
      <div className={styles.resultList}>
        <div className={baseStyles.error} role="alert">
          Failed to load series: {error.message || String(error)}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.resultList}>
      <div className={styles.seriesListHeader} role="status">
        {loading
          ? 'Loading series…'
          : `${total.toLocaleString()} series${shown ? ` (${from}-${to})` : ''}`}
      </div>

      <div className={styles.resultItems}>
        {/* While loading, "nothing matched" is a claim we cannot make yet. */}
        {!loading && shown === 0 ? (
          <div className={baseStyles.status} style={{ padding: 16 }}>
            No series match this filter.
          </div>
        ) : (
          items.map((s) => {
            const label = serieLabel(s, objectSymbol);
            const meta = serieMeta(s);
            const selected = s.serie_id === selectedSerieId;
            return (
              <button
                key={s.serie_id}
                type="button"
                className={`${styles.seriesItem}${selected ? ` ${styles.seriesItemActive}` : ''}`}
                aria-current={selected ? 'true' : undefined}
                onClick={() => onSelect?.(s.serie_id)}
                title={meta ? `${label} — ${meta}` : label}
              >
                <span className={styles.seriesItemPrimary}>{label}</span>
                {meta ? <span className={styles.seriesItemMeta}>{meta}</span> : null}
              </button>
            );
          })
        )}
      </div>

      <nav className={styles.pager} aria-label="Series pagination">
        <button
          type="button"
          className={styles.pagerButton}
          disabled={!hasPrev}
          onClick={() => onPageChange?.(Math.max(0, skip - limit))}
        >
          ‹ Prev
        </button>
        <button
          type="button"
          className={styles.pagerButton}
          disabled={!hasNext}
          onClick={() => onPageChange?.(skip + limit)}
        >
          Next ›
        </button>
      </nav>
    </div>
  );
}

export default SeriesResultList;
