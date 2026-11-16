import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "../../api";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime, formatNumber } from "../../format";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { chartApi, type Baseline, type BaselineThreshold } from "../../seriesChart";
import { ErrorLine, panelClass } from "../admin/ui";

/** How often and for how long the panel looks for the new version after a refresh; tests shorten both. */
export const baselinePolling = { intervalMs: 2000, timeoutMs: 60_000 };

const mutedClass = "text-slate-600 dark:text-slate-400";
const DASH = "—";

/** `n thing` or `n things`, with the number grouped. */
function count(n: number, one: string, many = `${one}s`): string {
  return `${formatNumber(n)} ${n === 1 ? one : many}`;
}

/** The change of `value` against `previous` as a signed percentage, "no change", or a dash when
 * either is missing or the previous value is 0. */
export function changeText(value: number | null, previous: number | null | undefined): string {
  if (value === null || previous === null || previous === undefined || previous === 0) return DASH;
  if (value === previous) return "no change";
  const percent = ((value - previous) / Math.abs(previous)) * 100;
  const sign = percent > 0 ? "+" : "−";
  const size = Math.abs(percent);
  if (size < 0.05) return `${sign}<0.1%`;
  return `${sign}${size >= 10 ? Math.round(size).toLocaleString("en-US") : Number(size.toFixed(1))}%`;
}

/** What the baseline left out, or null when it left nothing out. */
export function excludedText(excluded: Baseline["excluded"]): string | null {
  if (excluded.bad === 0 && excluded.findings === 0) return null;
  const windows = count(excluded.windows.length, "open critical finding window");
  return `${count(excluded.bad, "bad-quality sample")} and ${count(excluded.findings, "sample")} inside ${windows} left out`;
}

/** What a failed refresh means for the user. */
function refreshErrorText(error: unknown): string | null {
  return error instanceof ApiError && error.status === 409 && error.message === "no_data"
    ? "The cache holds no data of this series yet."
    : null;
}

/** The series page's "Baseline" panel: the current version, what it was computed from, the
 * thresholds it gives against those of the version before, and "Refresh baseline" for editors
 * (spec 028). */
export function BaselinePanel({ seriesId }: { seriesId: string }) {
  const canWrite = atLeast(useWorkspaceRole(), "editor");
  // The version that was current when a refresh was queued (null: none), while the panel
  // waits for a newer one.
  const [watch, setWatch] = useState<{ since: number | null; timedOut: boolean } | null>(null);
  const baselines = useQuery({
    queryKey: ["series", seriesId, "baselines"],
    queryFn: () => chartApi.baselines(seriesId),
    refetchInterval: (q) => {
      const newest = q.state.data?.items[0]?.version ?? null;
      return watch && !watch.timedOut && newest === watch.since ? baselinePolling.intervalMs : false;
    },
  });
  const current = baselines.data?.items[0];
  const previous = baselines.data?.items[1];
  const version = current?.version ?? null;
  const refresh = useMutation({
    // Resolves to the version that was current at the click.
    mutationFn: (since: number | null) => chartApi.refreshBaseline(seriesId).then(() => since),
    onMutate: () => setWatch(null),
    onSuccess: (since) => setWatch({ since, timedOut: false }),
  });

  const updated = watch !== null && version !== watch.since;
  const waiting = watch !== null && !updated && !watch.timedOut;
  useEffect(() => {
    if (!waiting) return;
    const timer = setTimeout(() => setWatch((w) => w && { ...w, timedOut: true }), baselinePolling.timeoutMs);
    return () => clearTimeout(timer);
  }, [waiting]);

  let status = "";
  if (updated && version !== null) status = `Baseline updated to version ${version}.`;
  else if (waiting) status = "Baseline is being computed…";
  else if (watch?.timedOut) {
    status =
      watch.since === null
        ? "No baseline was computed: there is too little usable data."
        : `No new version: nothing changed since version ${watch.since}, or there is too little usable data.`;
  }
  const noData = refreshErrorText(refresh.error);

  return (
    <section aria-labelledby="baseline-heading" className={`${panelClass} space-y-2 text-sm`}>
      <h2 id="baseline-heading" className="text-base font-semibold">
        Baseline
      </h2>
      <ErrorLine error={baselines.error} prefix="Could not load the baseline" />
      {baselines.isPending && <p className={mutedClass}>Loading…</p>}
      {current && <Current baseline={current} />}
      {current && <Thresholds current={current} previous={previous} />}
      {baselines.data && !current && <p className={mutedClass}>No baseline yet. Runs judge this series against the window they check.</p>}
      {canWrite && baselines.data && (
        <button type="button" className={buttonClass} disabled={refresh.isPending || waiting} onClick={() => refresh.mutate(version)}>
          {current ? "Refresh baseline" : "Compute baseline"}
        </button>
      )}
      <p role="status" aria-live="polite" className={status ? "" : "sr-only"}>
        {status}
      </p>
      {noData ? (
        <p role="alert" className="text-amber-800 dark:text-amber-300">
          {noData}
        </p>
      ) : (
        <ErrorLine error={refresh.error} prefix="Could not refresh the baseline" />
      )}
    </section>
  );
}

/** Version, when and why it was computed, its window and what it used and left out. */
function Current({ baseline: b }: { baseline: Baseline }) {
  const excluded = excludedText(b.excluded);
  return (
    <div className="space-y-1">
      <p>
        <span className="font-medium">Version {b.version}</span>, computed {formatLocalTime(b.created_at)} (
        {b.trigger === "manual" ? "refreshed by hand" : "scheduled refresh"})
      </p>
      <p className={`text-xs ${mutedClass}`}>
        {formatLocalTime(Number(b.window.from))} to {formatLocalTime(Number(b.window.to))}
      </p>
      <p>
        {count(b.n_samples, "sample")} used{excluded ? `; ${excluded}` : ""}.
      </p>
    </div>
  );
}

const sameParam = (a: BaselineThreshold) => (b: BaselineThreshold) => a.check_id === b.check_id && a.param === b.param;

/** The learned thresholds, each against its value in the version before. A list, not a
 * table: the panel sits in the page's narrow side column, where the previous value and the
 * change must be readable without scrolling sideways. */
function Thresholds({ current, previous }: { current: Baseline; previous: Baseline | undefined }) {
  if (current.thresholds.length === 0) return <p className={mutedClass}>This baseline gives no learned thresholds.</p>;
  const label = `Learned thresholds of version ${current.version}${previous ? `, against version ${previous.version}` : ""}`;
  return (
    <div>
      <h3 id="baseline-thresholds-heading" className="mb-1 font-semibold">
        Learned thresholds
      </h3>
      <ul aria-label={label} className="divide-y divide-slate-200 dark:divide-slate-800">
        {current.thresholds.map((t) => {
          const before = previous?.thresholds.find(sameParam(t))?.value;
          const change = changeText(t.value, before);
          return (
            <li key={`${t.check_id} ${t.param}`} className="space-y-0.5 py-2">
              <p className="flex flex-wrap items-baseline gap-x-2">
                <span data-part="check" className="font-mono text-xs">
                  {t.check_id}
                </span>
                <span data-part="param">{t.param}</span>
              </p>
              <p className="flex flex-wrap items-baseline gap-x-2 tabular-nums">
                <span data-part="value" className="text-base font-medium">
                  {t.value === null ? DASH : formatNumber(t.value)}
                </span>
                {previous && (
                  <span className={mutedClass}>
                    was <span data-part="previous">{before === null || before === undefined ? DASH : formatNumber(before)}</span>
                    {change !== DASH && (
                      <>
                        {" "}
                        (<span data-part="change">{change}</span>)
                      </>
                    )}
                  </span>
                )}
              </p>
              <p data-part="basis" className={`text-xs ${mutedClass}`}>
                {t.basis}
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
