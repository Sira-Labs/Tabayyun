import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { ScoreTile, SeverityBadge, StatusBadge } from "../components/Badges";
import { Evidence } from "../components/Evidence";
import { FindingsTimeline } from "../components/FindingsTimeline";
import { useRunPolling } from "../hooks/useRunPolling";
import { formatDuration, formatLocalTime, formatNumber, severityRank } from "../format";
import { formatSpan, formatStep } from "../timeline";
import type { Finding, Run } from "../types";

const PROGRESS_TEXT: Record<string, string> = {
  queued: "Waiting for a worker to pick up the run…",
  running: "Running the checks…",
};

/** Most severe first, then earliest window. */
function byImportance(a: Finding, b: Finding): number {
  return (severityRank[a.severity] ?? 9) - (severityRank[b.severity] ?? 9) || a.window.start - b.window.start;
}

/** A request from the timeline to open and show one finding; `n` repeats a request. */
type Focus = { id: string; n: number } | null;

type RowProps = { finding: Finding; open: boolean; onToggle: () => void; focus: Focus };

/** One finding: a button row that expands its evidence panel. */
function FindingRow({ finding, open, onToggle, focus }: RowProps) {
  const panelId = useId();
  const button = useRef<HTMLButtonElement>(null);
  // Scroll to and focus the row the timeline asked for; the parent has already opened it.
  useEffect(() => {
    if (focus?.id !== finding.id) return;
    button.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    button.current?.focus({ preventScroll: true });
  }, [focus, finding.id]);
  return (
    <li className="border-t border-slate-200 first:border-t-0 dark:border-slate-800">
      <button
        ref={button}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
        className="grid w-full gap-1 p-3 text-left text-sm hover:bg-slate-50 sm:grid-cols-[6rem_11rem_12rem_1fr_4rem] sm:gap-3 dark:hover:bg-slate-800/60"
      >
        <span>
          <SeverityBadge severity={finding.severity} />
        </span>
        <span className="font-mono text-xs">{finding.check_id}</span>
        <span className="text-xs">
          {formatLocalTime(finding.window.start)}
          <span className="block text-slate-600 dark:text-slate-400">{formatDuration(finding.window.end - finding.window.start)}</span>
        </span>
        <span>
          {finding.summary}
          {(finding.occurrences > 1 || finding.status !== "open") && (
            <span className="block text-xs text-slate-600 dark:text-slate-400">
              {finding.occurrences > 1 && `seen in ${finding.occurrences} runs`}
              {finding.occurrences > 1 && finding.status !== "open" && " · "}
              {finding.status !== "open" && finding.status}
            </span>
          )}
        </span>
        <span className="tabular-nums sm:text-right">
          <span className="sm:sr-only">Impact </span>
          {finding.score_impact.toFixed(3)}
        </span>
      </button>
      <div id={panelId} hidden={!open} className="bg-slate-50 px-3 pb-3 pt-1 dark:bg-slate-950">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-400">Evidence</h3>
        <Evidence value={finding.evidence} />
      </div>
    </li>
  );
}

/** The run's findings: where they lie in time, then the sorted, expandable table. */
function FindingsOverview({ run }: { run: Run }) {
  const findings = useQuery({ queryKey: ["run-findings", run.id], queryFn: () => api.listRunFindings(run.id) });
  const [focus, setFocus] = useState<Focus>(null);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const toggle = (id: string) =>
    setOpened((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  if (findings.isPending) return <p>Loading findings…</p>;
  if (findings.isError) return <p role="alert">Could not load findings: {findings.error.message}</p>;
  const items = [...findings.data.items].sort(byImportance);
  const seriesNames = Object.fromEntries(run.series.map((s) => [s.id, s.external_id]));
  return (
    <>
      {run.window && (
        <FindingsTimeline
          findings={items}
          window={run.window}
          seriesNames={seriesNames}
          multiSeries={run.series.length > 1}
          onSelect={(id) => {
            setOpened((prev) => new Set(prev).add(id));
            setFocus((prev) => ({ id, n: (prev?.n ?? 0) + 1 }));
          }}
        />
      )}
      <Findings items={items} focus={focus} opened={opened} onToggle={toggle} />
    </>
  );
}

type FindingsProps = { items: Finding[]; focus: Focus; opened: ReadonlySet<string>; onToggle: (id: string) => void };

/** The findings table, sorted, each row expandable. */
function Findings({ items, focus, opened, onToggle }: FindingsProps) {
  return (
    <section aria-labelledby="findings-heading" className="space-y-2">
      <h2 id="findings-heading" className="text-base font-semibold">
        Findings ({items.length})
      </h2>
      {items.length === 0 ? (
        <p className="rounded-lg border border-slate-200 bg-white p-4 text-slate-600 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
          No findings. Every check that ran passed.
        </p>
      ) : (
        <div className="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <div aria-hidden="true" className="hidden grid-cols-[6rem_11rem_12rem_1fr_4rem] gap-3 bg-slate-100 p-3 text-xs uppercase tracking-wide text-slate-700 sm:grid dark:bg-slate-800 dark:text-slate-300">
            <span>Severity</span>
            <span>Check</span>
            <span>Window</span>
            <span>Finding</span>
            <span className="text-right">Impact</span>
          </div>
          <ul>
            {items.map((f) => (
              <FindingRow key={f.id} finding={f} open={opened.has(f.id)} onToggle={() => onToggle(f.id)} focus={focus} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** Overall and per-dimension score tiles from the run's score row. */
function Scores({ run }: { run: Run }) {
  const seriesId = run.series[0]?.id;
  const scores = useQuery({
    queryKey: ["scores", seriesId, run.id],
    queryFn: () => api.runScores(seriesId!, run.id),
    enabled: Boolean(seriesId),
  });
  const row = scores.data?.items.find((s) => s.run_id === run.id);
  // run.series[].score is stored on the run at completion, so it is this run's overall score.
  const overall = row?.overall ?? run.series[0]?.score;
  if (overall === undefined && !scores.isError) return null;
  return (
    <>
      {overall !== undefined && (
        <section aria-label="Scores" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <ScoreTile label="Overall" value={overall} />
          {Object.entries(row?.dimensions ?? {})
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([dimension, value]) => (
              <ScoreTile key={dimension} label={dimension} value={value} />
            ))}
        </section>
      )}
      {scores.isError && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not load the dimension scores: {scores.error.message}{" "}
          <button
            type="button"
            onClick={() => void scores.refetch()}
            className="font-medium underline underline-offset-2"
          >
            Retry
          </button>
        </p>
      )}
    </>
  );
}

/** Only http(s) links from stored metadata become clickable. */
function safeUrl(url: string | undefined): boolean {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

/** The series' stored metadata (unit, limits, sampling, source, counts). */
function SeriesPanel({ seriesId }: { seriesId: string }) {
  const series = useQuery({ queryKey: ["series", seriesId], queryFn: () => api.getSeries(seriesId) });
  if (!series.data) return null;
  const s = series.data;
  const example = s.metadata?.example;
  const limits =
    s.physical_min === null && s.physical_max === null
      ? "not set"
      : `${s.physical_min === null ? "−∞" : formatNumber(s.physical_min)} to ${s.physical_max === null ? "∞" : formatNumber(s.physical_max)}`;
  return (
    <section aria-labelledby="series-heading" className="rounded-lg border border-slate-200 bg-white p-4 text-sm dark:border-slate-800 dark:bg-slate-900">
      <h2 id="series-heading" className="mb-2 text-base font-semibold">
        Series{" "}
        <Link to="/series/$seriesId" params={{ seriesId: s.id }} className="text-sky-800 underline underline-offset-2 dark:text-sky-300">
          {s.name}
        </Link>
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        <dt className="text-slate-600 dark:text-slate-400">External id</dt>
        <dd className="font-mono text-xs">{s.external_id}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Unit</dt>
        <dd>{s.unit ?? "not set"}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Physical limits</dt>
        <dd>{limits}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Sampling</dt>
        <dd>{s.expected_interval_ns ? formatStep(s.expected_interval_ns) : "not set"}</dd>
        {example?.source && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Source</dt>
            <dd>
              {safeUrl(example.url) ? (
                <a href={example.url} target="_blank" rel="noreferrer" className="text-sky-800 underline underline-offset-2 dark:text-sky-300">
                  {example.source}
                </a>
              ) : (
                example.source
              )}
            </dd>
          </>
        )}
        {example?.licence && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Licence</dt>
            <dd>{example.licence}</dd>
          </>
        )}
        {example?.notes && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Notes</dt>
            <dd>{example.notes}</dd>
          </>
        )}
        <dt className="text-slate-600 dark:text-slate-400">Open findings</dt>
        <dd>{s.open_findings}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Runs</dt>
        <dd>{s.n_runs}</dd>
      </dl>
    </section>
  );
}

/** "2.0 years · hourly": the window's span and the sampling step (the count has its own row). */
function windowSummary(run: Run, declaredStepNs: number | null | undefined): string | null {
  if (!run.window) return null;
  const span = run.window.end - run.window.start;
  const n = run.stats.n_samples;
  const parts = [formatSpan(span)];
  // A dataset run's series may differ in step, even when the dataset holds only one today.
  const single = run.series.length <= 1 && !run.dataset_id;
  if (single && declaredStepNs) parts.push(formatStep(declaredStepNs));
  else if (single && n !== undefined && n >= 2 && span > 0) parts.push(`≈ ${formatStep(span / (n - 1))}`);
  return parts.join(" · ");
}

/** Run title, status and timing, window, samples and finding counts. */
function Header({ run }: { run: Run }) {
  const stats = run.stats;
  const seriesId = run.series.length === 1 ? run.series[0]!.id : undefined;
  const series = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => api.getSeries(seriesId!),
    enabled: Boolean(seriesId),
  });
  const summary = windowSummary(run, series.data?.expected_interval_ns);
  return (
    <header className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Run {run.series[0]?.external_id ?? run.id.slice(0, 8)}</h1>
        <StatusBadge status={run.status} />
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr_max-content_1fr]">
        <dt className="text-slate-600 dark:text-slate-400">Created</dt>
        <dd>{formatLocalTime(run.created_at)}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Duration</dt>
        <dd>{run.duration_ms === null ? "—" : formatDuration(run.duration_ms * 1_000_000)}</dd>
        {run.window && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Data window</dt>
            <dd>
              {formatLocalTime(run.window.start)} → {formatLocalTime(run.window.end)}
              {summary && <span className="block text-slate-600 dark:text-slate-400">{summary}</span>}
            </dd>
          </>
        )}
        {stats.n_samples !== undefined && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Samples</dt>
            <dd>
              {formatNumber(stats.n_samples)}
              {stats.ts_unit && <span className="text-slate-600 dark:text-slate-400"> · timestamps read as {stats.ts_unit}</span>}
            </dd>
          </>
        )}
        {stats.n_findings !== undefined && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Findings</dt>
            <dd>
              {stats.n_findings}
              {stats.n_findings_merged ? ` (${stats.n_findings_merged} already known)` : ""}
            </dd>
          </>
        )}
      </dl>
    </header>
  );
}

/** `/runs/$runId`: polls until the run is terminal, then shows the report. */
export function RunReport() {
  const { runId } = useParams({ from: "/_app/runs/$runId" });
  const run = useRunPolling(runId);

  if (run.isPending) return <p>Loading run…</p>;
  if (run.isError) {
    const missing = run.error instanceof ApiError && run.error.status === 404;
    return (
      <p role="alert" className="text-red-700 dark:text-red-400">
        {missing ? "Run not found." : `Could not load the run: ${run.error.message}`}{" "}
        <Link to="/runs" className="underline">
          Back to runs
        </Link>
      </p>
    );
  }
  const data = run.data;
  const skipped = data.stats.skipped_checks ?? [];

  return (
    <article className="space-y-6">
      <p aria-live="polite" className="sr-only">
        Run {data.status}
      </p>
      <Header run={data} />
      {PROGRESS_TEXT[data.status] && (
        <div className="space-y-2 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
          <p>{PROGRESS_TEXT[data.status]}</p>
          <div role="progressbar" aria-label="Run progress" aria-valuetext={data.status} className="h-1.5 overflow-hidden rounded bg-slate-200 dark:bg-slate-800">
            <div className="h-full w-1/3 animate-pulse rounded bg-sky-700" />
          </div>
        </div>
      )}
      {data.status === "failed" && (
        <div role="alert" className="space-y-2 rounded-lg border border-red-300 bg-red-50 p-4 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
          <p className="font-medium">The run failed.</p>
          <p className="font-mono text-sm">{data.error}</p>
          <Link to="/runs/new" className="inline-block underline">
            Try again
          </Link>
        </div>
      )}
      {data.status === "succeeded" && (
        <>
          <Scores run={data} />
          <FindingsOverview run={data} />
          {skipped.length > 0 && (
            <section aria-labelledby="skipped-heading" className="space-y-2">
              <h2 id="skipped-heading" className="text-base font-semibold">
                Skipped checks ({skipped.length})
              </h2>
              <ul className="list-disc pl-5 text-sm">
                {skipped.map((s) => (
                  <li key={s.check_id}>
                    <span className="font-mono text-xs">{s.check_id}</span> needs {s.missing}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {data.series[0] && <SeriesPanel seriesId={data.series[0].id} />}
        </>
      )}
    </article>
  );
}
