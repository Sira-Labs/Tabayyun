import { useEffect, useId, useRef, type KeyboardEvent } from "react";
import { CHART_BG, type Theme } from "../../compare/palette";
import { BAND_FILL, spreadLabels, type PlotLane, type Shade } from "../../compare/plot";
import type { Range } from "../../seriesChart";
import { createPlot, joinTables, syncKey, type Plot, type PlotData, type PlotOptions } from "./uplot";

const NS_PER_S = 1e9;
const AXIS = "#64748b";
const GRID = "rgba(100, 116, 139, 0.15)";
const AXIS_SIZE = 60;
// uPlot adds this to a labelled axis; a lane without a right axis reserves it too, so the
// time axes of all lanes line up.
const AXIS_LABEL_SIZE = 30;
const MIN_WIDTH = 300;
// Synced lanes report one drag each; the repeats arrive within this many ms.
const SELECT_ECHO_MS = 300;

type Props = {
  lanes: PlotLane[];
  range: Range;
  theme: Theme;
  /** Ids of the series switched off in the legend. */
  hidden: ReadonlySet<string>;
  /** Where the keyboard put the cursor (ns), or null. */
  keyCursor: number | null;
  label: string;
  /** A span dragged in any lane. */
  onSelect: (range: Range) => void;
  /** Double-click: back to the previous window. */
  onBack: () => void;
  /** The time under the pointer (ns), or null when it leaves. */
  onCursor: (ts: number | null) => void;
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void;
};

type Handlers = Pick<Props, "onSelect" | "onBack" | "onCursor">;

const pxRatio = () => (typeof devicePixelRatio === "number" && devicePixelRatio > 0 ? devicePixelRatio : 1);

/** The lane's lines outer-joined on time. */
function laneData(lane: PlotLane): PlotData {
  return joinTables(lane.lines.map((l) => [l.ts, l.values] as PlotData));
}

/** Shaded finding windows over the full height of the lane, the selected one outlined. */
function drawShades(u: Plot, shades: Shade[]) {
  const { ctx, bbox } = u;
  for (const s of shades) {
    const x0 = Math.max(bbox.left, u.valToPos(s.from / NS_PER_S, "x", true));
    const x1 = Math.min(bbox.left + bbox.width, u.valToPos(s.to / NS_PER_S, "x", true));
    if (x1 <= x0) continue;
    // A finding a pixel wide still shows.
    const width = Math.max(x1 - x0, 2);
    ctx.fillStyle = s.fill;
    ctx.fillRect(x0, bbox.top, width, bbox.height);
    if (s.selected) {
      ctx.strokeStyle = AXIS;
      ctx.lineWidth = 2;
      ctx.strokeRect(x0, bbox.top + 1, width, bbox.height - 2);
    }
  }
}

function drawZeroLine(u: Plot, scale: string) {
  const { ctx, bbox } = u;
  const y = u.valToPos(0, scale, true);
  if (y < bbox.top || y > bbox.top + bbox.height) return;
  ctx.strokeStyle = AXIS;
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(bbox.left, y);
  ctx.lineTo(bbox.left + bbox.width, y);
  ctx.stroke();
}

/** Each visible line named at its right end, the suspect marked; labels are moved apart. */
function drawLabels(u: Plot, lane: PlotLane, theme: Theme) {
  const { ctx, bbox } = u;
  const ratio = pxRatio();
  const items = lane.lines.flatMap((line, i) => {
    if (!line.direct || !u.series[i + 1]?.show) return [];
    const ys = u.data[i + 1] ?? [];
    let k = ys.length - 1;
    while (k >= 0 && (ys[k] === null || ys[k] === undefined)) k -= 1;
    if (k < 0) return [];
    const y = u.valToPos(ys[k] as number, line.scale, true);
    return [{ line, y: Math.min(bbox.top + bbox.height, Math.max(bbox.top + 14 * ratio, y)) }];
  });
  const ys = spreadLabels(
    items.map((it) => it.y),
    13 * ratio,
  );
  ctx.save();
  ctx.font = `${11 * ratio}px system-ui, sans-serif`;
  ctx.textAlign = "right";
  ctx.textBaseline = "bottom";
  ctx.lineJoin = "round";
  ctx.setLineDash([]);
  items.forEach(({ line }, n) => {
    const text = line.suspect ? `${line.label} (suspect)` : line.label;
    const x = bbox.left + bbox.width - 4 * ratio;
    const y = ys[n]! - 3 * ratio;
    // A halo in the background colour keeps the label readable over other lines.
    ctx.strokeStyle = CHART_BG[theme];
    ctx.lineWidth = 3 * ratio;
    ctx.strokeText(text, x, y);
    ctx.fillStyle = line.stroke;
    ctx.fillText(text, x, y);
  });
  ctx.restore();
}

type LaneContext = {
  width: number;
  theme: Theme;
  /** Some lane has a right axis: the others reserve its width, so plot areas line up. */
  anyRight: boolean;
  sync: string;
  range: Range;
  hidden: ReadonlySet<string>;
  handlers: { current: Handlers };
  lastSelect: { current: { key: string; at: number } };
};

/** uPlot options of one lane. */
function laneOptions(lane: PlotLane, c: LaneContext): PlotOptions {
  const axes: NonNullable<PlotOptions["axes"]> = [{ stroke: AXIS, grid: { stroke: GRID } }];
  for (const a of lane.axes) {
    if (a.side === "none") continue;
    axes.push({ scale: a.scale, side: a.side === "right" ? 1 : 3, label: a.label, labelSize: AXIS_LABEL_SIZE, size: AXIS_SIZE, stroke: AXIS, grid: { stroke: GRID, show: a.side === "left" } });
  }
  if (c.anyRight && !lane.axes.some((a) => a.side === "right")) {
    axes.push({ scale: lane.axes[0]?.scale ?? "y0", side: 1, size: AXIS_SIZE + AXIS_LABEL_SIZE, stroke: AXIS, values: (_u, splits) => splits.map(() => ""), ticks: { show: false }, grid: { show: false } });
  }
  return {
    width: c.width,
    height: lane.height,
    legend: { show: false },
    cursor: { drag: { x: true, y: false, setScale: false }, sync: { key: c.sync, setSeries: false } },
    scales: { x: { time: true, range: () => [c.range.from / NS_PER_S, c.range.to / NS_PER_S] } },
    axes,
    series: [
      {},
      ...lane.lines.map((l) => ({
        label: l.label,
        scale: l.scale,
        stroke: l.stroke,
        width: l.width,
        dash: l.dash,
        spanGaps: false,
        points: { show: false },
        show: !c.hidden.has(l.id),
      })),
    ],
    bands: lane.band ? [{ series: [lane.band.hi + 1, lane.band.lo + 1], fill: BAND_FILL }] : undefined,
    hooks: {
      drawClear: [
        (u) => {
          const { ctx, bbox } = u;
          ctx.save();
          ctx.beginPath();
          ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
          ctx.clip();
          drawShades(u, lane.shades);
          if (lane.zeroLine) drawZeroLine(u, lane.lines[0]?.scale ?? "y0");
          ctx.restore();
        },
      ],
      draw: [(u) => drawLabels(u, lane, c.theme)],
      setSelect: [
        (u) => {
          if (u.select.width > 2) {
            const from = Math.round(u.posToVal(u.select.left, "x") * NS_PER_S);
            const to = Math.round(u.posToVal(u.select.left + u.select.width, "x") * NS_PER_S);
            // To the second: synced lanes report the same drag with sub-pixel differences.
            const key = `${Math.round(from / NS_PER_S)}-${Math.round(to / NS_PER_S)}`;
            const now = Date.now();
            const echo = c.lastSelect.current.key === key && now - c.lastSelect.current.at < SELECT_ECHO_MS;
            c.lastSelect.current = { key, at: now };
            if (!echo) c.handlers.current.onSelect({ from, to });
          }
          u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
        },
      ],
      setCursor: [
        (u) => {
          const left = u.cursor.left;
          c.handlers.current.onCursor(left === undefined || left === null || left < 0 ? null : u.posToVal(left, "x") * NS_PER_S);
        },
      ],
    },
  };
}

/** The series of a comparison: one uPlot instance per lane, stacked, sharing the time axis,
 * the cursor and drag-zoom (`uPlot.sync`). The default export, so the page loads it lazily. */
export default function CompareChart({ lanes, range, theme, hidden, keyCursor, label, onSelect, onBack, onCursor, onKeyDown }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plots = useRef<{ plot: Plot; lane: PlotLane }[]>([]);
  // Handlers change every render; uPlot's hooks read them through a ref.
  const handlers = useRef<Handlers>({ onSelect, onBack, onCursor });
  const hiddenNow = useRef(hidden);
  const lastSelect = useRef({ key: "", at: 0 });
  const syncName = useId();

  // Whether the plots' cursors were last placed by the keyboard: only those are cleared with it.
  const keyOwned = useRef(false);

  useEffect(() => {
    // uPlot calls this hook for pointer movement only (the keyboard cursor is set without
    // firing hooks), so the pointer takes the cursor over from the keyboard here.
    const fromPointer = (ts: number | null) => {
      keyOwned.current = false;
      onCursor(ts);
    };
    handlers.current = { onSelect, onBack, onCursor: fromPointer };
    hiddenNow.current = hidden;
  });

  // The lanes are rebuilt when what they draw changes; toggles and the cursor go through the
  // effects below.
  useEffect(() => {
    const el = host.current;
    if (!el || lanes.length === 0) return;
    const context: LaneContext = {
      width: Math.max(el.clientWidth, MIN_WIDTH),
      theme,
      anyRight: lanes.some((l) => l.axes.some((a) => a.side === "right")),
      sync: syncKey(syncName),
      range,
      hidden: hiddenNow.current,
      handlers,
      lastSelect,
    };
    const back = () => handlers.current.onBack();
    const made = lanes.map((lane) => {
      const target = document.createElement("div");
      target.dataset.lane = lane.key;
      el.append(target);
      const plot = createPlot(laneOptions(lane, context), laneData(lane), target);
      plot.over?.addEventListener("dblclick", back);
      return { plot, lane, target };
    });
    plots.current = made;
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            for (const { plot, lane } of made) plot.setSize({ width: Math.max(el.clientWidth, MIN_WIDTH), height: lane.height });
          });
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      for (const { plot, target } of made) {
        plot.over?.removeEventListener("dblclick", back);
        plot.destroy();
        target.remove();
      }
      plots.current = [];
    };
  }, [lanes, range, theme, syncName]);

  useEffect(() => {
    for (const { plot, lane } of plots.current) {
      lane.lines.forEach((line, i) => {
        const show = !hidden.has(line.id);
        if (plot.series[i + 1]?.show !== show) plot.setSeries(i + 1, { show });
      });
    }
  }, [hidden, lanes]);

  useEffect(() => {
    if (keyCursor === null) {
      if (keyOwned.current) {
        for (const { plot } of plots.current) plot.setCursor({ left: -10, top: -10 }, false);
        keyOwned.current = false;
      }
      return;
    }
    keyOwned.current = true;
    for (const { plot, lane } of plots.current) {
      plot.setCursor({ left: plot.valToPos(keyCursor / NS_PER_S, "x"), top: lane.height / 2 }, false);
    }
  }, [keyCursor, lanes]);

  return (
    <div
      ref={host}
      tabIndex={0}
      role="figure"
      aria-label={label}
      onKeyDown={onKeyDown}
      className="w-full rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
      data-testid="compare-chart"
    />
  );
}
