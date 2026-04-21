import { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Sidebar from './components/layout/Sidebar';
import PageContainer from './components/layout/PageContainer';
import ErrorBoundary from './components/ErrorBoundary';
import HelpPage from './pages/Help/HelpPage';
import DataPage from './pages/Data/DataPage';
import IndicatorsPage from './pages/Indicators/IndicatorsPage';
import SignalsPage from './pages/Signals/SignalsPage';
import PortfolioPage from './pages/Portfolio/PortfolioPage';
import SettingsPage from './pages/Settings/SettingsPage';
import './App.css';

function App() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem('tcg-sidebar-collapsed') === 'true'
  );

  function toggleSidebar() {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    localStorage.setItem('tcg-sidebar-collapsed', String(next));
    // Trigger resize for Plotly charts after CSS transition
    setTimeout(() => window.dispatchEvent(new Event('resize')), 260);
  }

  return (
    <div className="app-layout">
      <Sidebar collapsed={sidebarCollapsed} onToggle={toggleSidebar} />
      <main
        className="app-content"
        style={{ marginLeft: sidebarCollapsed ? 'var(--sidebar-width-collapsed)' : undefined }}
      >
        <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Navigate to="/data" replace />} />
            <Route
              path="/help"
              element={
                <PageContainer>
                  <HelpPage />
                </PageContainer>
              }
            />
            <Route
              path="/data"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <DataPage />
                  </ErrorBoundary>
                </PageContainer>
              }
            />
            <Route
              path="/indicators"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <IndicatorsPage />
                  </ErrorBoundary>
                </PageContainer>
              }
            />
            <Route
              path="/signals"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <SignalsPage />
                  </ErrorBoundary>
                </PageContainer>
              }
            />
            <Route
              path="/portfolio"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <PortfolioPage />
                  </ErrorBoundary>
                </PageContainer>
              }
            />
            <Route
              path="/settings"
              element={
                <PageContainer>
                  <SettingsPage />
                </PageContainer>
              }
            />
          </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}

export default App;
