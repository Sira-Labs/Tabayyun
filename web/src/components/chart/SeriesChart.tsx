import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { formatLocalTime } from "../../format";
import { keyAction, rugSegments, toPlotData, type Band, type ChartResponse, type Range, type Rect } from "../../seriesChart";
import { RUG_CLASS } from "./rug";
import { createPlot, type Plot, type PlotOptions } from "./uplot";

const NS_PER_S = 1e9;
const HEIGHT = 320;

// Translucent fills read on light and dark backgrounds alike.
const SEVERITY_FILL: Record<string, string> = {
  critical: "rgba(220, 38, 38, 0.22)",
  high: "rgba(234, 88, 12, 0.20)",
  medium: "rgba(217, 119, 6, 0.16)",
  low: "rgba(100, 116, 139, 0.16)",
};
const BAND_FILL = "rgba(14, 165, 233, 0.10)";
const BAND_EDGE = "rgba(14, 165, 233, 0.55)";
const LIMIT_STROKE = "rgba(220, 38, 38, 0.8)";
const LINE = "#0284c7";
const AXIS = "#64748b";
const GRID = "rgba(100, 116, 139, 0.15)";


/** What the chart draws besides the line; read through a ref so drawing never re-creates it. */
type Overlay = { rects: Rect[]; selected?: string; band: Band | null; limits: { min: number | null; max: number | null } };

type Props = {
  chart: ChartResponse;
  range: Range;
  label: string;
  unit: string | null;
  overlay: Overlay;
  /** A span dragged on the chart. */
  onSelect: (range: Range) => void;
  /** Double-click: back to the previous range. */
  onBack: () => void;
  /** A key on the focused chart: a new range or the whole extent. */
  onKey: (next: Range | "all") => void;
};

/** Fill `[x0, x1]` of the plot area between y values (null: the full height). */
function fillX(u: Plot, from: number, to: number, fill: string, stroke?: string) {
  const { ctx, bbox } = u;
  const x0 = Math.max(bbox.left, u.valToPos(from / NS_PER_S, "x", true));
  const x1 = Math.min(bbox.left + bbox.width, u.valToPos(to / NS_PER_S, "x", true));
  if (x1 <= x0) return;
  // A finding a pixel wide still shows.
  const width = Math.max(x1 - x0, 2);
  ctx.fillStyle = fill;
  ctx.fillRect(x0, bbox.top, width, bbox.height);
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 2;
    ctx.strokeRect(x0, bbox.top + 1, width, bbox.height - 2);
  }
}

/** Band and limits behind the line, then the findings. */
function drawOverlay(u: Plot, overlay: Overlay) {
  const { ctx, bbox } = u;
  ctx.save();
  ctx.beginPath();
  ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
  ctx.clip();
  const y = (v: number) => u.valToPos(v, "y", true);
  if (overlay.band) {
    const top = y(overlay.band.hi);
    const bottom = y(overlay.band.lo);
    ctx.fillStyle = BAND_FILL;
    ctx.fillRect(bbox.left, top, bbox.width, bottom - top);
    ctx.strokeStyle = BAND_EDGE;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    for (const edge of [top, bottom]) {
      ctx.beginPath();
      ctx.moveTo(bbox.left, edge);
      ctx.lineTo(bbox.left + bbox.width, edge);
      ctx.stroke();
    }
  }
  ctx.strokeStyle = LIMIT_STROKE;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 4]);
  for (const v of [overlay.limits.min, overlay.limits.max]) {
    if (v === null) continue;
    ctx.beginPath();
    ctx.moveTo(bbox.left, y(v));
    ctx.lineTo(bbox.left + bbox.width, y(v));
    ctx.stroke();
  }
  ctx.setLineDash([]);
  for (const r of overlay.rects) {
    fillX(u, r.from, r.to, SEVERITY_FILL[r.severity] ?? SEVERITY_FILL.low!, r.id === overlay.selected ? AXIS : undefined);
  }
  ctx.restore();
}

/** The series line with findings shaded, band and limits, a quality rug, drag-zoom and keys.
 * The default export, so the page can load it (and uPlot) lazily. */
export default function SeriesChart({ chart, range, label, unit, overlay, onSelect, onBack, onKey }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<Plot | null>(null);
  // Handlers and overlay change every render; uPlot's hooks read them through refs.
  const latest = useRef({ range, overlay, onSelect, onBack });
  const [plotBox, setPlotBox] = useState<{ left: number; width: number } | null>(null);

  useEffect(() => {
    latest.current = { range, overlay, onSelect, onBack };
  });

  // Create the plot once; data, size and overlays are pushed into it below.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const width = Math.max(el.clientWidth, 300);
    const opts: PlotOptions = {
      width,
      height: HEIGHT,
      legend: { show: false },
      cursor: { drag: { x: true, y: false, setScale: false } },
      scales: {
        x: { time: true, range: () => [latest.current.range.from / NS_PER_S, latest.current.range.to / NS_PER_S] },
      },
      axes: [
        { stroke: AXIS, grid: { stroke: GRID } },
        { stroke: AXIS, grid: { stroke: GRID }, label: unit ?? undefined, size: 60 },
      ],
      series: [{}, { label, stroke: LINE, width: 1.25, spanGaps: false, points: { show: false } }],
      hooks: {
        drawClear: [(u) => drawOverlay(u, latest.current.overlay)],
        setSelect: [
          (u) => {
            if (u.select.width > 2) {
              const from = u.posToVal(u.select.left, "x") * NS_PER_S;
              const to = u.posToVal(u.select.left + u.select.width, "x") * NS_PER_S;
              latest.current.onSelect({ from: Math.round(from), to: Math.round(to) });
            }
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          },
        ],
        setSize: [
          (u) => setPlotBox({ left: parseFloat(u.over.style.left) || 0, width: parseFloat(u.over.style.width) || u.width }),
        ],
      },
    };
    const u = createPlot(opts, toPlotData(chart), el);
    plot.current = u;
    const dbl = () => latest.current.onBack();
    u.over?.addEventListener("dblclick", dbl);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
      u.setSize({ width: Math.max(el.clientWidth, 300), height: HEIGHT });
    });
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      u.over?.removeEventListener("dblclick", dbl);
      u.destroy();
      plot.current = null;
    };
    // The plot is created once per mount; later data goes through setData.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    plot.current?.setData(toPlotData(chart), true);
  }, [chart, range]);

  useEffect(() => {
    plot.current?.redraw(false, false);
  }, [overlay]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const next = keyAction(e.key, range);
    if (next === null) return;
    e.preventDefault();
    onKey(next);
  };

  const segments = rugSegments(chart.quality, range);
  return (
    <div className="space-y-1">
      <div
        ref={host}
        tabIndex={0}
        role="figure"
        aria-label={`Chart of ${label}. Drag to zoom, double-click to go back; arrow keys pan, plus and minus zoom, Home shows all.`}
        onKeyDown={onKeyDown}
        className="w-full rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
        data-testid="series-chart"
      />
      <div style={plotBox ? { marginLeft: plotBox.left, width: plotBox.width } : undefined}>
        <div role="list" aria-label="Quality" className="relative h-2 w-full rounded-sm bg-emerald-600/25">
          {segments.map((s) => (
            <span
              key={`${s.from}-${s.quality}`}
              role="listitem"
              title={`${s.quality} quality, ${formatLocalTime(s.from)} to ${formatLocalTime(s.to)}`}
              aria-label={`${s.quality} quality from ${formatLocalTime(s.from)} to ${formatLocalTime(s.to)}`}
              className={`absolute top-0 h-2 ${RUG_CLASS[s.quality]}`}
              style={{ left: `${s.left}%`, width: `${s.width}%` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
