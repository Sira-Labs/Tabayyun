import { Link } from "@tanstack/react-router";
import { formatLocalTime, formatNumber } from "../../format";
import { lastFetchAt, type OverviewSource } from "../../overview";
import { TYPE_LABEL } from "../../sources";
import { HealthBadge } from "../sources/parts";
import { mutedClass } from "../suites/parts";

/** One source as a chip: name, health and series count, linking to the source. */
function SourceChip({ source }: { source: OverviewSource }) {
  const at = lastFetchAt(source);
  const fetched = at ? `last fetch ${formatLocalTime(at)}` : "never fetched";
  return (
    <li>
      <Link
        to="/sources/$sourceId"
        params={{ sourceId: source.id }}
        title={`${TYPE_LABEL[source.type] ?? source.type} · ${fetched}`}
        className={`flex flex-wrap items-center gap-2 rounded-full border px-3 py-1 text-sm hover:bg-slate-100 dark:hover:bg-slate-800 ${
          source.enabled
            ? "border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-900"
            : "border-dashed border-slate-400 bg-slate-50 text-slate-600 dark:border-slate-600 dark:bg-slate-950 dark:text-slate-400"
        }`}
      >
        <span className="font-medium">{source.name}</span>
        <HealthBadge status={source.status} />
        <span className={`text-xs tabular-nums ${mutedClass}`}>{formatNumber(source.n_series)} series</span>
        {!source.enabled && <span className="rounded border border-slate-400 px-1 text-xs dark:border-slate-500">disabled</span>}
      </Link>
    </li>
  );
}

/** "Sources": every source of the workspace with its health. */
export function SourcesStrip({ sources }: { sources: readonly OverviewSource[] }) {
  return (
    <section aria-labelledby="overview-sources-heading" className="space-y-2">
      <h2 id="overview-sources-heading" className="text-base font-semibold">
        Sources
      </h2>
      {sources.length === 0 ? (
        <p className={`text-sm ${mutedClass}`}>No sources yet.</p>
      ) : (
        <ul aria-label="Sources and their health" className="flex flex-wrap gap-2">
          {sources.map((s) => (
            <SourceChip key={s.id} source={s} />
          ))}
        </ul>
      )}
    </section>
  );
}
