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

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.dataset.theme = 'light';
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function SettingsPage() {
  const [theme, setTheme] = useState(getStoredTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  function handleThemeChange(newTheme) {
    setTheme(newTheme);
    try {
      localStorage.setItem('tcg-theme', newTheme);
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
    </div>
  );
}

export default SettingsPage;
