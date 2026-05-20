import { Component, Signal, inject, signal } from '@angular/core';
import { TestBed, fakeAsync, tick, flushMicrotasks } from '@angular/core/testing';
import {
  TcgBackendAutosaveHandle,
  TcgBackendAutosaveRegistration,
  TcgBackendAutosaveService,
} from './tcg-backend-autosave.service';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (err?: unknown) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (err?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * Test harness component — provides the service component-scoped, mirrors
 * the React Harness shape (exposes `setPayload`, `reset`, `lastStatus`).
 */
@Component({
  selector: 'tcg-harness',
  standalone: true,
  template: '',
  providers: [TcgBackendAutosaveService],
})
class HarnessComponent {
  readonly enabled = signal(true);
  readonly payload = signal<unknown>({ v: 0 });

  private readonly autosave = inject(TcgBackendAutosaveService);
  handle!: TcgBackendAutosaveHandle;
  onSaveSpy?: jasmine.Spy;

  register(onSave: TcgBackendAutosaveRegistration<unknown>['onSave'], debounceMs: number): void {
    this.handle = this.autosave.register({
      enabled: this.enabled,
      payload: this.payload,
      onSave,
      debounceMs,
    });
  }
}

describe('TcgBackendAutosaveService — race scenarios (parity with useBackendAutosave.race.test.jsx)', () => {
  function mount(onSave: (payload: unknown, opts: { signal: AbortSignal }) => Promise<unknown>, debounceMs = 100): HarnessComponent {
    TestBed.configureTestingModule({ imports: [HarnessComponent] });
    const fixture = TestBed.createComponent(HarnessComponent);
    fixture.detectChanges();
    const harness = fixture.componentInstance;
    harness.register(onSave, debounceMs);
    fixture.detectChanges();
    return harness;
  }

  it('scenario 1: AbortSignal threaded into onSave; second edit during in-flight coalesces (no concurrent invocation)', fakeAsync(() => {
    const calls: Array<{ payload: unknown; signal: AbortSignal }> = [];
    const deferreds: Array<Deferred<unknown>> = [];
    const onSave = jasmine
      .createSpy('onSave')
      .and.callFake((payload: unknown, opts: { signal: AbortSignal }) => {
        const d = deferred<unknown>();
        calls.push({ payload, signal: opts.signal });
        deferreds.push(d);
        return d.promise;
      });
    const harness = mount(onSave, 100);

    // Debounce #1 fires → save #1 in flight.
    tick(100);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(calls[0].payload).toEqual({ v: 0 });
    expect(typeof calls[0].signal.aborted).toBe('boolean');

    // User edits while in flight → coalesced.
    harness.payload.set({ v: 2 });
    tick(100);
    expect(onSave).toHaveBeenCalledTimes(1);

    deferreds[0].resolve(null);
    flushMicrotasks();
    // pendingRestart fires the second save.
    expect(onSave).toHaveBeenCalledTimes(2);
    expect(calls[1].payload).toEqual({ v: 2 });
    deferreds[1].resolve(null);
    flushMicrotasks();
    expect(harness.handle.status()).toBe('saved');
  }));

  it('scenario 2: rejected save #1 still triggers pendingRestart with latest payload', fakeAsync(() => {
    const deferreds: Array<Deferred<unknown>> = [];
    const onSave = jasmine.createSpy('onSave').and.callFake(() => {
      const d = deferred<unknown>();
      deferreds.push(d);
      return d.promise;
    });
    const harness = mount(onSave, 50);

    tick(50);
    expect(deferreds.length).toBe(1);
    expect(harness.handle.status()).toBe('saving');

    harness.payload.set({ v: 2 });
    tick(50);
    expect(deferreds.length).toBe(1); // coalesced

    deferreds[0].reject(new Error('boom'));
    flushMicrotasks();
    flushMicrotasks();
    expect(deferreds.length).toBe(2);
    expect(harness.handle.status()).toBe('saving');

    deferreds[1].resolve(null);
    flushMicrotasks();
    expect(harness.handle.status()).toBe('saved');
  }));

  it('scenario 3: dispose during in-flight save → status updates suppressed', fakeAsync(() => {
    const d = deferred<unknown>();
    const onSave = jasmine.createSpy('onSave').and.returnValue(d.promise);
    const harness = mount(onSave, 50);

    tick(50);
    expect(onSave).toHaveBeenCalled();

    harness.handle.dispose();

    d.resolve(null);
    flushMicrotasks();
    flushMicrotasks();
    // After dispose, status must NOT flip to 'saved'.
    expect(harness.handle.status()).not.toBe('saved');
  }));

  it('scenario 4: reset() aborts in-flight save (signal.aborted true)', fakeAsync(() => {
    const d = deferred<unknown>();
    let savedSignal: AbortSignal | undefined;
    const onSave = jasmine
      .createSpy('onSave')
      .and.callFake((_p: unknown, opts: { signal: AbortSignal }) => {
        savedSignal = opts.signal;
        return d.promise;
      });
    const harness = mount(onSave, 50);

    tick(50);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(savedSignal?.aborted).toBe(false);

    harness.handle.reset();
    expect(savedSignal?.aborted).toBe(true);
    expect(harness.handle.status()).toBe('idle');
  }));

  it('scenario 6: sustained backend hang → at most one in-flight save (coalescing)', fakeAsync(() => {
    const onSave = jasmine.createSpy('onSave').and.callFake(() => new Promise(() => {})); // never resolves
    const harness = mount(onSave, 50);

    for (let i = 1; i <= 5; i++) {
      harness.payload.set({ v: i });
      tick(50);
    }
    expect(onSave.calls.count()).toBeLessThanOrEqual(1);
  }));

  it('flush() — synchronous fire of pending debounced save', fakeAsync(() => {
    const onSave = jasmine.createSpy('onSave').and.returnValue(Promise.resolve(null));
    const harness = mount(onSave, 5000);

    harness.payload.set({ v: 1 });
    // Don't tick yet — flush should fire it now.
    harness.handle.flush();
    flushMicrotasks();
    expect(onSave).toHaveBeenCalled();
  }));

  it('setStatus() — one-shot status mutation outside the debounce path', fakeAsync(() => {
    const onSave = jasmine.createSpy('onSave').and.returnValue(Promise.resolve(null));
    const harness = mount(onSave, 5000);
    harness.handle.setStatus('error');
    expect(harness.handle.status()).toBe('error');
    harness.handle.setStatus('saved');
    expect(harness.handle.status()).toBe('saved');
  }));
});
