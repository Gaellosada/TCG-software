import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import OptionStreamForm, { buildDefaultOptionStream, validateOptionStream } from '../OptionStreamForm';
import styles from './InstrumentPickerModal.module.css';

/**
 * Category definitions.
 * Indexes and Assets show instruments directly (no drill-down).
 * Futures and Options keep collection-level navigation (many collections).
 */
const CATEGORY_CONFIG = [
  { key: 'indexes', label: 'Indexes', color: 'var(--cat-indexes)', collections: ['INDEX'] },
  { key: 'assets', label: 'Assets', color: 'var(--cat-assets)', collections: ['ETF', 'FOREX', 'FUND'] },
  { key: 'futures', label: 'Futures', color: 'var(--cat-futures)', dynamicFutures: true },
  { key: 'options', label: 'Options', color: 'var(--cat-options)', dynamicOptions: true },
];

/**
 * Shared InstrumentPickerModal — modal dialog for browsing and selecting
 * instruments. Categorized view with expandable groups, drill-down for
 * futures configuration. Used by Portfolio, Indicators, and Signals pages.
 *
 * Emits the v3 InputInstrument discriminated-union value:
 *   - Spot:         { type: 'spot', collection, instrument_id }
 *   - Continuous:   { type: 'continuous', collection, adjustment, cycle,
 *                     rollOffset, strategy: 'front_month' }
 *   - OptionStream: { type: 'option_stream', collection, option_type, cycle,
 *                     maturity, selection, stream }
 *
 * Props:
 *   isOpen            {boolean}    whether the modal is visible
 *   onClose           {Function}   () => void — close without selection
 *   onSelect          {Function}   (instrument) => void — called on instrument pick
 *   title             {string?}    modal heading (default: "Select Instrument")
 *   hiddenCategories  {string[]?}  category keys to hide (default: []).
 *                                  e.g. ['options'] to suppress the Options
 *                                  tab on a page that only handles cash/futures.
 */
export default function InstrumentPickerModal({
  isOpen,
  onClose,
  onSelect,
  title,
  hiddenCategories = [],
}) {
  const [allCollections, setAllCollections] = useState([]);
  const [collectionsLoading, setCollectionsLoading] = useState(false);
  const [collectionsError, setCollectionsError] = useState(null);

  const [instrumentsByCollection, setInstrumentsByCollection] = useState({});
  const [instrumentsLoading, setInstrumentsLoading] = useState(false);

  const [expanded, setExpanded] = useState({});

  // Futures drill-down state
  const [selectedFutCollection, setSelectedFutCollection] = useState(null);
  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [rollOffset, setRollOffset] = useState(2);
  const [availableCycles, setAvailableCycles] = useState([]);

  // Options drill-down state
  const [optionRoots, setOptionRoots] = useState([]);
  const [optionRootsLoading, setOptionRootsLoading] = useState(false);
  const [optionRootsError, setOptionRootsError] = useState(null);
  const [inOptionsDrillDown, setInOptionsDrillDown] = useState(false);
  const [optionStreamValue, setOptionStreamValue] = useState(null);

  const overlayRef = useRef(null);

  const visibleCategories = useMemo(
    () => CATEGORY_CONFIG.filter((c) => !hiddenCategories.includes(c.key)),
    [hiddenCategories],
  );
  const optionsVisible = useMemo(
    () => visibleCategories.some((c) => c.key === 'options'),
    [visibleCategories],
  );

  /* ── Load collections + instruments when modal opens ── */
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;

    setCollectionsLoading(true);
    setCollectionsError(null);

    (async () => {
      try {
        const collections = await listCollections();
        if (cancelled) return;
        setAllCollections(collections);

        setInstrumentsLoading(true);
        const nonFut = CATEGORY_CONFIG
          .filter((c) => !c.dynamicFutures)
          .flatMap((c) => c.collections)
          .filter((c) => collections.includes(c));

        const results = await Promise.all(
          nonFut.map(async (coll) => {
            const res = await listInstruments(coll, { skip: 0, limit: 500 });
            return [coll, res.items || []];
          }),
        );

        if (!cancelled) {
          const map = {};
          for (const [coll, items] of results) map[coll] = items;
          setInstrumentsByCollection(map);
          setInstrumentsLoading(false);
        }

        if (!cancelled) setCollectionsLoading(false);
      } catch (err) {
        if (!cancelled) {
          setCollectionsError(err.message);
          setCollectionsLoading(false);
          setInstrumentsLoading(false);
        }
      }
    })();

    return () => { cancelled = true; };
  }, [isOpen]);

  /* ── Load option roots when modal opens (only when options visible) ── */
  useEffect(() => {
    if (!isOpen || !optionsVisible) return;
    let cancelled = false;
    setOptionRootsLoading(true);
    setOptionRootsError(null);
    getOptionRoots()
      .then((resp) => {
        if (cancelled) return;
        setOptionRoots(resp.roots || []);
        setOptionRootsLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setOptionRootsError(err?.message || 'Failed to load option roots');
        setOptionRoots([]);
        setOptionRootsLoading(false);
      });
    return () => { cancelled = true; };
  }, [isOpen, optionsVisible]);

  /* ── Load available cycles for futures drill-down ── */
  useEffect(() => {
    if (!selectedFutCollection) {
      setAvailableCycles([]);
      return;
    }
    let cancelled = false;
    getAvailableCycles(selectedFutCollection)
      .then((cycles) => { if (!cancelled) setAvailableCycles(cycles); })
      .catch(() => { if (!cancelled) setAvailableCycles([]); });
    return () => { cancelled = true; };
  }, [selectedFutCollection]);

  /* ── ESC to close ── */
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isOpen, onClose]);

  /* ── Reset on close ── */
  useEffect(() => {
    if (!isOpen) {
      setSelectedFutCollection(null);
      setAdjustment('none');
      setCycle('');
      setRollOffset(2);
      setExpanded({});
      setInOptionsDrillDown(false);
      setOptionStreamValue(null);
    }
  }, [isOpen]);

  const toggleCategory = useCallback((key) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const handleOverlayClick = useCallback(
    (e) => { if (e.target === overlayRef.current) onClose(); },
    [onClose],
  );

  const handleSelectInstrument = useCallback(
    (symbol, collection) => {
      onSelect({ type: 'spot', collection, instrument_id: symbol });
      onClose();
    },
    [onSelect, onClose],
  );

  const handleSelectContinuous = useCallback(
    (collection) => {
      onSelect({
        type: 'continuous',
        collection,
        strategy: 'front_month',
        adjustment,
        cycle: cycle || null,
        rollOffset,
      });
      onClose();
    },
    [adjustment, cycle, rollOffset, onSelect, onClose],
  );

  const handleBackFromFut = useCallback(() => {
    setSelectedFutCollection(null);
    setAdjustment('none');
    setCycle('');
    setRollOffset(2);
  }, []);

  const handleEnterOptionsDrillDown = useCallback(() => {
    setInOptionsDrillDown(true);
    setOptionStreamValue((prev) => prev || buildDefaultOptionStream({ availableRoots: optionRoots }));
  }, [optionRoots]);

  const handleBackFromOptions = useCallback(() => {
    setInOptionsDrillDown(false);
    setOptionStreamValue(null);
  }, []);

  const handleConfirmOptionStream = useCallback(() => {
    if (!optionStreamValue) return;
    if (validateOptionStream(optionStreamValue, optionRoots) !== null) return;
    onSelect(optionStreamValue);
    onClose();
  }, [optionStreamValue, optionRoots, onSelect, onClose]);

  if (!isOpen) return null;

  const futCollections = allCollections.filter((c) => c.startsWith('FUT_'));
  const inFutDrillDown = selectedFutCollection !== null;
  const futuresVisible = visibleCategories.some((c) => c.key === 'futures');
  const optionStreamValidation = optionStreamValue
    ? validateOptionStream(optionStreamValue, optionRoots)
    : null;
  const confirmDisabled = !optionStreamValue || optionStreamValidation !== null;

  return (
    <div
      className={styles.overlay}
      ref={overlayRef}
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-label={title || 'Select Instrument'}
    >
      <div className={styles.modal}>
        {/* Header */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            {(inFutDrillDown || inOptionsDrillDown) && (
              <button
                className={styles.backBtn}
                type="button"
                onClick={inFutDrillDown ? handleBackFromFut : handleBackFromOptions}
              >
                &#8592;
              </button>
            )}
            <h3 className={styles.title}>
              {inFutDrillDown
                ? selectedFutCollection
                : inOptionsDrillDown
                  ? 'Options'
                  : (title || 'Select Instrument')}
            </h3>
          </div>
          <button className={styles.closeBtn} type="button" onClick={onClose} aria-label="Close">
            &#215;
          </button>
        </div>

        {/* Body */}
        <div className={styles.body}>
          {collectionsLoading && (
            <div className={styles.state}>Loading...</div>
          )}
          {collectionsError && (
            <div className={styles.error}>{collectionsError}</div>
          )}

          {inOptionsDrillDown ? (
            /* ── Options: pick an OptionStreamRef ── */
            <div className={styles.continuousSection}>
              {optionRootsLoading && <div className={styles.state}>Loading roots...</div>}
              {optionRootsError && <div className={styles.error}>{optionRootsError}</div>}
              {!optionRootsLoading && !optionRootsError && (
                <>
                  <OptionStreamForm
                    value={optionStreamValue}
                    onChange={setOptionStreamValue}
                    availableRoots={optionRoots}
                  />
                  <button
                    className={styles.selectContinuousBtn}
                    type="button"
                    onClick={handleConfirmOptionStream}
                    disabled={confirmDisabled}
                    title={optionStreamValidation ? optionStreamValidation.message : undefined}
                    data-testid="option-stream-confirm"
                  >
                    Confirm
                  </button>
                </>
              )}
            </div>
          ) : inFutDrillDown ? (
            /* ── Futures: configure continuous series ── */
            <div className={styles.continuousSection}>
              <p className={styles.continuousText}>
                <strong>{selectedFutCollection}</strong> will be added as a
                continuous rolled series (front month).
              </p>

              <div className={styles.rollingOptions}>
                <label className={styles.optionLabel}>
                  Adjustment
                  <select
                    className={styles.optionSelect}
                    value={adjustment}
                    onChange={(e) => setAdjustment(e.target.value)}
                  >
                    <option value="none">None</option>
                    <option value="ratio">Ratio</option>
                    <option value="difference">Difference</option>
                  </select>
                </label>

                <label className={styles.optionLabel}>
                  Cycle
                  <select
                    className={styles.optionSelect}
                    value={cycle}
                    onChange={(e) => setCycle(e.target.value)}
                  >
                    <option value="">All</option>
                    {availableCycles.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </label>

                <label className={styles.optionLabel}>
                  Roll Offset (days)
                  <input
                    type="number"
                    className={styles.optionSelect}
                    style={{ width: '56px' }}
                    value={rollOffset}
                    min={0}
                    max={30}
                    onChange={(e) => setRollOffset(Math.max(0, Math.min(30, parseInt(e.target.value, 10) || 0)))}
                  />
                </label>
              </div>

              <button
                className={styles.selectContinuousBtn}
                type="button"
                onClick={() => handleSelectContinuous(selectedFutCollection)}
              >
                Select Continuous Series
              </button>
            </div>
          ) : (
            /* ── Main view: toggleable categories ── */
            <>
              {visibleCategories.filter((c) => !c.dynamicFutures && !c.dynamicOptions).map((cat) => {
                const instruments = cat.collections.flatMap(
                  (coll) => (instrumentsByCollection[coll] || []).map((inst) => ({ ...inst, collection: coll })),
                );
                if (instruments.length === 0 && !instrumentsLoading) return null;
                const isExpanded = !!expanded[cat.key];
                return (
                  <div key={cat.key} className={styles.group}>
                    <button
                      className={styles.groupToggle}
                      type="button"
                      onClick={() => toggleCategory(cat.key)}
                    >
                      <span className={styles.groupDot} style={{ background: cat.color }} />
                      <span className={styles.groupLabel}>{cat.label}</span>
                      <span className={styles.groupCount}>{instruments.length}</span>
                      <span className={styles.chevron}>{isExpanded ? '\u25BE' : '\u25B8'}</span>
                    </button>
                    {isExpanded && (
                      instrumentsLoading ? (
                        <div className={styles.state}>Loading...</div>
                      ) : (
                        <ul className={styles.instrumentList}>
                          {instruments.map((inst) => (
                            <li
                              key={`${inst.collection}-${inst.symbol}`}
                              className={styles.instrumentItem}
                              role="button"
                              tabIndex={0}
                              onClick={() => handleSelectInstrument(inst.symbol, inst.collection)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') handleSelectInstrument(inst.symbol, inst.collection);
                              }}
                            >
                              <span className={styles.instrumentSymbol}>{inst.symbol}</span>
                            </li>
                          ))}
                        </ul>
                      )
                    )}
                  </div>
                );
              })}

              {/* Futures — collection-level drill-down */}
              {futuresVisible && futCollections.length > 0 && (
                <div className={styles.group}>
                  <button
                    className={styles.groupToggle}
                    type="button"
                    onClick={() => toggleCategory('futures')}
                  >
                    <span className={styles.groupDot} style={{ background: 'var(--cat-futures)' }} />
                    <span className={styles.groupLabel}>Futures</span>
                    <span className={styles.groupCount}>{futCollections.length}</span>
                    <span className={styles.chevron}>{expanded.futures ? '\u25BE' : '\u25B8'}</span>
                  </button>
                  {expanded.futures && (
                    <ul className={styles.collectionList}>
                      {futCollections.map((coll) => (
                        <li
                          key={coll}
                          className={styles.collectionItem}
                          role="button"
                          tabIndex={0}
                          onClick={() => setSelectedFutCollection(coll)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') setSelectedFutCollection(coll);
                          }}
                        >
                          <span>{coll}</span>
                          <span className={styles.chevron}>&#8250;</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* Options — drill into stream-form */}
              {optionsVisible && (
                <div className={styles.group}>
                  <button
                    className={styles.groupToggle}
                    type="button"
                    onClick={handleEnterOptionsDrillDown}
                    data-testid="picker-options-toggle"
                  >
                    <span className={styles.groupDot} style={{ background: 'var(--cat-options)' }} />
                    <span className={styles.groupLabel}>Options</span>
                    <span className={styles.groupCount}>
                      {optionRootsLoading ? '...' : optionRoots.length}
                    </span>
                    <span className={styles.chevron}>&#8250;</span>
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
