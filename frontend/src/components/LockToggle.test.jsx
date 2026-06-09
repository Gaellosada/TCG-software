// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import LockToggle from './LockToggle';

afterEach(() => { cleanup(); });

describe('<LockToggle>', () => {
  // -------------------------------------------------------------------------
  // Unlocked state — clicking locks immediately, no dialog
  // -------------------------------------------------------------------------

  it('clicking when unlocked calls onSetLocked(true) immediately', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={false} onSetLocked={onSetLocked} entityLabel="indicator" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(onSetLocked).toHaveBeenCalledTimes(1);
    expect(onSetLocked).toHaveBeenCalledWith(true);
  });

  it('clicking when unlocked does NOT open a confirmation dialog', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={false} onSetLocked={onSetLocked} entityLabel="indicator" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Locked state — clicking opens dialog, does NOT immediately call onSetLocked
  // -------------------------------------------------------------------------

  it('clicking when locked opens the confirmation dialog', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="signal" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
  });

  it('clicking when locked does NOT immediately call onSetLocked', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="signal" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(onSetLocked).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // Dialog confirm → calls onSetLocked(false)
  // -------------------------------------------------------------------------

  it('confirming the dialog calls onSetLocked(false)', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="portfolio" />,
    );
    // Open dialog
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    // Confirm
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    expect(onSetLocked).toHaveBeenCalledTimes(1);
    expect(onSetLocked).toHaveBeenCalledWith(false);
  });

  it('confirming the dialog closes it', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="portfolio" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Dialog cancel → does NOT call onSetLocked; dialog closes
  // -------------------------------------------------------------------------

  it('cancelling the dialog does NOT call onSetLocked', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="indicator" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(onSetLocked).not.toHaveBeenCalled();
  });

  it('cancelling the dialog closes it', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={true} onSetLocked={onSetLocked} entityLabel="indicator" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Disabled prop
  // -------------------------------------------------------------------------

  it('disabled button does not call onSetLocked when unlocked', () => {
    const onSetLocked = vi.fn();
    render(
      <LockToggle locked={false} onSetLocked={onSetLocked} entityLabel="indicator" disabled />,
    );
    // The button has disabled attr; fireEvent.click still fires in jsdom,
    // but our handler checks disabled and returns early.
    const btn = screen.getByTestId('lock-toggle-btn');
    // Verify the DOM attribute
    expect(btn.disabled).toBe(true);
    // Firing click on a disabled button — handler guards it
    fireEvent.click(btn);
    expect(onSetLocked).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // Aria / title labels
  // -------------------------------------------------------------------------

  it('shows "Lock indicator" title/aria-label when unlocked', () => {
    render(
      <LockToggle locked={false} onSetLocked={vi.fn()} entityLabel="indicator" />,
    );
    const btn = screen.getByTestId('lock-toggle-btn');
    expect(btn.getAttribute('aria-label')).toBe('Lock indicator');
    expect(btn.getAttribute('title')).toBe('Lock indicator');
  });

  it('shows "Unlock signal" title/aria-label when locked', () => {
    render(
      <LockToggle locked={true} onSetLocked={vi.fn()} entityLabel="signal" />,
    );
    const btn = screen.getByTestId('lock-toggle-btn');
    expect(btn.getAttribute('aria-label')).toBe('Unlock signal');
    expect(btn.getAttribute('title')).toBe('Unlock signal');
  });

  // -------------------------------------------------------------------------
  // Dialog neutral styling — destructive=false is verified via dialog
  // presence/absence of red confirm style; we test label content instead
  // because the ConfirmDialog module CSS isn't available in jsdom
  // -------------------------------------------------------------------------

  it('dialog uses neutral confirm label "Unlock" (not destructive wording)', () => {
    render(
      <LockToggle locked={true} onSetLocked={vi.fn()} entityLabel="portfolio" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(screen.getByTestId('confirm-dialog-confirm').textContent).toBe('Unlock');
    expect(screen.getByTestId('confirm-dialog-cancel').textContent).toBe('Cancel');
  });

  it('dialog title mentions the entityLabel', () => {
    render(
      <LockToggle locked={true} onSetLocked={vi.fn()} entityLabel="signal" />,
    );
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    expect(screen.getByText('Unlock Signal?')).toBeDefined();
  });
});
