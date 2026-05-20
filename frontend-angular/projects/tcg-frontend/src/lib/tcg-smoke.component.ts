import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';

import { TcgApiService } from './api/tcg-api.service';

/**
 * Minimal standalone component used by the dev-harness to prove that the
 * library is wired up to a live FastAPI backend. Hosts that pull the
 * library never need this component — it exists so `ng serve dev-harness`
 * is a meaningful smoke test, not just a static page.
 *
 * NOTE: no global styles, no router dependencies, no BrowserModule.
 */
@Component({
  selector: 'tcg-smoke',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="tcg-smoke">
      <h1 class="tcg-smoke__title">TCG Angular library — connectivity smoke</h1>
      <p class="tcg-smoke__status" [attr.data-state]="state()">
        Backend status: <strong>{{ state() }}</strong>
      </p>
      @if (errorMessage(); as msg) {
        <pre class="tcg-smoke__error">{{ msg }}</pre>
      }
      @if (payloadPreview(); as preview) {
        <pre class="tcg-smoke__payload">{{ preview }}</pre>
      }
    </section>
  `,
  styles: [
    `
      .tcg-smoke {
        font-family: system-ui, sans-serif;
        padding: 1.5rem;
      }
      .tcg-smoke__title {
        margin: 0 0 0.75rem;
        font-size: 1.25rem;
      }
      .tcg-smoke__status[data-state='ok'] strong {
        color: #1f7a3a;
      }
      .tcg-smoke__status[data-state='error'] strong {
        color: #a8331a;
      }
      .tcg-smoke__error,
      .tcg-smoke__payload {
        background: #f4f4f4;
        padding: 0.5rem 0.75rem;
        border-radius: 4px;
        overflow-x: auto;
      }
    `,
  ],
})
export class TcgSmokeComponent implements OnInit {
  private readonly api = inject(TcgApiService);

  readonly state = signal<'loading' | 'ok' | 'error'>('loading');
  readonly errorMessage = signal<string | null>(null);
  readonly payloadPreview = signal<string | null>(null);

  ngOnInit(): void {
    this.api.getHealth().subscribe({
      next: (payload) => {
        this.state.set('ok');
        // Preview at most 200 chars so a large response doesn't blow the DOM.
        const json = JSON.stringify(payload);
        this.payloadPreview.set(json.length > 200 ? json.slice(0, 200) + '…' : json);
      },
      error: (err: unknown) => {
        this.state.set('error');
        this.errorMessage.set(this.describe(err));
      },
    });
  }

  private describe(err: unknown): string {
    if (err && typeof err === 'object') {
      const anyErr = err as { status?: number; message?: string; statusText?: string };
      const parts: string[] = [];
      if (typeof anyErr.status === 'number') parts.push(`HTTP ${anyErr.status}`);
      if (anyErr.statusText) parts.push(anyErr.statusText);
      if (anyErr.message) parts.push(anyErr.message);
      if (parts.length) return parts.join(' — ');
    }
    return String(err);
  }
}
