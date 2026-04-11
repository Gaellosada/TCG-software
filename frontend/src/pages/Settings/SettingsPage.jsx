import { useState, useEffect } from 'react';
import Icon from '../../components/Icon';
import styles from './SettingsPage.module.css';

function getStoredTheme() {
  try {
    return localStorage.getItem('tcg-theme') || 'dark';
  } catch {
    return 'dark';
  }
}

function getStoredChartType() {
  try {
    return localStorage.getItem('tcg-default-chart-type') || 'candlestick';
  } catch {
    return 'candlestick';
  }
}

function applyChartType(type) {
  document.documentElement.dataset.chartType = type;
}

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.dataset.theme = 'light';
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function SettingsPage() {
  const [theme, setTheme] = useState(getStoredTheme);
  const [chartType, setChartType] = useState(getStoredChartType);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    applyChartType(chartType);
  }, [chartType]);

  function handleThemeChange(newTheme) {
    setTheme(newTheme);
    try {
      localStorage.setItem('tcg-theme', newTheme);
    } catch {
      // localStorage unavailable — ignore
    }
  }

  function handleChartTypeChange(newType) {
    setChartType(newType);
    try {
      localStorage.setItem('tcg-default-chart-type', newType);
    } catch {
      // localStorage unavailable — ignore
    }
  }

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Settings</h1>
      <p className={styles.description}>Configure your workspace preferences.</p>

      <div className={styles.card}>
        <h2 className={styles.cardTitle}>Appearance</h2>
        <p className={styles.cardDescription}>Choose your preferred color theme.</p>
        <div className={styles.themeButtons}>
          <button
            className={`${styles.themeBtn} ${theme === 'dark' ? styles.themeBtnActive : ''}`}
            onClick={() => handleThemeChange('dark')}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Icon name="moon" size={14} />
              Dark
            </span>
          </button>
          <button
            className={`${styles.themeBtn} ${theme === 'light' ? styles.themeBtnActive : ''}`}
            onClick={() => handleThemeChange('light')}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Icon name="sun" size={14} />
              Light
            </span>
          </button>
        </div>
      </div>

      <div className={styles.card}>
        <h2 className={styles.cardTitle}>Charts</h2>
        <p className={styles.cardDescription}>
          Default chart style for price data. Instruments without OHLC data always use line charts.
        </p>
        <div className={styles.themeButtons}>
          <button
            className={`${styles.themeBtn} ${chartType === 'candlestick' ? styles.themeBtnActive : ''}`}
            onClick={() => handleChartTypeChange('candlestick')}
          >
            Candlestick
          </button>
          <button
            className={`${styles.themeBtn} ${chartType === 'line' ? styles.themeBtnActive : ''}`}
            onClick={() => handleChartTypeChange('line')}
          >
            Line
          </button>
        </div>
      </div>
    </div>
  );
}

export default SettingsPage;
