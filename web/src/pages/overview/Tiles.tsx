import { scoreTone } from "../../components/Badges";
import { formatNumber } from "../../format";
import { metricLabel, scoreText, sparklinePoints, trendWords, type OverviewTile, type TrendPoint } from "../../overview";
import { mutedClass } from "../suites/parts";

const SPARK_W = 120;
const SPARK_H = 28;

/** A trend's means as a line, with the text alternative as its accessible name. */
export function Sparkline({ trend, days }: { trend: readonly TrendPoint[]; days: number }) {
  const words = trendWords(trend, days);
  const points = sparklinePoints(trend, SPARK_W, SPARK_H);
  return (
    <svg
      role="img"
      aria-label={words}
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      className="h-7 w-full overflow-visible text-slate-500 dark:text-slate-400"
    >
      <title>{words}</title>
      {points && (
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

/** One metric's tile: its mean coloured like the score tiles, the lowest value, how many series
 * have one, and the trend over the window. */
export function OverviewScoreTile({ tile, days }: { tile: OverviewTile; days: number }) {
  const label = metricLabel(tile.metric);
  const tone = tile.mean === null ? mutedClass : scoreTone(tile.mean);
  return (
    <li className="space-y-1 rounded-md border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <h3 className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">{label}</h3>
      <p className={`text-2xl font-semibold tabular-nums ${tone}`}>
        <span className="sr-only">mean </span>
        {scoreText(tile.mean)}
      </p>
      <p className={`text-xs ${mutedClass}`}>
        lowest {scoreText(tile.min)} · {formatNumber(tile.n_series)} series
      </p>
      <Sparkline trend={tile.trend} days={days} />
      <p className={`text-xs ${mutedClass}`} aria-hidden="true">
        {trendWords(tile.trend, days)}
      </p>
    </li>
  );
}

/** The eight score tiles, overall first. */
export function ScoreTiles({ tiles, days }: { tiles: readonly OverviewTile[]; days: number }) {
  return (
    <section aria-labelledby="overview-scores-heading" className="space-y-2">
      <h2 id="overview-scores-heading" className="text-base font-semibold">
        Scores
      </h2>
      <ul className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {tiles.map((tile) => (
          <OverviewScoreTile key={tile.metric} tile={tile} days={days} />
        ))}
      </ul>
    </section>
  );
}
