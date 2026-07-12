import { useCallback, useEffect, useRef, useState } from 'react';
import { listPortfolios, describePersistenceError } from '../../api/persistence';
// Reuse the signal picker's stylesheet verbatim — same modal chrome, list rows,
// category selector, empty/error states (UI-consistency rule: one look, no
// page-specific variant).
import styles from './SignalPickerModal.module.css';

// Categories selectable in this picker. Mirrors SignalPickerModal — excludes
// ARCHIVE (you don't build on an archived block). Default RESEARCH.
const PICKER_CATEGORIES = /** @type {const} */ (['RESEARCH', 'DEV', 'PROD']);
const DEFAULT_CATEGORY = 'RESEARCH';

// A portfolio is referenceable (a "pure" building block) iff it has no
// portfolio-ref legs. ``kind`` is authoritative; legacy docs (no ``kind``) are
// pure. Belt-and-suspenders: also treat any doc whose legs contain a portfolio
// leg as non-pure even if ``kind`` is missing/stale (depth-1 enforcement #1).
function isPure(doc) {
  if (doc && doc.kind === 'composed') return false;
  const legs = doc && Array.isArray(doc.legs) ? doc.legs : [];
  return !legs.some((l) => l && l.type === 'portfolio');
}

/**
 * Single-step modal for adding a saved PURE portfolio as a composed leg.
 *
 * Lists ONLY ``kind:"pure"`` (or legacy) portfolios in the selected category —
 * this is depth-1 enforcement #1 (the backend guard is the real backstop).
 *
 * Props:
 *   isOpen   {boolean}
 *   onClose  {Function}  () => void
 *   onSelect {Function}  (portfolioDoc) => void — receives the chosen doc
 *   excludeId {string=}  a portfolio id to hide (e.g. the one being edited, so
 *                        it can't reference itself)
 */
export default function PortfolioPickerModal({ isOpen, onClose, onSelect, excludeId = null }) {
  const closeRef = useRef(null);

  const [category, setCategory] = useState(DEFAULT_CATEGORY);
  const [portfolios, setPortfolios] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Monotonic request token — ignore resolutions from a superseded fetch.
  const reqTokenRef = useRef(0);

  const fetchPortfolios = useCallback(async (cat) => {
    const token = ++reqTokenRef.current;
    setLoading(true);
    setError(null);
    try {
      const docs = await listPortfolios(cat);
      if (token !== reqTokenRef.current) return;
      const pure = (Array.isArray(docs) ? docs : [])
        .filter(isPure)
        .filter((d) => d.id !== excludeId);
      setPortfolios(pure);
    } catch (err) {
      if (token !== reqTokenRef.current) return;
      setError(describePersistenceError(err));
      setPortfolios([]);
    } finally {
      if (token === reqTokenRef.current) setLoading(false);
    }
  }, [excludeId]);

  // Fetch on open and on category change while open.
  useEffect(() => {
    if (!isOpen) return;
    fetchPortfolios(category);
  }, [isOpen, category, fetchPortfolios]);

  // Reset on close.
  useEffect(() => {
    if (!isOpen) {
      reqTokenRef.current += 1;
      setCategory(DEFAULT_CATEGORY);
      setPortfolios([]);
      setLoading(false);
      setError(null);
    }
  }, [isOpen]);

  // Focus close button on open.
  useEffect(() => {
    if (!isOpen) return undefined;
    const t = setTimeout(() => {
      if (closeRef.current) closeRef.current.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [isOpen]);

  // Escape closes.
  useEffect(() => {
    if (!isOpen) return undefined;
    function onKey(e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className={styles.backdrop}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      data-testid="portfolio-picker-backdrop"
    >
      <div
        className={styles.card}
        role="dialog"
        aria-modal="true"
        aria-labelledby="portfolio-picker-title"
        data-testid="portfolio-picker"
      >
        <div className={styles.header}>
          <h3 id="portfolio-picker-title" className={styles.title}>
            Add Portfolio
          </h3>
          <button
            ref={closeRef}
            className={styles.closeBtn}
            type="button"
            onClick={onClose}
            aria-label="Close"
          >
            &#215;
          </button>
        </div>

        {/* Category selector — mirrors the signal picker. */}
        <div className={styles.categoryRow}>
          <label className={styles.categoryLabel} htmlFor="portfolio-picker-category-select">
            Category
          </label>
          <select
            id="portfolio-picker-category-select"
            className={styles.categorySelect}
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            aria-label="Filter portfolios by category"
            data-testid="portfolio-picker-category"
          >
            {PICKER_CATEGORIES.map((cat) => (
              <option key={cat} value={cat}>{cat}</option>
            ))}
          </select>
        </div>

        {loading ? (
          <div className={styles.empty} data-testid="portfolio-picker-loading">
            Loading portfolios&#8230;
          </div>
        ) : error ? (
          <div className={styles.error} data-testid="portfolio-picker-error">
            <div className={styles.errorMsg}>
              <strong>Failed to load portfolios:</strong> {error}
            </div>
            <button
              className={styles.retryBtn}
              type="button"
              onClick={() => fetchPortfolios(category)}
            >
              Retry
            </button>
          </div>
        ) : portfolios.length === 0 ? (
          <div className={styles.empty}>
            No pure portfolios in this category. Build one on the Portfolio page first.
          </div>
        ) : (
          <div className={styles.list}>
            {portfolios.map((pf) => {
              const legCount = Array.isArray(pf.legs) ? pf.legs.length : 0;
              return (
                <div key={pf.id} className={styles.signalRow} data-testid={`portfolio-picker-row-${pf.id}`}>
                  <div className={styles.signalInfo}>
                    <div className={styles.signalName}>{pf.name}</div>
                    <div className={styles.signalMeta}>
                      {legCount} leg{legCount !== 1 ? 's' : ''}
                      {pf.rebalance && pf.rebalance !== 'none' ? ` · rebalance ${pf.rebalance}` : ''}
                    </div>
                  </div>
                  <button
                    className={styles.selectBtn}
                    type="button"
                    onClick={() => onSelect(pf)}
                    aria-label={`Add portfolio ${pf.name}`}
                  >
                    Add
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
