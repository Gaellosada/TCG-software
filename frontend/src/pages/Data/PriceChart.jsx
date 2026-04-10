import Plot from 'react-plotly.js';
import useAsync from '../../hooks/useAsync';
import { getInstrumentPrices } from '../../api/data';
import styles from './PriceChart.module.css';

/**
 * Convert YYYYMMDD integer to ISO date string for Plotly.
 * @param {number} dateInt - e.g. 20240101
 * @returns {string} - e.g. "2024-01-01"
 */
function formatDateInt(dateInt) {
  const s = String(dateInt);
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

function PriceChart({ collection, instrument }) {
  const { data, loading, error } = useAsync(
    () => getInstrumentPrices(collection, instrument),
    [collection, instrument]
  );

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.loading}>Loading price data...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>
          Failed to load prices: {error.message}
        </div>
      </div>
    );
  }

  if (!data || !data.dates || data.dates.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.loading}>No price data available.</div>
      </div>
    );
  }

  const dates = data.dates.map(formatDateInt);
  const closePrices = data.close;

  return (
    <div className={styles.container}>
      <div className={styles.chartWrapper}>
        <Plot
          data={[
            {
              x: dates,
              y: closePrices,
              type: 'scatter',
              mode: 'lines',
              line: { color: '#3b82f6', width: 1.5 },
              hovertemplate: '%{x}<br>Close: %{y:.2f}<extra></extra>',
            },
          ]}
          layout={{
            autosize: true,
            title: {
              text: instrument,
              font: { size: 16, color: '#2c3040' },
            },
            xaxis: {
              type: 'date',
              gridcolor: '#f0f1f5',
              linecolor: '#e0e2e8',
            },
            yaxis: {
              title: { text: 'Close Price' },
              gridcolor: '#f0f1f5',
              linecolor: '#e0e2e8',
            },
            margin: { t: 48, r: 24, b: 48, l: 56 },
            paper_bgcolor: '#ffffff',
            plot_bgcolor: '#ffffff',
            dragmode: 'zoom',
            hovermode: 'x unified',
          }}
          config={{
            displayModeBar: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['lasso2d', 'select2d'],
            responsive: true,
          }}
          useResizeHandler={true}
          style={{ width: '100%', height: '100%' }}
        />
      </div>
    </div>
  );
}

export default PriceChart;
