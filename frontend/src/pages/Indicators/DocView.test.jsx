// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DocView from './DocView';

afterEach(() => {
  cleanup();
});

describe('<DocView> — readOnly', () => {
  it('does not render an Edit button when readOnly=true', () => {
    render(<DocView value="# Hi" readOnly={true} onChange={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull();
  });

  it('renders the readonly badge and sets data-readonly="true"', () => {
    const { container } = render(
      <DocView value="# Hi" readOnly={true} onChange={vi.fn()} />,
    );
    expect(screen.getByText(/read-only/i)).toBeTruthy();
    const wrapper = container.querySelector('[data-readonly="true"]');
    expect(wrapper).toBeTruthy();
  });

  it('renders markdown content in read mode', () => {
    render(<DocView value="# Hello world" readOnly={true} onChange={vi.fn()} />);
    expect(
      screen.getByRole('heading', { level: 1, name: /hello world/i }),
    ).toBeTruthy();
  });

  it('shows the default readonly placeholder when value is empty', () => {
    render(<DocView value="" readOnly={true} onChange={vi.fn()} />);
    expect(screen.getByTestId('docview-placeholder').textContent).toMatch(
      /no documentation provided/i,
    );
  });
});

describe('<DocView> — editable', () => {
  it('renders the Edit button when readOnly=false', () => {
    render(<DocView value="# Hi" readOnly={false} onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: /edit/i })).toBeTruthy();
  });

  it('shows a custom-friendly placeholder when empty and editable', () => {
    render(<DocView value="" readOnly={false} onChange={vi.fn()} />);
    expect(screen.getByTestId('docview-placeholder').textContent).toMatch(
      /no documentation yet/i,
    );
  });

  it('clicking Edit swaps to a textarea pre-filled with the current value', async () => {
    const user = userEvent.setup();
    render(<DocView value="# Hi" readOnly={false} onChange={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /edit/i }));
    const textarea = screen.getByRole('textbox', { name: /indicator documentation/i });
    expect(textarea).toBeTruthy();
    expect(textarea.value).toBe('# Hi');
  });

  it('commits on blur via onChange when the draft changed', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<DocView value="old" readOnly={false} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: /edit/i }));
    const textarea = screen.getByRole('textbox', { name: /indicator documentation/i });
    await user.clear(textarea);
    await user.type(textarea, 'new');
    // Blur by tabbing away.
    textarea.blur();
    expect(onChange).toHaveBeenCalledWith('new');
  });

  it('does not call onChange on blur when the draft equals the original value', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<DocView value="same" readOnly={false} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: /edit/i }));
    const textarea = screen.getByRole('textbox', { name: /indicator documentation/i });
    textarea.blur();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('Escape cancels without committing', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<DocView value="original" readOnly={false} onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: /edit/i }));
    const textarea = screen.getByRole('textbox', { name: /indicator documentation/i });
    await user.clear(textarea);
    await user.type(textarea, 'dropped');
    await user.keyboard('{Escape}');
    expect(onChange).not.toHaveBeenCalled();
    // Should exit edit mode back to the read view.
    expect(screen.queryByRole('textbox', { name: /indicator documentation/i })).toBeNull();
    // Edit button should reappear.
    expect(screen.getByRole('button', { name: /edit/i })).toBeTruthy();
  });
});
