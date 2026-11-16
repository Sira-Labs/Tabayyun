import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { ApiError } from "../../api";
import { StatusBadge } from "../../components/Badges";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime } from "../../format";
import { TERMINAL_STATUSES } from "../../hooks/useRunPolling";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { valueText } from "../../suiteParams";
import { suitesApi, type Suite, type SuiteCheck } from "../../suites";
import type { Page } from "../../types";
import { ErrorLine, dangerButtonClass, dialogs, linkButtonClass, panelClass } from "../admin/ui";
import { DatasetName, RunLink, ScheduleText, SourceBadge, mutedClass } from "./parts";

const detailKey = (id: string) => ["suites", id, "detail"];
const runsKey = (id: string) => ["suites", id, "runs"];

/** `/suites/:id`: a suite's configuration, thresholds with their source, actions and run
 * history (spec 027). Viewers see everything but the actions. */
export function SuiteDetail() {
  const { suiteId } = useParams({ from: "/_app/suites/$suiteId" });
  const role = useWorkspaceRole();
  const suite = useQuery({
    queryKey: detailKey(suiteId),
    queryFn: () => suitesApi.get(suiteId),
    // "Last run" follows a run that is still queued or running, like the history below.
    refetchInterval: (q) => {
      const last = q.state.data?.last_run;
      return last && !TERMINAL_STATUSES.has(last.status) ? 5000 : false;
    },
  });
  const canWrite = atLeast(role, "editor");

  if (suite.isPending) return <p>Loading suite…</p>;
  if (suite.isError) {
    const missing = suite.error instanceof ApiError && suite.error.status === 404;
    return missing ? <p role="alert">This suite does not exist (any more).</p> : <ErrorLine error={suite.error} prefix="Could not load the suite" />;
  }
  const s = suite.data;
  return (
    <section aria-labelledby="suite-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="suite-heading" className="flex flex-wrap items-baseline gap-2 text-lg font-semibold">
          {s.name}
          {!s.enabled && <span className={`text-sm font-normal ${mutedClass}`}>(disabled)</span>}
        </h1>
        <Link to="/suites" className="text-sm underline">
          All suites
        </Link>
      </div>
      <Configuration suite={s} />
      {canWrite && <Actions suite={s} />}
      <Checks checks={s.checks} />
      <History suiteId={s.id} />
    </section>
  );
}

/** Dataset, schedule and the runs before and after now. */
function Configuration({ suite }: { suite: Suite }) {
  const rows: [string, React.ReactNode][] = [
    ["Dataset", <DatasetName suite={suite} />],
    [
      "Schedule",
      <>
        <ScheduleText schedule={suite.schedule} />
        {suite.schedule && <code className={`ml-2 text-xs ${mutedClass}`}>{suite.schedule}</code>}
      </>,
    ],
    ["Next run", suite.next_run_at ? formatLocalTime(suite.next_run_at) : suite.schedule && !suite.enabled ? "— (disabled)" : "—"],
    ["Last run", suite.last_run ? <RunLink run={suite.last_run} /> : "—"],
  ];
  return (
    <section aria-labelledby="suite-config-heading" className={`${panelClass} space-y-2`}>
      <h2 id="suite-config-heading" className="font-semibold">
        Configuration
      </h2>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[8rem_1fr]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className={mutedClass}>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {suite.dataset_id === null && (
        <p className="text-sm text-amber-800 dark:text-amber-300">
          The dataset was deleted. The schedule skips this suite until it is given a dataset.
        </p>
      )}
    </section>
  );
}

/** What a failed "Run now" means for the user. */
function runErrorText(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError && error.status === 409 && error.message === "no_dataset") {
    return "This suite has no dataset, so there is nothing to run. Edit the suite and choose one.";
  }
  return `Could not start a run: ${error instanceof Error ? error.message : String(error)}`;
}

/** Run now, enable or disable, edit and delete (editors). */
function Actions({ suite }: { suite: Suite }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: detailKey(suite.id) });
    void client.invalidateQueries({ queryKey: ["suites", "list"] });
  };
  const run = useMutation({
    mutationFn: () => suitesApi.runNow(suite.id),
    onSuccess: () => {
      refresh();
      void client.invalidateQueries({ queryKey: runsKey(suite.id) });
    },
  });
  const toggle = useMutation({ mutationFn: () => suitesApi.update(suite.id, { enabled: !suite.enabled }), onSuccess: refresh });
  const remove = useMutation({
    mutationFn: () => suitesApi.remove(suite.id),
    onSuccess: () => {
      client.removeQueries({ queryKey: detailKey(suite.id) });
      // The list must not show the deleted suite while it reloads.
      client.setQueryData<Page<Suite>>(
        ["suites", "list"],
        (page) => page && { ...page, items: page.items.filter((row) => row.id !== suite.id) },
      );
      void client.invalidateQueries({ queryKey: ["suites", "list"] });
      void navigate({ to: "/suites" });
    },
  });
  const runError = runErrorText(run.error);

  return (
    <section aria-labelledby="suite-actions-heading" className={`${panelClass} space-y-3`}>
      <h2 id="suite-actions-heading" className="font-semibold">
        Actions
      </h2>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={buttonClass} disabled={run.isPending} onClick={() => run.mutate()}>
          Run now
        </button>
        <Link to="/suites/$suiteId/edit" params={{ suiteId: suite.id }} className={buttonClass}>
          Edit
        </Link>
        <button type="button" className={linkButtonClass} disabled={toggle.isPending} onClick={() => toggle.mutate()}>
          {suite.enabled ? "Disable" : "Enable"}
        </button>
        <button
          type="button"
          className={dangerButtonClass}
          disabled={remove.isPending}
          onClick={() => dialogs.confirm(`Delete the suite "${suite.name}"? Its runs stay.`) && remove.mutate()}
        >
          Delete
        </button>
      </div>
      {run.data && (
        <p className="flex flex-wrap items-center gap-2 text-sm" aria-live="polite">
          Run started: <RunLink run={run.data} />
        </p>
      )}
      {runError && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {runError}
        </p>
      )}
      <ErrorLine error={toggle.error} prefix={`Could not ${suite.enabled ? "disable" : "enable"} the suite`} />
      <ErrorLine error={remove.error} prefix="Could not delete the suite" />
    </section>
  );
}

/** A threshold's value: what applies, or a dash when it is decided at run time. */
function thresholdValue(value: unknown): string {
  return value === null || value === undefined ? "—" : valueText(value);
}

/** The thresholds of one check that runs. */
function CheckThresholds({ check }: { check: SuiteCheck }) {
  return (
    <div className="space-y-1">
      <h3 className="flex flex-wrap items-baseline gap-2 text-sm font-medium">
        <span className="font-mono">{check.id}</span>
        <span className={`text-xs font-normal ${mutedClass}`}>{check.scope === "group" ? "across series" : "per series"}</span>
      </h3>
      {check.thresholds.length === 0 ? (
        <p className={`text-sm ${mutedClass}`}>No parameters.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Thresholds of {check.id}</caption>
            <thead className={`text-xs uppercase tracking-wide ${mutedClass}`}>
              <tr>
                <th scope="col" className="w-1/4 p-1">Parameter</th>
                <th scope="col" className="w-1/4 p-1">Value</th>
                <th scope="col" className="w-1/6 p-1">Source</th>
                <th scope="col" className="p-1">Note</th>
              </tr>
            </thead>
            <tbody>
              {check.thresholds.map((t) => (
                <tr key={t.param} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="p-1 font-mono text-xs">{t.param}</td>
                  <td className="p-1 font-mono text-xs tabular-nums">{thresholdValue(t.value)}</td>
                  <td className="p-1">
                    <SourceBadge source={t.source} />
                  </td>
                  <td className={`p-1 text-xs ${mutedClass}`}>{t.note ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** The checks that run, each with its thresholds and where they come from; then those that are off. */
function Checks({ checks }: { checks: SuiteCheck[] }) {
  const on = checks.filter((c) => c.enabled);
  const off = checks.filter((c) => !c.enabled);
  return (
    <section aria-labelledby="suite-checks-heading" className={`${panelClass} space-y-4`}>
      <h2 id="suite-checks-heading" className="font-semibold">
        Checks and thresholds
      </h2>
      {on.length === 0 && <p className={`text-sm ${mutedClass}`}>No check is switched on.</p>}
      {on.map((check) => (
        <CheckThresholds key={check.id} check={check} />
      ))}
      {off.length > 0 && (
        <p className={`text-sm ${mutedClass}`}>
          Switched off: <span className="font-mono text-xs">{off.map((c) => c.id).join(", ")}</span>
        </p>
      )}
    </section>
  );
}

/** The suite's runs, newest first, each linking to its report. */
function History({ suiteId }: { suiteId: string }) {
  const runs = useInfiniteQuery({
    queryKey: runsKey(suiteId),
    queryFn: ({ pageParam }) => suitesApi.runs(suiteId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // Keep the history moving while a run is still queued or running.
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((r) => !TERMINAL_STATUSES.has(r.status))) ? 5000 : false,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section aria-labelledby="suite-history-heading" className={`${panelClass} space-y-2`}>
      <h2 id="suite-history-heading" className="font-semibold">
        Run history
      </h2>
      {runs.isPending && <p>Loading runs…</p>}
      <ErrorLine error={runs.error} prefix="Could not load the runs" />
      {runs.isSuccess && items.length === 0 && <p className={`text-sm ${mutedClass}`}>No runs yet.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">The suite's runs, newest first</caption>
            <thead className={`text-xs uppercase tracking-wide ${mutedClass}`}>
              <tr>
                <th scope="col" className="p-1">Status</th>
                <th scope="col" className="p-1">Started</th>
                <th scope="col" className="p-1">Trigger</th>
                <th scope="col" className="p-1 text-right">Findings</th>
                <th scope="col" className="p-1">Report</th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="p-1">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="p-1 text-xs sm:whitespace-nowrap">{formatLocalTime(run.started_at ?? run.created_at)}</td>
                  <td className="p-1 text-xs">{run.trigger}</td>
                  <td className="p-1 text-right tabular-nums">{run.stats.n_findings ?? "—"}</td>
                  <td className="p-1">
                    <Link to="/runs/$runId" params={{ runId: run.id }} className="underline underline-offset-2">
                      Open report
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {runs.hasNextPage && (
        <button type="button" className={buttonClass} disabled={runs.isFetchingNextPage} onClick={() => void runs.fetchNextPage()}>
          {runs.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}
    </section>
  );
}
