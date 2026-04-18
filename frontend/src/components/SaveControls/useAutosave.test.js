// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import React, { useState } from 'react';
import useAutosave from './useAutosave';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// Minimal harness — no JSX so this file stays a .js (plan-stated filename).
function Harness({ enabled, initialDirty, initialValue, onSave, debounceMs }) {
  const [dirty] = useState(initialDirty);
  const [value, setValue] = useState(initialValue);
  useAutosave({ enabled, dirty, value, onSave, debounceMs });
  Harness.setValue = setValue;
  return null;
}

function mount(props) {
  return render(React.createElement(Harness, props));
}

describe('useAutosave', () => {
  it('fires onSave after debounceMs when enabled && dirty', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    expect(onSave).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(499); });
    expect(onSave).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave).toHaveBeenCalledWith({ v: 1 });
  });

  it('is a no-op when enabled is false', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: false, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(10000); });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('does not fire when dirty is false', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: false, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('cancels pending save when enabled flips to false', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    const { rerender } = mount({ enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(200); });
    rerender(React.createElement(Harness, { enabled: false, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 }));
    act(() => { vi.advanceTimersByTime(1000); });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('reschedules when value changes before timer fires', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(300); });
    act(() => { Harness.setValue({ v: 2 }); });
    act(() => { vi.advanceTimersByTime(300); });
    expect(onSave).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(200); });
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave).toHaveBeenCalledWith({ v: 2 });
  });

  it('flushes pending save on beforeunload', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: true, initialValue: { v: 42 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(100); });
    expect(onSave).not.toHaveBeenCalled();
    act(() => { window.dispatchEvent(new Event('beforeunload')); });
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave).toHaveBeenCalledWith({ v: 42 });
  });

  it('flushes on pagehide', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: true, initialValue: { v: 7 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(50); });
    act(() => { window.dispatchEvent(new Event('pagehide')); });
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave).toHaveBeenCalledWith({ v: 7 });
  });

  it('flush is a no-op when no timer is pending', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    mount({ enabled: true, initialDirty: false, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { window.dispatchEvent(new Event('beforeunload')); });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('uses the latest onSave when it changes', () => {
    vi.useFakeTimers();
    const onSave1 = vi.fn();
    const onSave2 = vi.fn();
    const { rerender } = mount({ enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave: onSave1, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(200); });
    rerender(React.createElement(Harness, { enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave: onSave2, debounceMs: 500 }));
    act(() => { vi.advanceTimersByTime(400); });
    expect(onSave1).not.toHaveBeenCalled();
    expect(onSave2).toHaveBeenCalledOnce();
  });

  it('does not flush after unmount', () => {
    vi.useFakeTimers();
    const onSave = vi.fn();
    const { unmount } = mount({ enabled: true, initialDirty: true, initialValue: { v: 1 }, onSave, debounceMs: 500 });
    act(() => { vi.advanceTimersByTime(100); });
    unmount();
    act(() => { window.dispatchEvent(new Event('beforeunload')); });
    expect(onSave).not.toHaveBeenCalled();
  });
});
