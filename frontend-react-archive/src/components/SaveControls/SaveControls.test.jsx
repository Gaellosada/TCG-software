// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import SaveControls from './SaveControls';

afterEach(() => {
  cleanup();
});

describe('<SaveControls>', () => {
  let props;
  beforeEach(() => {
    props = {
      dirty: true,
      autosave: false,
      onSave: vi.fn(),
      onToggleAutosave: vi.fn(),
    };
  });

  it('renders Save button and Auto save checkbox', () => {
    render(<SaveControls {...props} />);
    expect(screen.getByRole('button', { name: /save/i })).toBeTruthy();
    expect(screen.getByLabelText(/auto save/i)).toBeTruthy();
  });

  it('invokes onSave when Save clicked', () => {
    render(<SaveControls {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(props.onSave).toHaveBeenCalledOnce();
  });

  it('disables Save when !dirty', () => {
    render(<SaveControls {...props} dirty={false} />);
    const btn = screen.getByRole('button', { name: /save/i });
    expect(btn.disabled).toBe(true);
  });

  it('disables Save when saveDisabled', () => {
    render(<SaveControls {...props} saveDisabled />);
    const btn = screen.getByRole('button', { name: /save/i });
    expect(btn.disabled).toBe(true);
  });

  it('toggles autosave via the checkbox', () => {
    render(<SaveControls {...props} />);
    const cb = screen.getByLabelText(/auto save/i);
    fireEvent.click(cb);
    expect(props.onToggleAutosave).toHaveBeenCalledWith(true);
  });

  it('shows savedAtLabel when !dirty and a label is provided', () => {
    render(<SaveControls {...props} dirty={false} savedAtLabel="saved 3s ago" />);
    expect(screen.getByText(/saved 3s ago/i)).toBeTruthy();
  });

  it('hides savedAtLabel when dirty', () => {
    render(<SaveControls {...props} dirty savedAtLabel="saved 3s ago" />);
    expect(screen.queryByText(/saved 3s ago/i)).toBeNull();
  });

  it('shows "Unsaved changes" when dirty and autosave off', () => {
    render(<SaveControls {...props} dirty autosave={false} />);
    expect(screen.getByText(/unsaved changes/i)).toBeTruthy();
  });

  it('does not show "Unsaved changes" when autosave is on', () => {
    render(<SaveControls {...props} dirty autosave />);
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
  });

  it('reflects autosave=true on the checkbox', () => {
    render(<SaveControls {...props} autosave />);
    const cb = screen.getByLabelText(/auto save/i);
    expect(cb.checked).toBe(true);
  });

  it('renders an optional leftSlot before the Save button', () => {
    render(
      <SaveControls {...props} leftSlot={<input aria-label="name" defaultValue="SMA" />} />,
    );
    expect(screen.getByLabelText('name')).toBeTruthy();
    expect(screen.getByRole('button', { name: /save/i })).toBeTruthy();
  });
});
