/**
 * Library-wide ambient type declarations.
 *
 * `plotly.js-dist-min` ships a UMD bundle without TypeScript types. The
 * actual surface we use lives in `TcgPlotlyService.PlotlyModule`. This
 * declaration is the minimum that satisfies the TS compiler at the
 * dynamic-import site.
 */
declare module 'plotly.js-dist-min' {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const Plotly: any;
  export default Plotly;
}
