import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';

// Apply persisted theme before first render to avoid flash
try {
  const stored = localStorage.getItem('tcg-theme');
  if (stored === 'light') {
    document.documentElement.dataset.theme = 'light';
  }
} catch {
  // localStorage unavailable — dark default
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
