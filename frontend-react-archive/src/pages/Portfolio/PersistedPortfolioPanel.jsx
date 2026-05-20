// PersistedPortfolioPanel — panel that shows backend-persisted portfolios
// filtered by category, with category selector, + Save current, per-row
// category chip, row-level select, and row-level archive.
//
// Styled to match the shared Card component (same container, header title,
// font sizes, row layout) used throughout the Portfolio page (HoldingsList,
// etc.). Per-row category chip and hover-reveal actions follow the same
// patterns as SignalsList.
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

import Card from '../../components/Card';
import styles from './PersistedPortfolioPanel.module.css';
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
  const headerRight = (
    <div className={styles.headerActions}>
      <label className={styles.categoryLabel} htmlFor="portfolio-category-select">
        Category
      </label>
      <select
        id="portfolio-category-select"
        className={styles.categorySelect}
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
        className={styles.addBtn}
        onClick={onSaveCurrent}
        disabled={!!saveDisabled}
        title="Save current portfolio to this category"
        aria-label="Save current portfolio"
        data-testid="persist-portfolio-btn"
      >
        + Save current
      </button>
    </div>
  );

  return (
    <Card
      title="Saved Portfolios"
      right={headerRight}
      data-testid="persisted-portfolio-panel"
    >
      <div className={styles.list}>
        {loading ? (
          <div className={styles.empty}>Loading...</div>
        ) : portfolios.length === 0 ? (
          <div className={styles.empty} data-testid="persisted-portfolio-empty">
            No saved portfolios in {category} — click &quot;+ Save current&quot; to add one.
          </div>
        ) : (
          portfolios.map((p) => {
            const isSelected = p.id === selectedId;
            return (
              <div
                key={p.id}
                className={`${styles.row}${isSelected ? ` ${styles.rowActive}` : ''}`}
                data-testid={`persisted-portfolio-row-${p.id}`}
                data-selected={isSelected ? 'true' : 'false'}
              >
                <button
                  type="button"
                  className={styles.rowName}
                  title={`Load ${p.name}`}
                  onClick={() => onSelect && onSelect(p.id)}
                  data-testid={`load-portfolio-${p.id}`}
                >
                  {p.name}
                </button>
                <select
                  className={styles.rowCatSelect}
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
                  className={styles.rowDeleteBtn}
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
    </Card>
  );
}

export default PersistedPortfolioPanel;
