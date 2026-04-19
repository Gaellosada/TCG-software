// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EditorPanel from './EditorPanel';

afterEach(() => {
  cleanup();
});

function defaultProps(overrides = {}) {
  return {
    code: 'print(1)',
    onCodeChange: vi.fn(),
    doc: '# Hello',
    onDocChange: vi.fn(),
    readOnly: false,
    viewMode: 'code',
    onViewModeChange: vi.fn(),
    ...overrides,
  };
}

describe('<EditorPanel>', () => {
  it('renders a tablist with Code and Documentation tabs', () => {
    render(<EditorPanel {...defaultProps()} />);
    const tablist = screen.getByRole('tablist');
    const tabs = within(tablist).getAllByRole('tab');
    expect(tabs).toHaveLength(2);
    expect(tabs[0].textContent).toMatch(/code/i);
    expect(tabs[1].textContent).toMatch(/documentation/i);
  });

  it('marks the tab matching viewMode as aria-selected', () => {
    const { rerender } = render(<EditorPanel {...defaultProps({ viewMode: 'code' })} />);
    expect(screen.getByRole('tab', { name: /^code$/i }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: /documentation/i }).getAttribute('aria-selected')).toBe('false');

    rerender(<EditorPanel {...defaultProps({ viewMode: 'doc' })} />);
    expect(screen.getByRole('tab', { name: /^code$/i }).getAttribute('aria-selected')).toBe('false');
    expect(screen.getByRole('tab', { name: /documentation/i }).getAttribute('aria-selected')).toBe('true');
  });

  it('calls onViewModeChange when Documentation tab is clicked', async () => {
    const onViewModeChange = vi.fn();
    const user = userEvent.setup();
    render(<EditorPanel {...defaultProps({ onViewModeChange })} />);
    await user.click(screen.getByRole('tab', { name: /documentation/i }));
    expect(onViewModeChange).toHaveBeenCalledWith('doc');
  });

  it('renders the code body when viewMode is code', () => {
    render(<EditorPanel {...defaultProps({ viewMode: 'code' })} />);
    // CodeEditor renders a CodeMirror instance with aria-label="Indicator code"
    expect(screen.getByLabelText(/indicator code/i)).toBeTruthy();
    // Doc read mode (markdown) is not rendered
    expect(screen.queryByLabelText(/indicator documentation/i)).toBeNull();
  });

  it('renders the doc body when viewMode is doc', () => {
    render(<EditorPanel {...defaultProps({ viewMode: 'doc' })} />);
    // Markdown renders an <h1>Hello</h1> for '# Hello'
    expect(screen.getByRole('heading', { level: 1, name: /hello/i })).toBeTruthy();
    // CodeEditor is not rendered
    expect(screen.queryByLabelText(/indicator code/i)).toBeNull();
  });

  it('arrow keys move selection between tabs', async () => {
    const onViewModeChange = vi.fn();
    const user = userEvent.setup();
    render(<EditorPanel {...defaultProps({ viewMode: 'code', onViewModeChange })} />);
    const codeTab = screen.getByRole('tab', { name: /^code$/i });
    codeTab.focus();
    await user.keyboard('{ArrowRight}');
    expect(onViewModeChange).toHaveBeenCalledWith('doc');
    onViewModeChange.mockClear();
    await user.keyboard('{ArrowLeft}');
    expect(onViewModeChange).toHaveBeenCalledWith('doc');
    // Wraps around when pressing ArrowLeft from Code (idx 0 - 1 = last)
  });

  it('remounts DocView when indicatorId changes (drops in-progress draft)', async () => {
    const onDocChange = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <EditorPanel
        {...defaultProps({ viewMode: 'doc', indicatorId: 'a', doc: 'a-doc', onDocChange })}
      />,
    );
    // Enter edit mode and start typing a draft for indicator 'a'.
    await user.click(screen.getByRole('button', { name: /edit/i }));
    const textarea = screen.getByLabelText(/indicator documentation/i);
    await user.clear(textarea);
    await user.type(textarea, 'draft for A');
    // Switch to indicator 'b' mid-edit. DocView should remount (new key) —
    // the draft for 'a' must NOT survive into 'b', and onDocChange must
    // NOT have been called (no silent cross-indicator commit).
    rerender(
      <EditorPanel
        {...defaultProps({ viewMode: 'doc', indicatorId: 'b', doc: 'b-doc', onDocChange })}
      />,
    );
    expect(onDocChange).not.toHaveBeenCalled();
    // Indicator 'b' renders in read mode with its own value; no textarea.
    expect(screen.queryByLabelText(/indicator documentation/i)).toBeNull();
    expect(screen.getByText(/b-doc/)).toBeTruthy();
  });

  it('exposes role="tabpanel" for the body', () => {
    render(<EditorPanel {...defaultProps({ viewMode: 'code' })} />);
    const tabpanel = screen.getByRole('tabpanel');
    expect(tabpanel).toBeTruthy();
    // aria-labelledby should point at the active tab
    const activeTab = screen.getByRole('tab', { name: /^code$/i });
    expect(tabpanel.getAttribute('aria-labelledby')).toBe(activeTab.id);
  });
});
