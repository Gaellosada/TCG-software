import { useEffect, useMemo, useState } from 'react';
import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import styles from './InstrumentPicker.module.css';

/**
 * Shared InstrumentPicker — single source of truth for "pick a price
 * series" across the app (used by Signals Inputs panel; designed to be
 * adopted by Portfolio / Indicators too).
 *
 * Emits the v3 InputInstrument discriminated-union value:
 *   - Spot:       { type: 'spot', collection, instrument_id }
 *   - Continuous: { type: 'continuous', collection, adjustment, cycle,
 *                   rollOffset, strategy: 'front_month' }
 *
 * A single row of controls:
 *   [ type | collection | instrument-or-continuous-details ]
 *
 * Value is controlled — caller owns ``value`` and receives ``onChange``
 * on every edit. Pass ``value = null`` for an empty picker.
 *
 * Props:
 *   value      {Object|null}  current InputInstrument (or null)
 *   onChange   {Function}     (nextInstrument | null) => void
 *   ariaLabel  {string?}      accessibility label
 *   testId     {string?}      data-testid prefix (default 'instrument-picker')
 */
export default function InstrumentPicker({ value, onChange, ariaLabel, testId }) {
  const tid = testId || 'instrument-picker';
  const typeValue = value && value.type === 'continuous' ? 'continuous' : 'spot';

  const [collections, setCollections] = useState([]);
  const [loadingCollections, setLoadingCollections] = useState(false);
  const [instrumentsByCollection, setInstrumentsByCollection] = useState({});
  const [loadingInstruments, setLoadingInstruments] = useState(false);
  const [availableCycles, setAvailableCycles] = useState([]);

  // Load collections on mount.
  useEffect(() => {
    let cancelled = false;
    setLoadingCollections(true);
    (async () => {
      try {
        const cols = await listCollections();
        if (!cancelled) setCollections(cols);
      } catch {
        if (!cancelled) setCollections([]);
      } finally {
        if (!cancelled) setLoadingCollections(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // For spot: load instruments whenever the picked collection changes.
  const spotCollection = typeValue === 'spot' && value ? value.collection : '';
  useEffect(() => {
    if (typeValue !== 'spot') return undefined;
    if (!spotCollection) return undefined;
    if (instrumentsByCollection[spotCollection]) return undefined;
    let cancelled = false;
    setLoadingInstruments(true);
    (async () => {
      try {
        const res = await listInstruments(spotCollection, { skip: 0, limit: 500 });
        if (!cancelled) {
          setInstrumentsByCollection((prev) => ({
            ...prev,
            [spotCollection]: res.items || [],
          }));
        }
      } catch {
        if (!cancelled) {
          setInstrumentsByCollection((prev) => ({ ...prev, [spotCollection]: [] }));
        }
      } finally {
        if (!cancelled) setLoadingInstruments(false);
      }
    })();
    return () => { cancelled = true; };
  }, [typeValue, spotCollection, instrumentsByCollection]);

  // For continuous: load available cycles when collection changes.
  const contCollection = typeValue === 'continuous' && value ? value.collection : '';
  useEffect(() => {
    if (typeValue !== 'continuous') { setAvailableCycles([]); return undefined; }
    if (!contCollection) { setAvailableCycles([]); return undefined; }
    let cancelled = false;
    getAvailableCycles(contCollection)
      .then((cycles) => { if (!cancelled) setAvailableCycles(cycles); })
      .catch(() => { if (!cancelled) setAvailableCycles([]); });
    return () => { cancelled = true; };
  }, [typeValue, contCollection]);

  // Futures collections are those starting with "FUT_" — the only ones
  // that make sense for continuous rolling. Spot accepts any.
  const futCollections = useMemo(
    () => collections.filter((c) => c.startsWith('FUT_')),
    [collections],
  );

  function switchType(nextType) {
    if (nextType === 'spot') {
      onChange({ type: 'spot', collection: '', instrument_id: '' });
    } else {
      onChange({
        type: 'continuous',
        collection: '',
        adjustment: 'none',
        cycle: null,
        rollOffset: 2,
        strategy: 'front_month',
      });
    }
  }

  function updateSpot(patch) {
    const base = (value && value.type === 'spot')
      ? value : { type: 'spot', collection: '', instrument_id: '' };
    onChange({ ...base, ...patch });
  }

  function updateContinuous(patch) {
    const base = (value && value.type === 'continuous')
      ? value : {
        type: 'continuous',
        collection: '',
        adjustment: 'none',
        cycle: null,
        rollOffset: 2,
        strategy: 'front_month',
      };
    onChange({ ...base, ...patch });
  }

  const spotInstruments = (value && value.collection)
    ? (instrumentsByCollection[value.collection] || [])
    : [];

  return (
    <div
      className={styles.picker}
      role="group"
      aria-label={ariaLabel || 'Instrument picker'}
      data-testid={tid}
    >
      <select
        className={styles.control}
        value={typeValue}
        onChange={(e) => switchType(e.target.value)}
        aria-label="Instrument type"
        data-testid={`${tid}-type`}
      >
        <option value="spot">Spot</option>
        <option value="continuous">Continuous</option>
      </select>

      {typeValue === 'spot' ? (
        <>
          <select
            className={styles.control}
            value={value?.collection || ''}
            onChange={(e) => updateSpot({ collection: e.target.value, instrument_id: '' })}
            aria-label="Collection"
            disabled={loadingCollections}
            data-testid={`${tid}-collection`}
          >
            <option value="">{loadingCollections ? 'Loading…' : 'Collection…'}</option>
            {collections.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          <select
            className={styles.control}
            value={value?.instrument_id || ''}
            onChange={(e) => updateSpot({ instrument_id: e.target.value })}
            aria-label="Instrument"
            disabled={!value?.collection || loadingInstruments}
            data-testid={`${tid}-instrument`}
          >
            <option value="">
              {!value?.collection
                ? 'Pick collection first'
                : (loadingInstruments ? 'Loading…' : 'Instrument…')}
            </option>
            {spotInstruments.map((inst) => (
              <option key={inst.symbol} value={inst.symbol}>{inst.symbol}</option>
            ))}
          </select>
        </>
      ) : (
        <>
          <select
            className={styles.control}
            value={value?.collection || ''}
            onChange={(e) => updateContinuous({ collection: e.target.value, cycle: null })}
            aria-label="Futures collection"
            disabled={loadingCollections}
            data-testid={`${tid}-collection`}
          >
            <option value="">
              {loadingCollections ? 'Loading…' : 'Futures collection…'}
            </option>
            {futCollections.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          <select
            className={styles.control}
            value={value?.adjustment || 'none'}
            onChange={(e) => updateContinuous({ adjustment: e.target.value })}
            aria-label="Adjustment"
            data-testid={`${tid}-adjustment`}
          >
            <option value="none">No adjustment</option>
            <option value="proportional">Proportional</option>
            <option value="difference">Difference</option>
          </select>

          <select
            className={styles.control}
            value={value?.cycle || ''}
            onChange={(e) => updateContinuous({ cycle: e.target.value || null })}
            aria-label="Cycle"
            disabled={!value?.collection}
            data-testid={`${tid}-cycle`}
          >
            <option value="">All cycles</option>
            {availableCycles.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          <label className={styles.rollOffsetLabel}>
            <span className={styles.rollOffsetText}>Roll offset</span>
            <input
              type="number"
              min="0"
              max="30"
              step="1"
              className={styles.rollOffsetInput}
              value={Number.isFinite(value?.rollOffset) ? value.rollOffset : 2}
              onChange={(e) => {
                const raw = parseInt(e.target.value, 10);
                const n = Number.isFinite(raw) ? Math.max(0, Math.min(30, raw)) : 0;
                updateContinuous({ rollOffset: n });
              }}
              aria-label="Roll offset (days before expiry)"
              data-testid={`${tid}-roll-offset`}
            />
          </label>
        </>
      )}
    </div>
  );
}
