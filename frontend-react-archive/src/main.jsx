import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

// Apply persisted preferences before first render to avoid flash
try {
  const storedTheme = localStorage.getItem('tcg-theme');
  document.documentElement.dataset.theme = storedTheme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.chartType =
    localStorage.getItem('tcg-default-chart-type') || 'line';
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
