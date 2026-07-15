import { useState, useEffect } from 'react';
import { getVersion } from '@tauri-apps/api/app';
import Icon from '../../components/Icon';
import RiskFreeRateInput from '../../components/RiskFreeRateInput';
import DatabaseSettings from './DatabaseSettings';
import { isTauri } from '../../api/base';
import styles from './SettingsPage.module.css';
import {
  DEFAULT_RISK_FREE_RATE_PCT,
  PORTFOLIO_CACHE_KEY,
  isPortfolioCacheEnabled,
} from '../../lib/userSettings';
import { clearPortfolioCache } from '../../api/portfolio';

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

function getStoredSlippageBps() {
  try {
    return localStorage.getItem('tcg-slippage-bps') || '0';
  } catch {
    return '0';
  }
}

function getStoredFeesBps() {
  try {
    return localStorage.getItem('tcg-fees-bps') || '0';
  } catch {
    return '0';
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
  // Global execution-cost pair, stored in basis points. Sent per-run on both
  // signal and portfolio compute calls (backend converts bps→rate).
  const [slippageBps, setSlippageBps] = useState(getStoredSlippageBps);
  const [feesBps, setFeesBps] = useState(getStoredFeesBps);
  // Portfolio-result cache toggle — DEFAULT ON. Persisted to localStorage;
  // read by usePortfolio at mount and sent as ``use_cache`` on compute.
  const [portfolioCache, setPortfolioCache] = useState(isPortfolioCacheEnabled);
  const [cacheCleared, setCacheCleared] = useState(false);
  const [cacheClearError, setCacheClearError] = useState(false);
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

  function handleSlippageChange(value) {
    setSlippageBps(value);
    const bps = parseFloat(value);
    if (!Number.isFinite(bps) || bps < 0) return;
    try {
      localStorage.setItem('tcg-slippage-bps', value);
    } catch {
      // localStorage unavailable — ignore
    }
  }

  function handleFeesChange(value) {
    setFeesBps(value);
    const bps = parseFloat(value);
    if (!Number.isFinite(bps) || bps < 0) return;
    try {
      localStorage.setItem('tcg-fees-bps', value);
    } catch {
      // localStorage unavailable — ignore
    }
  }

  function handlePortfolioCacheToggle(next) {
    setPortfolioCache(next);
    setCacheCleared(false);
    setCacheClearError(false);
    try {
      localStorage.setItem(PORTFOLIO_CACHE_KEY, String(next));
    } catch {
      // localStorage unavailable — ignore
    }
  }

  function handleClearCache() {
    setCacheCleared(false);
    setCacheClearError(false);
    // Clear the BACKEND cache, then acknowledge in the UI.
    Promise.resolve(clearPortfolioCache())
      .then(() => setCacheCleared(true))
      .catch(() => setCacheClearError(true));
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
        <span className={styles.settingLabel}>Slippage</span>
        <div>
          <RiskFreeRateInput
            valuePct={slippageBps}
            onChange={(e) => handleSlippageChange(e.target.value)}
            ariaLabel="Slippage (basis points)"
            unit="bps"
            step="0.1"
          />
          <div className={styles.settingHint}>
            Applied per trade on signal and portfolio backtests. 1 bp = 0.01%.
          </div>
        </div>
      </div>

      <div className={styles.settingRow}>
        <span className={styles.settingLabel}>Fees</span>
        <div>
          <RiskFreeRateInput
            valuePct={feesBps}
            onChange={(e) => handleFeesChange(e.target.value)}
            ariaLabel="Fees (basis points)"
            unit="bps"
            step="0.1"
          />
          <div className={styles.settingHint}>
            Applied per trade on signal and portfolio backtests. 1 bp = 0.01%.
          </div>
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
            When on, compute reuses the backend&apos;s stored result for an
            identical portfolio and range; editing anything recomputes. Turn off
            to always recompute fresh.
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
            {cacheClearError ? <span data-testid="cache-clear-error"> — failed</span> : null}
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
