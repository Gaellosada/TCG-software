import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import ObjectBrowser from './ObjectBrowser';
import ObjectDetail from './ObjectDetail';
import styles from '../Data/DataPage.module.css';

/**
 * Database v2 page — a schema-native explorer over the dwh star schema
 * ``tcg_instruments_v2`` (object → contract → serie → fact_*), served by the
 * ``/api/data-v2`` router. Parallel to the v1 Data page (which stays as-is):
 * the object browser groups objects by ``kind`` instead of by collection
 * category, and drill-down is object → filtered page of series → chart. (There
 * is no contracts step: contract metadata rides along on each series row.)
 *
 * Reuses the v1 DataPage layout CSS (left browser panel + right detail panel)
 * and the shared Chart component for all rendering.
 */
function DataV2Page() {
  const [selected, setSelected] = useState(null);
  const [searchParams, setSearchParams] = useSearchParams();

  /*
   * Picking an object clears the filter in the query string — EXCEPT on the
   * first pick of the session.
   *
   * ``ObjectDetail`` holds the applied filter in the URL, and the URL outlives
   * that component (this page keys it on ``object_id``, so it remounts). Two
   * requirements collide there, and the distinction that resolves them is not
   * "did the object change" but "is this the link being opened, or the user
   * browsing on":
   *
   *  - A shared link carries no object (the object comes from the list, which
   *    this page already has in one cheap call). So the recipient MUST pick the
   *    object out of the list, and that pick must keep the link's filter — with
   *    ``selected == null`` there is no previous object, so nothing is being
   *    left behind. Clearing there would make every shared link arrive empty.
   *  - Moving from one object to another must NOT carry the filter over. The
   *    panel is rebuilt from the new object's ``/facets``, and a <select> whose
   *    value is absent from its options falls back to the first one ("Any") by
   *    HTML's own reset algorithm — so the panel would report "no type filter"
   *    while ``serie_type=bbba`` was still being applied, and an expiration or
   *    strike the new object does not have would silently match nothing. The
   *    panel would be LYING about the applied filter, with only the address bar
   *    telling the truth. It also gates the new object properly: nothing is
   *    fetched for it until the user applies something.
   */
  function handleSelect(next) {
    if (selected && next.object_id !== selected.object_id) {
      setSearchParams(new URLSearchParams());
    } else if (!selected) {
      /*
       * First pick of the session — typically a shared link being opened. The
       * query string may already carry a filter, but the router wrote that
       * history entry BEFORE any object existed, so it has no object stamp.
       * ``ObjectDetail`` stamps every filter IT writes (into the entry's
       * ``state``, not the query string) so a later Back onto a DIFFERENT object
       * can tell the filter was produced elsewhere and gate it — but a filter
       * that arrived on the link and is never re-written keeps a bare entry.
       * Without a stamp here, switching away and pressing Back re-applies the
       * link's filter to the new object (the panel-lies condition again). Stamp
       * the entry now, in place (``replace`` + unchanged query string), so EVERY
       * applied-filter entry carries the object it belongs to.
       */
      setSearchParams(searchParams, {
        replace: true,
        state: { filterObjectId: next.object_id },
      });
    }
    setSelected(next);
  }

  return (
    <div className={styles.page}>
      <div className={styles.leftPanel}>
        <ObjectBrowser selected={selected} onSelect={handleSelect} />
      </div>
      <div className={styles.rightPanel}>
        {selected ? (
          // Remount on object identity so switching objects wipes the
          // previous series selection / continuous controls.
          <ObjectDetail key={selected.object_id} object={selected} />
        ) : (
          <div className={styles.welcome}>
            <div className={styles.welcomeInner}>
              <h2>Select an object</h2>
              <p>
                Pick an object from the list on the left, apply a filter to list
                its series, then chart one — or build a continuous series
                (futures &amp; options).
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default DataV2Page;
