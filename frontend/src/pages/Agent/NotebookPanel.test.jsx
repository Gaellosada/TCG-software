// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import NotebookPanel from './NotebookPanel';

// Mock react-plotly.js to avoid pulling in the full plotly bundle
vi.mock('react-plotly.js', () => ({
  default: vi.fn((props) => <div data-testid="plotly-mock">Plotly</div>),
}));

// Mock renderMarkdown
vi.mock('./renderMarkdown', () => ({
  default: vi.fn((text) => text || ''),
}));

// Mock the agent API
vi.mock('../../api/agent', () => ({
  getNotebook: vi.fn(),
}));

import { getNotebook } from '../../api/agent';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('<NotebookPanel>', () => {
  it('shows "Select a session" when sessionId is null', () => {
    render(<NotebookPanel sessionId={null} notebookReady={false} />);
    expect(screen.getByText(/select a session/i)).toBeTruthy();
  });

  it('shows "Pending..." when session is set but notebookReady is false', () => {
    render(<NotebookPanel sessionId="sess-1" notebookReady={false} />);
    expect(screen.getByText('Pending...')).toBeTruthy();
  });

  it('fetches and renders code cells when notebookReady is true', async () => {
    getNotebook.mockResolvedValue({
      cells: [
        {
          cell_type: 'code',
          source: 'print("hello")',
          execution_count: 1,
          outputs: [
            { output_type: 'stream', text: 'hello\n' },
          ],
        },
      ],
    });

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText('print("hello")')).toBeTruthy();
    });
    // Stream output rendered in a <pre> — use getAllByText since "hello" appears
    // in both the source and the output
    const helloElements = screen.getAllByText(/hello/);
    expect(helloElements.length).toBeGreaterThanOrEqual(2); // source + output
    // Execution count: rendered as "[1]:" with possible whitespace
    expect(screen.getByText(/\[1\]:/)).toBeTruthy();
  });

  it('renders markdown cells', async () => {
    getNotebook.mockResolvedValue({
      cells: [
        {
          cell_type: 'markdown',
          source: '# Analysis Results',
        },
      ],
    });

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText('# Analysis Results')).toBeTruthy();
    });
  });

  it('shows error state on fetch failure', async () => {
    getNotebook.mockRejectedValue(new Error('Not found'));

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText('Not found')).toBeTruthy();
    });
  });

  it('renders error outputs from code cells', async () => {
    getNotebook.mockResolvedValue({
      cells: [
        {
          cell_type: 'code',
          source: '1/0',
          execution_count: 2,
          outputs: [
            {
              output_type: 'error',
              ename: 'ZeroDivisionError',
              evalue: 'division by zero',
              traceback: [],
            },
          ],
        },
      ],
    });

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText(/ZeroDivisionError.*division by zero/)).toBeTruthy();
    });
  });

  it('renders plotly outputs', async () => {
    getNotebook.mockResolvedValue({
      cells: [
        {
          cell_type: 'code',
          source: 'fig.show()',
          execution_count: 3,
          outputs: [
            {
              output_type: 'display_data',
              data: {
                'application/vnd.plotly.v1+json': {
                  data: [{ x: [1, 2], y: [3, 4], type: 'scatter' }],
                  layout: {},
                },
              },
            },
          ],
        },
      ],
    });

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('plotly-mock')).toBeTruthy();
    });
  });

  it('shows Refresh button after notebook loads', async () => {
    getNotebook.mockResolvedValue({ cells: [] });

    render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText('Refresh')).toBeTruthy();
    });
  });

  it('resets notebook state when sessionId changes', async () => {
    getNotebook.mockResolvedValue({
      cells: [{ cell_type: 'code', source: 'x=1', execution_count: 1, outputs: [] }],
    });

    const { rerender } = render(<NotebookPanel sessionId="sess-1" notebookReady={true} />);

    await waitFor(() => {
      expect(screen.getByText('x=1')).toBeTruthy();
    });

    // Change session — should reset to pending since notebookReady is still bound
    // to old session's state
    rerender(<NotebookPanel sessionId="sess-2" notebookReady={false} />);
    expect(screen.getByText('Pending...')).toBeTruthy();
  });
});
