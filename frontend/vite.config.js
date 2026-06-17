import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backendPort = parseInt(process.env.TCG_BACKEND_PORT || '8000');
const frontendPort = parseInt(process.env.TCG_FRONTEND_PORT || '5173');

export default defineConfig({
  plugins: [react()],
  server: {
    port: frontendPort,
    proxy: {
      '/api': `http://localhost:${backendPort}`,
    },
  },
  test: {
    include: ['src/**/*.test.{js,jsx,ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**'],
    environment: 'jsdom',
    // Auto-wraps every RTL render in a QueryClientProvider (see the file) so
    // components migrated to TanStack Query render without per-test boilerplate.
    setupFiles: ['./src/test/setup.js'],
  },
});
