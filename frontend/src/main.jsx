import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { createQueryClient } from './queryClient';
import './index.css';

// One app-wide client. This is a single-user SPA (no SSR), so a module-level
// client is correct: the cache must persist across route changes — that warm
// cache is exactly what makes re-navigation render instantly with no spinner.
const queryClient = createQueryClient();

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
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
