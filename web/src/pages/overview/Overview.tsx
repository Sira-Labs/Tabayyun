import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { formatNumber } from "../../format";
import { overviewApi, overviewKeys, validateOverviewSearch, type Overview as OverviewData, type OverviewSearch } from "../../overview";
import { ErrorLine } from "../admin/ui";
import { mutedClass } from "../suites/parts";
import { FindingsPerDay } from "./FindingsChart";
import { OverviewFilters } from "./Filters";
import { SourcesStrip } from "./Sources";
import { ScoreTiles } from "./Tiles";
import { WorstSeries } from "./Worst";

/** Nothing scored yet in the workspace: where to start. */
function NothingScored() {
  return (
    <p>
      Nothing was scored in this workspace yet.{" "}
      <Link to="/runs/new" className="font-medium underline">
        New run
      </Link>
    </p>
  );
}

/** The overview's parts once the data is there. */
function OverviewBody({ data, filtered }: { data: OverviewData; filtered: boolean }) {
  if (data.series.scored === 0 && !filtered) {
    return (
      <>
        <NothingScored />
        <SourcesStrip sources={data.sources} />
      </>
    );
  }
  return (
    <>
      <p className={`text-sm ${mutedClass}`}>
        {formatNumber(data.series.scored)} of {formatNumber(data.series.total)} series scored, {data.window.start} to {data.window.end} (UTC)
      </p>
      <ScoreTiles tiles={data.tiles} days={data.window.days} />
      <WorstSeries worst={data.worst} />
      <FindingsPerDay findings={data.findings} />
      <SourcesStrip sources={data.sources} />
    </>
  );
}

/** `/`: how the workspace's data is doing (spec 032): score tiles with their trend, the worst
 * series, new findings per day and the sources' health, filtered through the URL. */
export function Overview() {
  // The router merges the root's raw search into this route's: keep the known keys only.
  const search = validateOverviewSearch(useSearch({ from: "/_app/" }) as Record<string, unknown>);
  const navigate = useNavigate({ from: "/" });
  const overview = useQuery({
    queryKey: overviewKeys.get(search),
    queryFn: () => overviewApi.get(search),
    placeholderData: keepPreviousData,
  });
  const change = (part: OverviewSearch) =>
    void navigate({ search: (prev: OverviewSearch) => validateOverviewSearch({ ...prev, ...part }), replace: true });
  const filtered = search.asset !== undefined || search.source !== undefined;

  return (
    <section aria-labelledby="overview-heading" className="space-y-6">
      <h1 id="overview-heading" className="text-lg font-semibold">
        Overview
      </h1>
      <OverviewFilters search={search} onChange={change} />
      {overview.isPending && <p>Loading the overview…</p>}
      <ErrorLine error={overview.error} prefix="Could not load the overview" />
      {overview.data && <OverviewBody data={overview.data} filtered={filtered} />}
    </section>
  );
}
