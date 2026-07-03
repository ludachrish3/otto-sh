// `plotly.js-gl2d-dist-min` ships no type declarations of its own (no
// "types"/"typings" entry in its package.json) — this is the minimal typed
// surface `plotly.ts` wraps: `newPlot`/`react`/`extendTraces`/`relayout`,
// the four calls dashboard.js makes (`Plotly.newPlot`/`Plotly.react`/
// `Plotly.extendTraces`/`Plotly.relayout`), plus `Plots.resize` (Task 11 —
// the resize-follows-container fix; see its doc comment below).
// `PlotlyData`/`PlotlyLayout` are deliberately loose `Record<string,
// unknown>` shapes rather than pulling in @types/plotly.js's much larger
// (and often overly strict — e.g. requiring every trace's `dash`/`marker`
// field to be an exact enum literal) surface for four functions: the
// trace/layout objects here are built exclusively by `plotly.ts`'s own
// byte-parity port of dashboard.js's `buildMetricTraces`/`buildLayout`, so no
// external caller needs a richer type. Widen this file (not `any`) if a
// later task needs more of the API.
declare module "plotly.js-gl2d-dist-min" {
  export type PlotlyData = Record<string, unknown>;
  export type PlotlyLayout = Record<string, unknown>;

  export interface PlotlyConfig {
    responsive?: boolean;
    displaylogo?: boolean;
    modeBarButtonsToRemove?: readonly string[];
  }

  /** The payload plotly.js's `plotly_clickannotation` event hands its listeners — `data.index` is the annotation's position in the `layout.annotations` array the graph was drawn with (dashboard.js's `buildAnnotations()` maps 1:1 over `state.events`, so it doubles as an event index). */
  export interface PlotlyClickAnnotationEvent {
    index: number;
    event: MouseEvent;
  }

  interface PlotlyStatic {
    newPlot(
      div: HTMLElement,
      data: PlotlyData[],
      layout?: PlotlyLayout,
      config?: PlotlyConfig,
    ): Promise<HTMLElement>;
    react(div: HTMLElement, data: PlotlyData[], layout?: PlotlyLayout): Promise<HTMLElement>;
    extendTraces(div: HTMLElement, update: Record<string, unknown[][]>, indices: number[]): void;
    relayout(div: HTMLElement, update: Record<string, unknown>): Promise<HTMLElement>;
    /**
     * Task 11: `Plots.resize(gd)` re-measures the graph div's current box
     * and redraws to fit it — the same call Plotly's own `config.responsive`
     * window-resize handler makes internally, and what `react-plotly.js`
     * calls from its own ResizeObserver for exactly this "keep a plot
     * fitted to a resizing container" use case. Preferred here over a
     * hand-rolled `relayout(div, {width})` as the documented, purpose-built
     * entry point for this rather than reconstructing its effect from a
     * lower-level primitive.
     */
    Plots: {
      resize(div: HTMLElement): Promise<void>;
    };
  }

  const Plotly: PlotlyStatic;
  export default Plotly;
}
