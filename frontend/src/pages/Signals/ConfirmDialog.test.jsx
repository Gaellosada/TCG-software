// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import ConfirmDialog from './ConfirmDialog';

describe('<ConfirmDialog>', () => {
  it('renders nothing when open=false', () => {
    render(
      <ConfirmDialog open={false} onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  it('renders the title + message + action buttons when open', () => {
    render(
      <ConfirmDialog
        open
        title="Delete?"
        message="Really?"
        confirmLabel="Yes"
        cancelLabel="No"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    expect(screen.getByText('Delete?')).toBeDefined();
    expect(screen.getByText('Really?')).toBeDefined();
    expect(screen.getByTestId('confirm-dialog-confirm').textContent).toBe('Yes');
    expect(screen.getByTestId('confirm-dialog-cancel').textContent).toBe('No');
  });

  it('clicking Confirm calls onConfirm', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="T" message="M" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('clicking Cancel calls onCancel', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="T" message="M" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('Escape key cancels', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="T" message="M" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Enter key confirms', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="T" message="M" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('backdrop click cancels', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog open title="T" message="M" onConfirm={onConfirm} onCancel={onCancel} />,
    );
    const backdrop = screen.getByTestId('confirm-dialog-backdrop');
    fireEvent.mouseDown(backdrop, { target: backdrop, currentTarget: backdrop });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
