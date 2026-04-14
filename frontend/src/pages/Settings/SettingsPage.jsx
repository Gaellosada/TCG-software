import { useState, useEffect } from 'react';
import Icon from '../../components/Icon';
import useProviderPreference from '../../hooks/useProviderPreference';
import styles from './SettingsPage.module.css';

const PROVIDER_COLLECTION_TYPES = [
  { key: 'INDEX', label: 'Index', providers: ['YAHOO', 'BLOOMBERG', 'IVOLATILITY'] },
  { key: 'ETF', label: 'ETF', providers: ['YAHOO', 'BLOOMBERG'] },
  { key: 'FUND', label: 'Fund', providers: ['BLOOMBERG', 'YAHOO'] },
  { key: 'FOREX', label: 'Forex', providers: ['YAHOO', 'BITSTAMP', 'COINGECKO'] },
  { key: 'FUT_', label: 'Futures', providers: ['IVOLATILITY', 'DERIBIT', 'BLOOMBERG'] },
  { key: 'OPT_', label: 'Options', providers: ['IVOLATILITY', 'DERIBIT', 'CBOE'] },
];

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
  const { getDefault, setDefault } = useProviderPreference();

  // Build provider defaults state from localStorage
  const [providerDefaults, setProviderDefaults] = useState(() => {
    const defaults = {};
    for (const { key } of PROVIDER_COLLECTION_TYPES) {
      defaults[key] = getDefault(key) || '';
    }
    return defaults;
  });

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

  function handleProviderChange(collectionKey, provider) {
    setProviderDefaults(prev => ({ ...prev, [collectionKey]: provider }));
    setDefault(collectionKey, provider || '');
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

      <h2 className={styles.sectionTitle}>Default providers</h2>
      {PROVIDER_COLLECTION_TYPES.map(({ key, label, providers }) => (
        <div key={key} className={styles.settingRow}>
          <span className={styles.settingLabel}>{label}</span>
          <select
            className={styles.providerSelect}
            value={providerDefaults[key] || ''}
            onChange={(e) => handleProviderChange(key, e.target.value)}
            aria-label={`Default provider for ${label}`}
          >
            <option value="">Auto</option>
            {providers.map(p => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </div>
      ))}
    </div>
  );
}

export default SettingsPage;
