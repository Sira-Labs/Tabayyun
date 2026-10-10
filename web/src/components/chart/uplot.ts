// The one place that touches uPlot (ADR-0008). Pages draw through `createPlot`, so tests can
// replace this module: jsdom has no canvas.
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export type Plot = uPlot;
export type PlotOptions = uPlot.Options;
export type PlotData = uPlot.AlignedData;

/** A uPlot instance drawing `data` into `target`. */
export function createPlot(opts: PlotOptions, data: PlotData, target: HTMLElement): Plot {
  return new uPlot(opts, data, target);
}
