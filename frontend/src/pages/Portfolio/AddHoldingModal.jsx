import { useState, useEffect, useRef, useCallback } from 'react';
import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import styles from './AddHoldingModal.module.css';

/**
 * Category definitions.
 * Indexes and Assets show instruments directly (no drill-down).
 * Futures keep collection-level navigation (many collections).
 */
const CATEGORY_CONFIG = [
  { key: 'indexes', label: 'Indexes', color: 'var(--cat-indexes)', collections: ['INDEX'] },
  { key: 'assets', label: 'Assets', color: 'var(--cat-assets)', collections: ['ETF', 'FOREX', 'FUND'] },
  { key: 'futures', label: 'Futures', color: 'var(--cat-futures)', dynamicFutures: true },
];

export default function AddHoldingModal({ isOpen, onClose, onAddLeg }) {
  const [allCollections, setAllCollections] = useState([]);
  const [collectionsLoading, setCollectionsLoading] = useState(false);
  const [collectionsError, setCollectionsError] = useState(null);

  // Instruments for indexes/assets, loaded upfront keyed by collection
  const [instrumentsByCollection, setInstrumentsByCollection] = useState({});
  const [instrumentsLoading, setInstrumentsLoading] = useState(false);

  // Category expand state — all collapsed by default
  const [expanded, setExpanded] = useState({});

  // Futures drill-down state
  const [selectedFutCollection, setSelectedFutCollection] = useState(null);
  const [labelInput, setLabelInput] = useState('');
  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [rollOffset, setRollOffset] = useState(2);
  const [availableCycles, setAvailableCycles] = useState([]);

  const overlayRef = useRef(null);

  /* ── Load collections + instruments for indexes/assets when modal opens ── */
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

        // Load instruments for all non-futures collections upfront
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

  /* ── Load available cycles when a futures collection is selected ── */
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
      setLabelInput('');
      setAdjustment('none');
      setCycle('');
      setRollOffset(2);
      setExpanded({});
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
      onAddLeg({
        label: symbol,
        type: 'instrument',
        collection,
        symbol,
        weight: 100,
      });
      onClose();
    },
    [onAddLeg, onClose],
  );

  const handleAddContinuous = useCallback(
    (collection) => {
      const label = labelInput.trim() || collection;
      onAddLeg({
        label,
        type: 'continuous',
        collection,
        strategy: 'front_month',
        adjustment,
        cycle: cycle || null,
        rollOffset,
        weight: 100,
      });
      onClose();
    },
    [labelInput, adjustment, cycle, rollOffset, onAddLeg, onClose],
  );

  const handleBackFromFut = useCallback(() => {
    setSelectedFutCollection(null);
    setLabelInput('');
    setAdjustment('none');
    setCycle('');
    setRollOffset(2);
  }, []);

  if (!isOpen) return null;

  const futCollections = allCollections.filter((c) => c.startsWith('FUT_'));
  const inFutDrillDown = selectedFutCollection !== null;

  return (
    <div
      className={styles.overlay}
      ref={overlayRef}
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-label="Add holding to portfolio"
    >
      <div className={styles.modal}>
        {/* Header */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            {inFutDrillDown && (
              <button className={styles.backBtn} type="button" onClick={handleBackFromFut}>
                &#8592;
              </button>
            )}
            <h3 className={styles.title}>
              {inFutDrillDown ? selectedFutCollection : 'Add Holding'}
            </h3>
          </div>
          <button className={styles.closeBtn} type="button" onClick={onClose} aria-label="Close">
            &#215;
          </button>
        </div>

        {/* Label input — only for futures drill-down */}
        {inFutDrillDown && (
          <div className={styles.labelRow}>
            <label className={styles.labelText} htmlFor="holding-label">Label</label>
            <input
              id="holding-label"
              className={styles.labelInput}
              type="text"
              value={labelInput}
              onChange={(e) => setLabelInput(e.target.value)}
              placeholder="Custom label (optional)"
              spellCheck={false}
            />
          </div>
        )}

        {/* Body */}
        <div className={styles.body}>
          {collectionsLoading && (
            <div className={styles.state}>Loading...</div>
          )}
          {collectionsError && (
            <div className={styles.error}>{collectionsError}</div>
          )}

          {inFutDrillDown ? (
            /* ── Futures: add as continuous series ── */
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
                    <option value="proportional">Proportional</option>
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
                className={styles.addContinuousBtn}
                type="button"
                onClick={() => handleAddContinuous(selectedFutCollection)}
              >
                Add Continuous Series
              </button>
            </div>
          ) : (
            /* ── Main view: toggleable categories ── */
            <>
              {/* Indexes & Assets — instruments shown directly when expanded */}
              {CATEGORY_CONFIG.filter((c) => !c.dynamicFutures).map((cat) => {
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

              {/* Futures — toggleable, then collection-level drill-down */}
              {futCollections.length > 0 && (
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
