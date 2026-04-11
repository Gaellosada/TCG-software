import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

// Apply persisted preferences before first render to avoid flash
try {
  const storedTheme = localStorage.getItem('tcg-theme');
  if (storedTheme === 'light') {
    document.documentElement.dataset.theme = 'light';
  }
  const storedChart = localStorage.getItem('tcg-default-chart-type');
  if (storedChart) {
    document.documentElement.dataset.chartType = storedChart;
  }
} catch {
  // localStorage unavailable — defaults apply
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
