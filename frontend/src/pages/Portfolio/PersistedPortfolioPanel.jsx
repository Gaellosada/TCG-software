// PersistedPortfolioPanel — panel that shows backend-persisted portfolios
// filtered by category, with category selector, + Save current, per-row
// category chip, row-level select, and row-level archive.
//
// Props:
//   category              {string}   currently selected category
//   onCategoryChange      {Function} (cat) => void
//   portfolios            {Array}    [{id, name, category, ...}]
//   loading               {boolean}
//   onSaveCurrent         {Function} () => void — save current portfolio state
//   saveDisabled          {boolean}  disable the save button
//   onChangeItemCat       {Function} (id, newCat) => void
//   onArchive             {Function} (id) => void
//   selectedId            {string|null} id of the currently loaded portfolio
//   onSelect              {Function} (id) => void — load this portfolio into editor

import styles from './PortfolioPage.module.css';
import { CATEGORIES } from '../../api/persistence';

function PersistedPortfolioPanel({
  category,
  onCategoryChange,
  portfolios,
  loading,
  onSaveCurrent,
  saveDisabled,
  onChangeItemCat,
  onArchive,
  selectedId,
  onSelect,
}) {
  return (
    <div className={styles.persistedPanel} data-testid="persisted-portfolio-panel">
      <div className={styles.persistedPanelHeader}>
        <span className={styles.persistedPanelTitle}>Saved Portfolios</span>
        <label className={styles.persistedCategoryLabel} htmlFor="portfolio-category-select">
          Category
        </label>
        <select
          id="portfolio-category-select"
          className={styles.persistedCategorySelect}
          value={category}
          onChange={(e) => onCategoryChange(e.target.value)}
          aria-label="Filter portfolios by category"
          data-testid="portfolio-category-filter"
        >
          {CATEGORIES.map((cat) => (
            <option key={cat} value={cat}>{cat}</option>
          ))}
        </select>
        <button
          type="button"
          className={styles.persistedAddBtn}
          onClick={onSaveCurrent}
          disabled={!!saveDisabled}
          title="Save current portfolio to this category"
          aria-label="Save current portfolio"
          data-testid="persist-portfolio-btn"
        >
          + Save current
        </button>
      </div>
      <div className={styles.persistedList}>
        {loading ? (
          <div className={styles.persistedEmpty}>Loading...</div>
        ) : portfolios.length === 0 ? (
          <div className={styles.persistedEmpty} data-testid="persisted-portfolio-empty">
            No saved portfolios in {category} — click &quot;+ Save current&quot; to add one.
          </div>
        ) : (
          portfolios.map((p) => {
            const isSelected = p.id === selectedId;
            return (
              <div
                key={p.id}
                className={`${styles.persistedRow} ${isSelected ? styles.persistedRowActive || '' : ''}`}
                data-testid={`persisted-portfolio-row-${p.id}`}
                data-selected={isSelected ? 'true' : 'false'}
              >
                <button
                  type="button"
                  className={styles.persistedRowName}
                  title={`Load ${p.name}`}
                  onClick={() => onSelect && onSelect(p.id)}
                  data-testid={`load-portfolio-${p.id}`}
                >
                  {p.name}
                </button>
                <select
                  className={styles.persistedRowCatSelect}
                  value={p.category}
                  onChange={(e) => onChangeItemCat(p.id, e.target.value)}
                  aria-label={`Category for ${p.name}`}
                  data-testid={`portfolio-cat-select-${p.id}`}
                  title="Move to category"
                >
                  {CATEGORIES.map((cat) => (
                    <option key={cat} value={cat}>{cat}</option>
                  ))}
                </select>
                <button
                  type="button"
                  className={styles.persistedRowDeleteBtn}
                  onClick={() => onArchive(p.id)}
                  title="Archive portfolio"
                  aria-label={`Archive ${p.name}`}
                  data-testid={`archive-portfolio-${p.id}`}
                >
                  ×
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

export default PersistedPortfolioPanel;
