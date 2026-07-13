import { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Sidebar from './components/layout/Sidebar';
import PageContainer from './components/layout/PageContainer';
import ErrorBoundary from './components/ErrorBoundary';
import BackendBanner from './components/BackendBanner';
import HelpPage from './pages/Help/HelpPage';
import DataPage from './pages/Data/DataPage';
import IndicatorsPage from './pages/Indicators/IndicatorsPage';
import SignalsPage from './pages/Signals/SignalsPage';
import PortfolioPage from './pages/Portfolio/PortfolioPage';
import SettingsPage from './pages/Settings/SettingsPage';
import RunningSignalsPage from './pages/RunningSignals/RunningSignalsPage';
import MongoDBAgentPage from './pages/MongoDBAgent/MongoDBAgentPage';
import TicketsPage from './pages/Tickets/TicketsPage';
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
        {/* Desktop-only: warns when the auto-spawned backend is unreachable and
            links to Settings. No-op (renders null) in the web build. */}
        <BackendBanner />
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
            {/* Distinct ``key`` per route so React REMOUNTS PortfolioPage on a
                pure↔composed switch. Both routes render the same component type
                at the same tree position, so without a key React reconciles them
                as ONE instance and usePortfolio's state (legs, persistedId,
                persistedLocked, name, results…) leaks across the switch — the
                carried-over persistedLocked also disabled the holdings fieldset,
                which is why the leaked legs couldn't be removed. */}
            <Route
              path="/portfolio"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <PortfolioPage key="pure" />
                  </ErrorBoundary>
                </PageContainer>
              }
            />
            {/* Composed portfolios — the SAME page component behind the
                ``mode="composed"`` capability flag (Sign 5: shared components,
                no duplicated page). */}
            <Route
              path="/composed-portfolios"
              element={
                <PageContainer>
                  <ErrorBoundary>
                    <PortfolioPage key="composed" mode="composed" />
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
            <Route
              path="/running-signals"
              element={
                <PageContainer>
                  <RunningSignalsPage />
                </PageContainer>
              }
            />
            <Route
              path="/mongodb-agent"
              element={
                <PageContainer>
                  <MongoDBAgentPage />
                </PageContainer>
              }
            />
            <Route
              path="/tickets"
              element={
                <PageContainer>
                  <TicketsPage />
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
