// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ChatPanel from './ChatPanel';

// Mock renderMarkdown to avoid needing full markdown deps in tests
vi.mock('./renderMarkdown', () => ({
  default: vi.fn((text) => text || ''),
}));

afterEach(cleanup);

function defaultProps(overrides = {}) {
  return {
    messages: [],
    isConnected: true,
    sendMessage: vi.fn(),
    isStreaming: false,
    ...overrides,
  };
}

describe('<ChatPanel>', () => {
  it('shows empty state when no messages', () => {
    render(<ChatPanel {...defaultProps()} />);
    expect(screen.getByText('Start a conversation...')).toBeTruthy();
  });

  it('renders user messages', () => {
    const messages = [{ role: 'user', content: 'Hello agent' }];
    render(<ChatPanel {...defaultProps({ messages })} />);
    expect(screen.getByText('Hello agent')).toBeTruthy();
  });

  it('renders assistant messages with markdown', () => {
    const messages = [{ role: 'assistant', content: 'Here is your answer' }];
    render(<ChatPanel {...defaultProps({ messages })} />);
    expect(screen.getByText('Here is your answer')).toBeTruthy();
  });

  it('renders error messages', () => {
    const messages = [{ role: 'error', content: 'Something failed' }];
    render(<ChatPanel {...defaultProps({ messages })} />);
    expect(screen.getByText('Something failed')).toBeTruthy();
    expect(screen.getByText('Error')).toBeTruthy();
  });

  it('renders tool messages with expand/collapse', () => {
    const messages = [{ role: 'tool', name: 'run_query', input: { query: '{}' } }];
    const { container } = render(<ChatPanel {...defaultProps({ messages })} />);
    expect(screen.getByText('run_query')).toBeTruthy();

    // Click to expand
    fireEvent.click(screen.getByText('run_query'));
    // The input is rendered in a <pre> with JSON.stringify(input, null, 2)
    const pre = container.querySelector('pre');
    expect(pre).toBeTruthy();
    expect(pre.textContent).toContain('"query"');
  });

  it('sends message on Enter key', () => {
    const sendMessage = vi.fn();
    render(<ChatPanel {...defaultProps({ sendMessage })} />);
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'test message' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    expect(sendMessage).toHaveBeenCalledWith('test message');
  });

  it('does not send on Shift+Enter (allows newline)', () => {
    const sendMessage = vi.fn();
    render(<ChatPanel {...defaultProps({ sendMessage })} />);
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'test message' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it('does not send empty messages', () => {
    const sendMessage = vi.fn();
    render(<ChatPanel {...defaultProps({ sendMessage })} />);
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: '   ' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it('disables textarea when disconnected', () => {
    render(<ChatPanel {...defaultProps({ isConnected: false })} />);
    const textarea = screen.getByRole('textbox');
    expect(textarea.disabled).toBe(true);
  });

  it('disables send button when streaming', () => {
    render(<ChatPanel {...defaultProps({ isStreaming: true })} />);
    const sendBtn = screen.getByRole('button', { name: /send message/i });
    expect(sendBtn.disabled).toBe(true);
  });

  it('shows streaming cursor on last message when streaming', () => {
    const messages = [{ role: 'assistant', content: 'Thinking...', streaming: true }];
    const { container } = render(<ChatPanel {...defaultProps({ messages, isStreaming: true })} />);
    // The cursor span should exist (CSS class-based)
    const cursors = container.querySelectorAll('[class*="cursor"]');
    expect(cursors.length).toBeGreaterThan(0);
  });

  it('shows connection dot with connected title', () => {
    const { container } = render(<ChatPanel {...defaultProps({ isConnected: true })} />);
    const dot = container.querySelector('[title="Connected"]');
    expect(dot).toBeTruthy();
  });

  it('shows connection dot with disconnected title', () => {
    const { container } = render(<ChatPanel {...defaultProps({ isConnected: false })} />);
    const dot = container.querySelector('[title="Disconnected"]');
    expect(dot).toBeTruthy();
  });
});
