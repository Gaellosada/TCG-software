import { useState, useEffect, useRef } from 'react';
import { listCollections, listInstruments } from '../../api/data';
import styles from './ParamsPanel.module.css';

/**
 * Cascaded collection + instrument picker.
 *
 * Reuses ``api/data.js`` — the same discovery helpers the Data page uses —
 * so a new instrument added to MongoDB is immediately visible here without
 * any backend change.
 *
 * Props:
 *   value             {Object|null} optional controlled pre-selection
 *                                   ``{collection, instrument_id}``
 *   onSave            {Function}    ({collection, instrument_id}) => void
 *                                   — submit handler; preferred over onAdd
 *   onAdd             {Function}    legacy alias for onSave; still honoured
 *   onCancel          {Function}    () => void
 *   defaultCollection {string}      pre-select this collection if present
 *   saveLabel         {string}      button label (defaults to "Save" when
 *                                   value supplied, "Add" otherwise)
 *
 * Per-collection instrument lists are memoised in a ``useRef`` so re-opening
 * the adder doesn't refetch.
 */
function SeriesPicker({ value, onSave, onAdd, onCancel, defaultCollection, saveLabel }) {
  const [collections, setCollections] = useState([]);
  const [collection, setCollection] = useState(value?.collection || '');
  const [instruments, setInstruments] = useState([]);
  const [instrumentId, setInstrumentId] = useState(value?.instrument_id || '');
  const [loadingCollections, setLoadingCollections] = useState(true);
  const [loadingInstruments, setLoadingInstruments] = useState(false);
  const [error, setError] = useState(null);

  // Cache instrument lists per collection across re-opens of the picker.
  const instrumentsCache = useRef(new Map());

  const submitHandler = onSave || onAdd;

  // Load collections on mount; choose the initial collection in priority
  // order: controlled ``value.collection`` → defaultCollection → 'INDEX'
  // → first available.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await listCollections();
        if (cancelled) return;
        setCollections(all || []);
        // Respect existing controlled value if it's valid.
        if (value?.collection && all.includes(value.collection)) {
          setCollection(value.collection);
        } else if (defaultCollection && all.includes(defaultCollection)) {
          setCollection(defaultCollection);
        } else if (all.includes('INDEX')) {
          setCollection('INDEX');
        } else if (all.length > 0) {
          setCollection(all[0]);
        }
      } catch (e) {
        if (!cancelled) setError(e.message || 'Failed to load collections');
      } finally {
        if (!cancelled) setLoadingCollections(false);
      }
    })();
    return () => { cancelled = true; };
    // Only run on mount — changes to ``value`` don't re-fire because the
    // picker is unmounted/remounted by the caller when switching target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load instruments whenever the selected collection changes.
  useEffect(() => {
    if (!collection) {
      setInstruments([]);
      setInstrumentId('');
      return undefined;
    }
    let cancelled = false;

    const cached = instrumentsCache.current.get(collection);
    if (cached) {
      setInstruments(cached);
      // Preserve controlled instrument_id if it's in the cached list.
      if (value?.instrument_id && cached.some((it) => it.symbol === value.instrument_id)) {
        setInstrumentId(value.instrument_id);
      } else {
        setInstrumentId(cached.length > 0 ? cached[0].symbol : '');
      }
      return undefined;
    }

    setLoadingInstruments(true);
    setError(null);
    (async () => {
      try {
        const res = await listInstruments(collection, { skip: 0, limit: 500 });
        if (cancelled) return;
        const items = (res && res.items) || [];
        instrumentsCache.current.set(collection, items);
        setInstruments(items);
        if (value?.instrument_id && items.some((it) => it.symbol === value.instrument_id)) {
          setInstrumentId(value.instrument_id);
        } else {
          setInstrumentId(items.length > 0 ? items[0].symbol : '');
        }
      } catch (e) {
        if (!cancelled) {
          setError(e.message || 'Failed to load instruments');
          setInstruments([]);
          setInstrumentId('');
        }
      } finally {
        if (!cancelled) setLoadingInstruments(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collection]);

  function submit() {
    if (!collection || !instrumentId) return;
    if (submitHandler) submitHandler({ collection, instrument_id: instrumentId });
  }

  const effectiveSaveLabel = saveLabel || (value ? 'Save' : 'Add');

  return (
    <div className={styles.adder}>
      <select
        className={styles.adderSelect}
        value={collection}
        onChange={(e) => setCollection(e.target.value)}
        disabled={loadingCollections || collections.length === 0}
        aria-label="collection"
      >
        {loadingCollections && <option value="">Loading...</option>}
        {!loadingCollections && collections.length === 0 && (
          <option value="">No collections</option>
        )}
        {collections.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>

      <select
        className={styles.adderSelect}
        value={instrumentId}
        onChange={(e) => setInstrumentId(e.target.value)}
        disabled={loadingInstruments || instruments.length === 0}
        aria-label="instrument"
      >
        {loadingInstruments && <option value="">Loading...</option>}
        {!loadingInstruments && instruments.length === 0 && (
          <option value="">No instruments</option>
        )}
        {instruments.map((inst) => (
          <option key={inst.symbol} value={inst.symbol}>{inst.symbol}</option>
        ))}
      </select>

      {error && (
        <div className={styles.errorBanner}>
          <span className={styles.errorText}>{error}</span>
        </div>
      )}

      <div className={styles.adderActions}>
        <button
          className={styles.miniBtn}
          onClick={submit}
          disabled={!collection || !instrumentId}
        >
          {effectiveSaveLabel}
        </button>
        <button className={styles.miniBtn} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export default SeriesPicker;
