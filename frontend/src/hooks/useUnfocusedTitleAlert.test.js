// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useUnfocusedTitleAlert } from './useUnfocusedTitleAlert';

/**
 * Simulate document.hidden by overriding the property descriptor.
 * Must be restored after each test.
 */
function mockDocumentHidden(value) {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => value,
  });
}

describe('useUnfocusedTitleAlert', () => {
  const ORIGINAL_TITLE = 'TCG Platform';

  beforeEach(() => {
    document.title = ORIGINAL_TITLE;
    mockDocumentHidden(false);
    // Reset localStorage key
    localStorage.removeItem('tcg_notif_perm_asked');
    // Reset Notification API mock to default
    vi.stubGlobal('Notification', undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    mockDocumentHidden(false);
    document.title = ORIGINAL_TITLE;
  });

  it('does not modify title when page is visible on trigger', () => {
    mockDocumentHidden(false); // page visible
    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    expect(document.title).toBe(ORIGINAL_TITLE);
  });

  it('prefixes title with " ● " when page is hidden on turn_complete', () => {
    mockDocumentHidden(true); // page hidden

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);
  });

  it('prefixes title when page is hidden on auto_continue_capped', () => {
    mockDocumentHidden(true);

    const { rerender } = renderHook(
      ({ autoContinueCapped }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete: null,
          autoContinueCapped,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { autoContinueCapped: null } },
    );

    act(() => {
      rerender({ autoContinueCapped: { iter: 5, max: 5 } });
    });

    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);
  });

  it('prefixes title when page is hidden on notebook_ready', () => {
    mockDocumentHidden(true);

    const { rerender } = renderHook(
      ({ notebookReady }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete: null,
          autoContinueCapped: null,
          notebookReady,
          notebookFailedInfo: null,
        }),
      { initialProps: { notebookReady: false } },
    );

    act(() => {
      rerender({ notebookReady: true });
    });

    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);
  });

  it('prefixes title when page is hidden on notebook_failed', () => {
    mockDocumentHidden(true);

    const { rerender } = renderHook(
      ({ notebookFailedInfo }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete: null,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo,
        }),
      { initialProps: { notebookFailedInfo: null } },
    );

    act(() => {
      rerender({
        notebookFailedInfo: {
          reason: 'no_outputs',
          detail: null,
          timestamp: '2026-05-07T16:00:00Z',
        },
      });
    });

    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);
  });

  it('restores clean title on visibilitychange to visible', () => {
    mockDocumentHidden(true);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    // Trigger title prefix while hidden.
    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 3 } });
    });
    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);

    // Simulate tab becoming visible.
    act(() => {
      mockDocumentHidden(false);
      document.dispatchEvent(new Event('visibilitychange'));
    });

    expect(document.title).toBe(ORIGINAL_TITLE);
  });

  it('does not double-prefix title if already prefixed', () => {
    mockDocumentHidden(true);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 1 } });
    });
    const afterFirst = document.title;
    expect(afterFirst).toBe(`● ${ORIGINAL_TITLE}`);

    // Trigger again (new turn_complete value) — should not double-prefix.
    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 2 } });
    });
    expect(document.title).toBe(`● ${ORIGINAL_TITLE}`);
  });

  it('requests Notification permission after first turn_complete', () => {
    mockDocumentHidden(false); // page visible — permission still requested

    const mockRequestPermission = vi.fn().mockResolvedValue('granted');
    const MockNotification = vi.fn();
    MockNotification.permission = 'default';
    MockNotification.requestPermission = mockRequestPermission;
    vi.stubGlobal('Notification', MockNotification);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    expect(mockRequestPermission).not.toHaveBeenCalled();

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    expect(mockRequestPermission).toHaveBeenCalledOnce();
  });

  it('asks once per page load even if a prior page load already asked (no sticky localStorage gate)', () => {
    // C2 R-NOTIF-PERM-NO-RECOVERY: prior R7-iter1 used a localStorage flag
    // that left users with no recovery path after a single dismiss. The
    // updated hook drops the flag — every fresh page load gets one ask if
    // Notification.permission === 'default'. Browser UX handles
    // dismiss-without-spam (subsequent requestPermission() calls during the
    // same load resolve immediately with the current permission state).
    localStorage.setItem('tcg_notif_perm_asked', '1'); // legacy flag, must be ignored
    const mockRequestPermission = vi.fn().mockResolvedValue('default');
    const MockNotification = vi.fn();
    MockNotification.permission = 'default';
    MockNotification.requestPermission = mockRequestPermission;
    vi.stubGlobal('Notification', MockNotification);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    // Fresh page load (fresh ref) ⇒ asks regardless of legacy localStorage flag.
    expect(mockRequestPermission).toHaveBeenCalledOnce();
  });

  it('does not request permission if already denied', () => {
    const mockRequestPermission = vi.fn();
    const MockNotification = vi.fn();
    MockNotification.permission = 'denied';
    MockNotification.requestPermission = mockRequestPermission;
    vi.stubGlobal('Notification', MockNotification);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    expect(mockRequestPermission).not.toHaveBeenCalled();
  });

  it('fires OS notification when permission granted and page hidden', () => {
    mockDocumentHidden(true);

    const mockNotificationConstructor = vi.fn();
    mockNotificationConstructor.permission = 'granted';
    mockNotificationConstructor.requestPermission = vi.fn();
    vi.stubGlobal('Notification', mockNotificationConstructor);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    expect(mockNotificationConstructor).toHaveBeenCalledWith(
      'Task complete',
      expect.objectContaining({
        body: 'Task complete',
        tag: 'agent-task-complete',
      }),
    );
  });

  it('does NOT fire OS notification when page is visible (even if permission granted)', () => {
    mockDocumentHidden(false); // page visible

    const mockNotificationConstructor = vi.fn();
    mockNotificationConstructor.permission = 'granted';
    mockNotificationConstructor.requestPermission = vi.fn();
    vi.stubGlobal('Notification', mockNotificationConstructor);

    const { rerender } = renderHook(
      ({ lastTurnComplete }) =>
        useUnfocusedTitleAlert({
          lastTurnComplete,
          autoContinueCapped: null,
          notebookReady: false,
          notebookFailedInfo: null,
        }),
      { initialProps: { lastTurnComplete: null } },
    );

    act(() => {
      rerender({ lastTurnComplete: { at: new Date(), elapsedSeconds: 5 } });
    });

    expect(mockNotificationConstructor).not.toHaveBeenCalled();
  });
});
