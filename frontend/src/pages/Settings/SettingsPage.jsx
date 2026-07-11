import { useState, useEffect } from 'react';
import { getVersion } from '@tauri-apps/api/app';
import Icon from '../../components/Icon';
import RiskFreeRateInput from '../../components/RiskFreeRateInput';
import DatabaseSettings from './DatabaseSettings';
import { isTauri } from '../../api/base';
import styles from './SettingsPage.module.css';
import { DEFAULT_RISK_FREE_RATE_PCT, PORTFOLIO_CACHE_KEY } from '../../lib/userSettings';
import { clearCache } from '../../lib/portfolioCache';

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

function getStoredPortfolioCache() {
  try {
    return localStorage.getItem(PORTFOLIO_CACHE_KEY) === 'true';
  } catch {
    return false;
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
  // Local portfolio-result cache — opt-in, default OFF. Persisted to
  // localStorage; read by usePortfolio at mount.
  const [portfolioCache, setPortfolioCache] = useState(getStoredPortfolioCache);
  const [cacheCleared, setCacheCleared] = useState(false);
  // Desktop-only: the app version (from tauri.conf.json) shown in a small
  // footer. Empty in web mode so nothing renders there.
  const [appVersion, setAppVersion] = useState('');

  useEffect(() => {
    if (!isTauri()) return undefined;
    let cancelled = false;
    getVersion()
      .then((v) => {
        if (!cancelled) setAppVersion(v);
      })
      .catch(() => {
        // Version is purely informational — ignore failures.
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

  function handlePortfolioCacheToggle(next) {
    setPortfolioCache(next);
    setCacheCleared(false);
    try {
      localStorage.setItem(PORTFOLIO_CACHE_KEY, String(next));
    } catch {
      // localStorage unavailable — ignore
    }
  }

  function handleClearCache() {
    // Best-effort; clearCache never throws. Reflect the action in the UI.
    Promise.resolve(clearCache()).finally(() => setCacheCleared(true));
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

      <div className={styles.settingRow}>
        <span className={styles.settingLabel}>Cache portfolio results</span>
        <div>
          <div
            className={styles.buttonGroup}
            role="radiogroup"
            aria-label="Cache portfolio results"
            data-testid="portfolio-cache-toggle"
          >
            <button
              role="radio"
              aria-checked={portfolioCache}
              data-testid="portfolio-cache-on"
              className={`${styles.optionBtn} ${portfolioCache ? styles.optionBtnActive : ''}`}
              onClick={() => handlePortfolioCacheToggle(true)}
            >
              On
            </button>
            <button
              role="radio"
              aria-checked={!portfolioCache}
              data-testid="portfolio-cache-off"
              className={`${styles.optionBtn} ${!portfolioCache ? styles.optionBtnActive : ''}`}
              onClick={() => handlePortfolioCacheToggle(false)}
            >
              Off
            </button>
          </div>
          <div className={styles.settingHint}>
            Reuse a portfolio&apos;s last computed result instantly when nothing
            changed. Editing the portfolio, a signal, or an indicator recomputes
            automatically.
            {' '}
            <button
              type="button"
              className={styles.linkBtn}
              onClick={handleClearCache}
              data-testid="clear-cache-btn"
            >
              Clear cached results
            </button>
            {cacheCleared ? <span data-testid="cache-cleared"> — cleared</span> : null}
          </div>
        </div>
      </div>

      {/* Desktop-only: the web build connects via the server-side .env, so the
          credentials editor is shown only inside the Tauri webview. */}
      {isTauri() ? <DatabaseSettings /> : null}

      {/* Desktop-only app version footer (from tauri.conf.json via getVersion). */}
      {appVersion ? (
        <div className={styles.versionFooter} data-testid="app-version">
          Version {appVersion}
        </div>
      ) : null}
    </div>
  );
}

export default SettingsPage;
