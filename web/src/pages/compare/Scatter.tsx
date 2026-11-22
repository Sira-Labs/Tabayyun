import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo, useState, type KeyboardEvent } from "react";
import { ApiError } from "../../api";
import { compareApi, type ScatterAxis, type ScatterResponse } from "../../compare/api";
import type { Theme } from "../../compare/palette";
import { inFinding, involves, scaleOf, summaryText, timeColor, timePosition, type Scale } from "../../compare/scatter";
import { formatLocalTime, formatNumber } from "../../format";
import type { Range } from "../../seriesChart";
import type { Finding } from "../../types";
import { ErrorLine, linkButtonClass, selectClass } from "../admin/ui";

const WIDTH = 640;
const HEIGHT = 380;
const MARGIN = { top: 12, right: 16, bottom: 46, left: 64 };
const PLOT_W = WIDTH - MARGIN.left - MARGIN.right;
const PLOT_H = HEIGHT - MARGIN.top - MARGIN.bottom;

const DOT_R = 2.5;
const SELECTED_R = 4;
/** The ring of a point in a finding window: the theme's text colour. */
const RING: Record<Theme, string> = { light: "#0f172a", dark: "#f1f5f9" };

const mutedClass = "text-sm text-slate-600 dark:text-slate-400";
const chipClass = "rounded border border-slate-300 px-2 py-1 text-sm hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800";

/** `A and B`. */
const both = (names: string[]) => names.join(" and ");
const titleOf = (axis: ScatterAxis) => `${axis.name}${axis.unit ? ` (${axis.unit})` : ""}`;
const valueOf = (value: number, axis: ScatterAxis) => `${formatNumber(value)}${axis.unit ? ` ${axis.unit}` : ""}`;

type Point = { i: number; ts: number; x: number; y: number; t: number; finding: boolean; selected: boolean };

/** The answer's points with their place on the time ramp and their finding marks. */
function pointsOf(data: ScatterResponse, window: Range, findings: Finding[], selected: string | undefined): Point[] {
  const grid = Number(data.grid_ns);
  const pair = [data.x.id, data.y.id];
  const chosen = findings.filter((f) => f.id === selected);
  const marking = findings.filter((f) => f.id === selected || involves(f, pair));
  return data.ts.flatMap((raw, i) => {
    const ts = Number(raw);
    const x = data.xs[i];
    const y = data.ys[i];
    if (x === undefined || y === undefined) return [];
    return [{ i, ts, x, y, t: timePosition(ts, window), finding: inFinding(ts, grid, marking), selected: inFinding(ts, grid, chosen) }];
  });
}

const pointText = (p: Point, data: ScatterResponse) => `${formatLocalTime(p.ts)} · ${data.x.name}: ${valueOf(p.x, data.x)} · ${data.y.name}: ${valueOf(p.y, data.y)}`;

type PlotProps = { data: ScatterResponse; points: Point[]; window: Range; theme: Theme; active: number | null };

/** The cloud: x against y, one dot per bin in the colour of its time; dots in finding windows
 * are ringed, the selected finding's are drawn last and stronger. */
function Plot({ data, points, window, theme, active }: PlotProps) {
  const dark = theme === "dark";
  const xScale = useMemo(() => scaleOf(points.map((p) => p.x), PLOT_W), [points]);
  const yScale = useMemo(() => scaleOf(points.map((p) => p.y), PLOT_H), [points]);
  const px = (p: Point) => MARGIN.left + xScale.at(p.x);
  const py = (p: Point) => MARGIN.top + PLOT_H - yScale.at(p.y);
  const ringed = points.filter((p) => p.finding).length;
  const label =
    `Scatter of ${data.y.name} against ${data.x.name}, ${formatLocalTime(window.from)} to ${formatLocalTime(window.to)}: ` +
    `${formatNumber(points.length)} ${points.length === 1 ? "point" : "points"} coloured by time, ${ringed === 0 ? "none" : formatNumber(ringed)} inside finding windows.`;
  // Plain dots first, ringed ones over them, the selected finding's on top.
  const order = [...points].sort((a, b) => Number(a.selected) - Number(b.selected) || Number(a.finding) - Number(b.finding));
  const focus = active === null ? undefined : points.find((p) => p.i === active);
  const axisText = "fill-slate-600 text-[11px] dark:fill-slate-400";
  const line = "stroke-slate-300 dark:stroke-slate-700";
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="h-auto w-full max-w-3xl" data-testid="compare-scatter">
      <Ticks scale={xScale} axis="x" line={line} text={axisText} />
      <Ticks scale={yScale} axis="y" line={line} text={axisText} />
      <rect x={MARGIN.left} y={MARGIN.top} width={PLOT_W} height={PLOT_H} fill="none" className={line} />
      <text x={MARGIN.left + PLOT_W / 2} y={HEIGHT - 8} textAnchor="middle" className="fill-slate-900 text-xs dark:fill-slate-100">
        {titleOf(data.x)}
      </text>
      <text transform={`translate(14 ${MARGIN.top + PLOT_H / 2}) rotate(-90)`} textAnchor="middle" className="fill-slate-900 text-xs dark:fill-slate-100">
        {titleOf(data.y)}
      </text>
      {order.map((p) => (
        <circle
          key={p.i}
          cx={px(p)}
          cy={py(p)}
          r={p.selected ? SELECTED_R : DOT_R}
          fill={timeColor(p.t, dark)}
          fillOpacity={p.finding ? 1 : 0.75}
          stroke={p.finding ? RING[theme] : "none"}
          strokeWidth={p.selected ? 2 : 1}
          data-finding={p.finding}
          data-selected={p.selected}
        >
          <title>{pointText(p, data)}</title>
        </circle>
      ))}
      {focus && <circle cx={px(focus)} cy={py(focus)} r={SELECTED_R + 4} fill="none" stroke={RING[theme]} strokeWidth={1.5} strokeDasharray="3 2" data-testid="scatter-focus" />}
    </svg>
  );
}

/** The grid lines and tick labels of one axis. */
function Ticks({ scale, axis, line, text }: { scale: Scale; axis: "x" | "y"; line: string; text: string }) {
  return (
    <g aria-hidden="true">
      {scale.ticks.map((tick) => {
        if (axis === "x") {
          const x = MARGIN.left + scale.at(tick);
          return (
            <g key={tick}>
              <line x1={x} x2={x} y1={MARGIN.top} y2={MARGIN.top + PLOT_H} className={line} strokeDasharray="2 3" />
              <text x={x} y={MARGIN.top + PLOT_H + 14} textAnchor="middle" className={text}>
                {formatNumber(tick)}
              </text>
            </g>
          );
        }
        const y = MARGIN.top + PLOT_H - scale.at(tick);
        return (
          <g key={tick}>
            <line x1={MARGIN.left} x2={MARGIN.left + PLOT_W} y1={y} y2={y} className={line} strokeDasharray="2 3" />
            <text x={MARGIN.left - 6} y={y + 4} textAnchor="end" className={text}>
              {formatNumber(tick)}
            </text>
          </g>
        );
      })}
    </g>
  );
}

/** What the colour and the rings mean: the time ramp with the window's two dates, and the two rings. */
function Key({ window, theme }: { window: Range; theme: Theme }) {
  const dark = theme === "dark";
  const swatch = (selected: boolean) => (
    <svg aria-hidden="true" viewBox="0 0 14 14" className="size-3.5">
      <circle cx={7} cy={7} r={selected ? SELECTED_R : DOT_R} fill={timeColor(0.5, dark)} stroke={RING[theme]} strokeWidth={selected ? 2 : 1} />
    </svg>
  );
  return (
    <ul aria-label="Legend" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
      <li className="flex items-center gap-2">
        <span>Colour by time: {formatLocalTime(window.from)}</span>
        <span aria-hidden="true" className="inline-block h-2.5 w-24 rounded-sm" style={{ background: `linear-gradient(to right, ${timeColor(0, dark)}, ${timeColor(1, dark)})` }} />
        <span>{formatLocalTime(window.to)}</span>
      </li>
      <li className="flex items-center gap-1">
        {swatch(false)}
        inside a finding window
      </li>
      <li className="flex items-center gap-1">
        {swatch(true)}
        selected finding
      </li>
    </ul>
  );
}

type TableProps = { data: ScatterResponse; points: Point[]; active: number | null; onActive: (i: number) => void };

/** The points as a table. One row is in the tab order; the arrow keys move through the rows,
 * and the row focused or pointed at is marked in the plot. */
function PointsTable({ data, points, active, onActive }: TableProps) {
  const onKeyDown = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    const step = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (step === 0 || !(e.target instanceof HTMLElement)) return;
    const row = e.target.closest("tr");
    const next = step > 0 ? row?.nextElementSibling : row?.previousElementSibling;
    if (next instanceof HTMLElement) {
      e.preventDefault();
      next.focus();
    }
  };
  const tabStop = points.some((p) => p.i === active) ? active : (points[0]?.i ?? null);
  return (
    <div className="mt-2 max-h-96 overflow-auto rounded border border-slate-200 dark:border-slate-800">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">Points of the scatter; the arrow keys move through the rows</caption>
        <thead className="sticky top-0 bg-slate-100 dark:bg-slate-800">
          <tr>
            <th scope="col" className="p-2">Time</th>
            <th scope="col" className="p-2">{titleOf(data.x)}</th>
            <th scope="col" className="p-2">{titleOf(data.y)}</th>
            <th scope="col" className="p-2">In a finding</th>
          </tr>
        </thead>
        <tbody onKeyDown={onKeyDown}>
          {points.map((p) => (
            <tr
              key={p.i}
              tabIndex={p.i === tabStop ? 0 : -1}
              onFocus={() => onActive(p.i)}
              onMouseEnter={() => onActive(p.i)}
              className="border-t border-slate-200 hover:bg-slate-50 focus:bg-sky-50 dark:border-slate-800 dark:hover:bg-slate-800 dark:focus:bg-sky-950"
            >
              <th scope="row" className="whitespace-nowrap p-2 font-normal">{formatLocalTime(p.ts)}</th>
              <td className="p-2 tabular-nums">{formatNumber(p.x)}</td>
              <td className="p-2 tabular-nums">{formatNumber(p.y)}</td>
              <td className="p-2">{p.selected ? "yes, the selected finding" : p.finding ? "yes" : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Props = {
  series: ScatterAxis[];
  xy: [string, string];
  /** The window the chart shows. */
  range: Range;
  findings: Finding[];
  selected: string | undefined;
  theme: Theme;
  onXy: (next: [string, string] | undefined) => void;
};

/** One compared series against another over the chart's window (spec 033). */
export function Scatter({ series, xy, range, findings, selected, theme, onXy }: Props) {
  const [x, y] = xy;
  const scatter = useQuery({
    queryKey: ["compare", "scatter", x, y, range.from, range.to],
    queryFn: () => compareApi.scatter(x, y, range),
    placeholderData: keepPreviousData,
    // A wrong request (unknown ids, window too large) does not get better by asking again.
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
  });
  const [tableOpen, setTableOpen] = useState(false);
  const [marked, setMarked] = useState<{ data: ScatterResponse; i: number } | null>(null);

  const data = scatter.data;
  // A marked point belongs to the answer it was marked in.
  const active = marked && marked.data === data ? marked.i : null;
  const window = useMemo<Range>(() => (data?.window ? { from: Number(data.window.from), to: Number(data.window.to) } : range), [data, range]);
  const points = useMemo(() => (data ? pointsOf(data, window, findings, selected) : []), [data, window, findings, selected]);
  const stale = scatter.isPlaceholderData;
  const nameOf = (id: string) => series.find((s) => s.id === id)?.name ?? id;
  const focus = active === null ? undefined : points.find((p) => p.i === active);

  const choose = (axis: 0 | 1, id: string) => {
    const other = xy[1 - axis]!;
    // Choosing the other axis' series swaps the two.
    if (id === other) return onXy([y, x]);
    onXy(axis === 0 ? [id, other] : [other, id]);
  };
  const axisSelect = (axis: 0 | 1) => (
    <label className="flex flex-col gap-1 text-sm">
      {axis === 0 ? "X axis" : "Y axis"}
      <select className={selectClass} value={xy[axis]} onChange={(e) => choose(axis, e.target.value)}>
        {series.map((s) => (
          <option key={s.id} value={s.id}>
            {titleOf(s)}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <section aria-labelledby="compare-scatter-heading" aria-busy={scatter.isFetching} className="space-y-3">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <h2 id="compare-scatter-heading" className="mr-auto text-base font-semibold">
          Scatter: {nameOf(y)} against {nameOf(x)}
        </h2>
        {axisSelect(0)}
        {axisSelect(1)}
        <button type="button" className={chipClass} onClick={() => onXy([y, x])}>
          Swap
        </button>
        <button type="button" className={chipClass} onClick={() => onXy(undefined)}>
          Hide scatter
        </button>
      </div>
      <ErrorLine error={scatter.error} prefix="Could not load the scatter" />
      {stale && (
        <p role="status" className={mutedClass}>
          Updating…
        </p>
      )}
      {!data ? (
        scatter.error ? null : <p className={mutedClass}>Loading the scatter…</p>
      ) : data.missing.length > 0 ? (
        <p className="text-sm text-amber-800 dark:text-amber-300">
          {both(data.missing.map(nameOf))} {data.missing.length === 1 ? "has" : "have"} no usable samples in this window.
        </p>
      ) : points.length < 2 ? (
        <p className="text-sm">Not enough bins with a value of both series to draw a scatter.</p>
      ) : (
        <div className={stale ? "space-y-2 opacity-50" : "space-y-2"}>
          <Plot data={data} points={points} window={window} theme={theme} active={active} />
          <p className={`min-h-5 ${mutedClass}`} data-testid="scatter-readout">
            {focus ? pointText(focus, data) : ""}
          </p>
          <Key window={window} theme={theme} />
          <p className="text-sm">{summaryText({ n_bins: data.n_bins, shown: points.length, gridNs: Number(data.grid_ns), r: data.r, inFindings: points.filter((p) => p.finding).length })}</p>
          <button type="button" className={linkButtonClass} aria-expanded={tableOpen} onClick={() => setTableOpen((v) => !v)}>
            View the scatter as a table
          </button>
          {tableOpen && <PointsTable data={data} points={points} active={active} onActive={(i) => setMarked({ data, i })} />}
        </div>
      )}
    </section>
  );
}
