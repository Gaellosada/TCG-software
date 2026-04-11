import { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Sidebar from './components/layout/Sidebar';
import PageContainer from './components/layout/PageContainer';
import HelpPage from './pages/Help/HelpPage';
import DataPage from './pages/Data/DataPage';
import PortfolioPage from './pages/Portfolio/PortfolioPage';
import ResearchPage from './pages/Research/ResearchPage';
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
                <DataPage />
              </PageContainer>
            }
          />
          <Route
            path="/portfolio"
            element={
              <PageContainer>
                <PortfolioPage />
              </PageContainer>
            }
          />
          <Route
            path="/research"
            element={
              <PageContainer>
                <ResearchPage />
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
      </main>
    </div>
  );
}

export default App;
