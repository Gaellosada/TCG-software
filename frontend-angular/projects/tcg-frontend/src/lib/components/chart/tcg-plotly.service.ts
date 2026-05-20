import { Injectable } from '@angular/core';

// The `plotly.js-dist-min` package ships untyped (no `@types/plotly.js`
// covers it directly); declare a minimal surface so we don't fall back to
// `any`. `unknown` covers data/layout/config shapes; consumers pass shapes
// matching Plotly's documented API.
//
// REVIEW: when migrating to `@types/plotly.js`, replace `TcgPlotlyModule` with
// `typeof import('plotly.js')` and drop this minimal declaration.
export type PlotlyModule = TcgPlotlyModule;

export interface TcgPlotlyModule {
  newPlot(
    root: HTMLElement,
    data: unknown[],
    layout?: unknown,
    config?: unknown,
  ): Promise<unknown>;
  react(root: HTMLElement, data: unknown[], layout?: unknown, config?: unknown): Promise<unknown>;
  purge(root: HTMLElement): void;
  Plots: { resize(root: HTMLElement): void };
}

/**
 * Lazy-loads `plotly.js-dist-min` on first call and caches the resolved
 * module promise so subsequent charts re-use the same instance without
 * re-downloading.
 *
 * G5: component-scoped — provided by `TcgChartComponent` itself. The
 * in-memory cache lives on the service instance; if a chart is mounted
 * in a new component tree, the JS-level module cache still hits (browser
 * dedupes the dynamic import).
 *
 * The dynamic `import()` is split into its own webpack/Vite chunk so the
 * Plotly bundle (~1 MB gzipped) does NOT land in the initial JS payload.
 */
@Injectable()
export class TcgPlotlyService {
  private modulePromise: Promise<TcgPlotlyModule> | null = null;

  load(): Promise<TcgPlotlyModule> {
    if (!this.modulePromise) {
      this.modulePromise = import(
        /* webpackChunkName: "plotly" */ 'plotly.js-dist-min'
        // The runtime type is opaque; we cast through `unknown` to our
        // minimal interface. The asserted shape is what we actually use.
      ).then((m) => {
        const mod = (m as { default?: TcgPlotlyModule }).default ?? (m as unknown as TcgPlotlyModule);
        return mod;
      });
    }
    return this.modulePromise;
  }
}
