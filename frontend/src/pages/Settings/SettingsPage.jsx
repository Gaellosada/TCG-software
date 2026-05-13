import { useState, useEffect } from 'react';
import Icon from '../../components/Icon';
import RiskFreeRateInput from '../../components/RiskFreeRateInput';
import styles from './SettingsPage.module.css';
import { DEFAULT_RISK_FREE_RATE_PCT } from '../../lib/userSettings';

function getStoredTheme() {
  try {
    return localStorage.getItem('tcg-theme') || 'light';
  } catch {
    return 'light';
  }
}

function getStoredChartType() {
  try {
    return localStorage.getItem('tcg-default-chart-type') || 'line';
  } catch {
    return 'line';
  }
}

function getStoredRiskFreeRate() {
  try {
    return localStorage.getItem('tcg-risk-free-rate') || DEFAULT_RISK_FREE_RATE_PCT.toFixed(2);
  } catch {
    return DEFAULT_RISK_FREE_RATE_PCT.toFixed(2);
  }
}

function applyChartType(type) {
  document.documentElement.dataset.chartType = type;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
}

function SettingsPage() {
  const [theme, setTheme] = useState(getStoredTheme);
  const [chartType, setChartType] = useState(getStoredChartType);
  const [rfPct, setRfPct] = useState(getStoredRiskFreeRate);

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

  function handleRfChange(value) {
    setRfPct(value);
    const pct = parseFloat(value);
    if (!Number.isFinite(pct) || pct < 0) return;
    try {
      localStorage.setItem('tcg-risk-free-rate', value);
    } catch {
      // localStorage unavailable — ignore
    }
  }

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Settings</h1>

      <div className={styles.settingRow}>
        <span className={styles.settingLabel}>Theme</span>
        <div className={styles.buttonGroup} role="radiogroup" aria-label="Theme">
          <button
            role="radio"
            aria-checked={theme === 'dark'}
            className={`${styles.optionBtn} ${theme === 'dark' ? styles.optionBtnActive : ''}`}
            onClick={() => handleThemeChange('dark')}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Icon name="moon" size={14} />
              Dark
            </span>
          </button>
          <button
            role="radio"
            aria-checked={theme === 'light'}
            className={`${styles.optionBtn} ${theme === 'light' ? styles.optionBtnActive : ''}`}
            onClick={() => handleThemeChange('light')}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Icon name="sun" size={14} />
              Light
            </span>
          </button>
        </div>
      </div>

      <div className={styles.settingRow}>
        <span className={styles.settingLabel}>Default chart</span>
        <div className={styles.buttonGroup} role="radiogroup" aria-label="Default chart type">
          <button
            role="radio"
            aria-checked={chartType === 'candlestick'}
            className={`${styles.optionBtn} ${chartType === 'candlestick' ? styles.optionBtnActive : ''}`}
            onClick={() => handleChartTypeChange('candlestick')}
          >
            Candlestick
          </button>
          <button
            role="radio"
            aria-checked={chartType === 'line'}
            className={`${styles.optionBtn} ${chartType === 'line' ? styles.optionBtnActive : ''}`}
            onClick={() => handleChartTypeChange('line')}
          >
            Line
          </button>
        </div>
      </div>

      <div className={styles.settingRow}>
        <span className={styles.settingLabel}>Default risk-free rate</span>
        <div>
          <RiskFreeRateInput
            valuePct={rfPct}
            onChange={(e) => handleRfChange(e.target.value)}
            ariaLabel="Default risk-free rate (percent)"
          />
          <div className={styles.settingHint}>Used for Sharpe, Sortino, and Calmar ratios.</div>
        </div>
      </div>
    </div>
  );
}

export default SettingsPage;
