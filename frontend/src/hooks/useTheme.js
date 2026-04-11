import { useState, useEffect } from 'react';

/**
 * Shared hook that tracks the current theme ('dark' | 'light').
 * Observes the data-theme attribute on <html> via MutationObserver
 * so Plotly charts (which can't read CSS variables) stay in sync.
 */
export default function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme || 'dark'
  );

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setTheme(document.documentElement.dataset.theme || 'dark');
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => observer.disconnect();
  }, []);

  return theme;
}
