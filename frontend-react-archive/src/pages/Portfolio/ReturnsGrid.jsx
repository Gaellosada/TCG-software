import { useCallback, useEffect, useMemo, useState } from 'react';
import PillToggle from '../../components/PillToggle';
import { formatReturn, cellBgStyle, toLogReturn } from '../../utils/portfolioMath';
import styles from './ReturnsGrid.module.css';

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH_KEYS = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12'];

function cellClass(value) {
  if (value == null || isNaN(value)) return styles.dim;
  return '';
}

export default function ReturnsGrid({ monthlyReturns, yearlyReturns }) {
  const [selectedView, setSelectedView] = useState('portfolio');
  const [returnMode, setReturnMode] = useState('normal');

  // Derive available view options from data
  const viewOptions = useMemo(() => {
    if (!monthlyReturns || monthlyReturns.length === 0 || !monthlyReturns[0]) return [];
    const keys = Object.keys(monthlyReturns[0]).filter((k) => k !== 'period');
    return keys.map((k) => ({ value: k, label: k === 'portfolio' ? 'Portfolio' : k }));
  }, [monthlyReturns]);

  // Reset selectedView if the previously-selected column is no longer
  // present (e.g. the user recomputed a different portfolio whose legs
  // don't include the same labels).
  useEffect(() => {
    if (viewOptions.length === 0) return;
    if (!viewOptions.some((o) => o.value === selectedView)) {
      setSelectedView('portfolio');
    }
  }, [viewOptions, selectedView]);

  const applyMode = useCallback((val) => returnMode === 'log' ? toLogReturn(val) : val, [returnMode]);

  // Compute max absolute value for color scaling
  const maxAbs = useMemo(() => {
    if (!monthlyReturns) return 0;
    let max = 0;
    for (const row of monthlyReturns) {
      const val = applyMode(row[selectedView]);
      if (val != null && !isNaN(val)) {
        const abs = Math.abs(val);
        if (abs > max) max = abs;
      }
    }
    return max;
  }, [monthlyReturns, selectedView, applyMode]);

  // Build year x month grid
  const { grid, years } = useMemo(() => {
    if (!monthlyReturns || monthlyReturns.length === 0) {
      return { grid: {}, years: [] };
    }

    const g = {};
    monthlyReturns.forEach((row) => {
      const [year, month] = row.period.split('-');
      if (!g[year]) g[year] = {};
      g[year][month] = applyMode(row[selectedView]);
    });

    if (yearlyReturns) {
      yearlyReturns.forEach((row) => {
        const year = row.period;
        if (!g[year]) g[year] = {};
        g[year].total = applyMode(row[selectedView]);
      });
    }

    return { grid: g, years: Object.keys(g).sort() };
  }, [monthlyReturns, yearlyReturns, selectedView, applyMode]);

  if (!monthlyReturns || monthlyReturns.length === 0) return null;

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h3 className={styles.title}>Returns Grid</h3>
        <div className={styles.headerControls}>
          <PillToggle
            options={[
              { value: 'normal', label: 'Normal' },
              { value: 'log', label: 'Log' },
            ]}
            value={returnMode}
            onChange={setReturnMode}
            ariaLabel="Return type"
          />
          {viewOptions.length > 1 && (
            <select
              className={styles.viewSelect}
              value={selectedView}
              onChange={(e) => setSelectedView(e.target.value)}
              aria-label="Select returns view"
            >
              {viewOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className={styles.tableWrapper}>
        <table className={styles.table} aria-label="Year by month returns grid">
          <thead>
            <tr>
              <th className={styles.yearCol}>Year</th>
              {MONTH_LABELS.map((m) => (
                <th key={m}>{m}</th>
              ))}
              <th className={styles.totalCol}>Year</th>
            </tr>
          </thead>
          <tbody>
            {years.map((year) => {
              const row = grid[year] || {};
              return (
                <tr key={year}>
                  <td className={styles.yearCell}>{year}</td>
                  {MONTH_KEYS.map((mk) => {
                    const val = row[mk];
                    return (
                      <td
                        key={mk}
                        className={`${styles.cell} ${cellClass(val)}`}
                        style={cellBgStyle(val, maxAbs)}
                      >
                        {formatReturn(val)}
                      </td>
                    );
                  })}
                  <td
                    className={`${styles.cell} ${styles.totalCell} ${cellClass(row.total)}`}
                    style={cellBgStyle(row.total, maxAbs)}
                  >
                    {formatReturn(row.total)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
