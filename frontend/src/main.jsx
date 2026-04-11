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
  document.documentElement.dataset.chartType =
    localStorage.getItem('tcg-default-chart-type') || 'candlestick';
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
