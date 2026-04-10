import { Routes, Route, Navigate } from 'react-router-dom';
import Sidebar from './components/layout/Sidebar';
import PageContainer from './components/layout/PageContainer';
import HelpPage from './pages/Help/HelpPage';
import DataPage from './pages/Data/DataPage';
import PortfolioPage from './pages/Portfolio/PortfolioPage';
import ResearchPage from './pages/Research/ResearchPage';
import SavedStrategiesPage from './pages/SavedStrategies/SavedStrategiesPage';
import './App.css';

function App() {
  return (
    <div className="app-layout">
      <Sidebar />
      <main className="app-content">
        <Routes>
          <Route path="/" element={<Navigate to="/help" replace />} />
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
            path="/saved-strategies"
            element={
              <PageContainer>
                <SavedStrategiesPage />
              </PageContainer>
            }
          />
        </Routes>
      </main>
    </div>
  );
}

export default App;
