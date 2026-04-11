import { useState, useEffect } from 'react';

/**
 * Shared hook that tracks the default chart type ('candlestick' | 'line').
 * Observes the data-chart-type attribute on <html> via MutationObserver
 * so all chart components stay in sync when the setting changes.
 */
export default function useChartPreference() {
  const [chartType, setChartType] = useState(
    () => document.documentElement.dataset.chartType || 'candlestick'
  );

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setChartType(document.documentElement.dataset.chartType || 'candlestick');
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-chart-type'],
    });
    return () => observer.disconnect();
  }, []);

  return chartType;
}
