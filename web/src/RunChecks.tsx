import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ApiError } from "./api";
import { formatDuration, formatTs, severityClass } from "./format";
import type { CheckReport } from "./types";

async function postRun(form: FormData): Promise<CheckReport> {
  const res = await fetch("/api/checks/run", {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-Tabayyun-Request": "1" },
    body: form,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as CheckReport;
}

function ScoreTile({ label, value }: { label: string; value: number }) {
  const tone = value >= 90 ? "text-emerald-600 dark:text-emerald-400" : value >= 70 ? "text-amber-600 dark:text-amber-400" : "text-red-600 dark:text-red-400";
  return (
    <div className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${tone}`}>{value.toFixed(1)}</div>
    </div>
  );
}

export function RunChecks() {
  const [report, setReport] = useState<CheckReport | null>(null);
  const run = useMutation({ mutationFn: postRun, onSuccess: setReport });

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    run.mutate(new FormData(e.currentTarget));
  }

  return (
    <section className="space-y-6">
      <form onSubmit={onSubmit} className="grid gap-3 rounded-lg border border-slate-200 bg-white p-4 sm:grid-cols-2 dark:border-slate-800 dark:bg-slate-900">
        <label className="flex flex-col gap-1 text-sm sm:col-span-2">
          <span>CSV file (columns: timestamp, value, optional quality)</span>
          <input name="file" type="file" accept=".csv,text/csv" required className="rounded border border-slate-300 p-2 dark:border-slate-700" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Series id</span>
          <input name="series_id" defaultValue="uploaded" className="rounded border border-slate-300 p-2 dark:border-slate-700 dark:bg-slate-950" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Unit (enables physical limits, e.g. %, m3/h)</span>
          <input name="unit" placeholder="optional" className="rounded border border-slate-300 p-2 dark:border-slate-700 dark:bg-slate-950" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Timestamp column</span>
          <input name="ts_col" defaultValue="ts" className="rounded border border-slate-300 p-2 dark:border-slate-700 dark:bg-slate-950" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Value column</span>
          <input name="value_col" defaultValue="value" className="rounded border border-slate-300 p-2 dark:border-slate-700 dark:bg-slate-950" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Quality column</span>
          <input name="quality_col" placeholder="optional" className="rounded border border-slate-300 p-2 dark:border-slate-700 dark:bg-slate-950" />
        </label>
        <div className="flex items-end">
          <button type="submit" disabled={run.isPending} className="min-h-11 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900">
            {run.isPending ? "Running…" : "Run checks"}
          </button>
        </div>
        {run.isError && (
          <p role="alert" className="text-sm text-red-600 sm:col-span-2 dark:text-red-400">
            {run.error.message}
          </p>
        )}
      </form>

      {report && (
        <div className="space-y-4" aria-live="polite">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <ScoreTile label="Overall" value={report.score.overall} />
            {Object.entries(report.score.dimensions)
              .filter(([, v]) => v < 100)
              .slice(0, 7)
              .map(([k, v]) => (
                <ScoreTile key={k} label={k} value={v} />
              ))}
          </div>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            {report.n_samples.toLocaleString()} samples from {formatTs(report.window.start)} to {formatTs(report.window.end)} ·{" "}
            {report.findings.length} findings
            {report.skipped.length > 0 && ` · skipped: ${report.skipped.map((s) => `${s.check_id} (needs ${s.missing})`).join(", ")}`}
          </p>
          <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                <tr>
                  <th className="p-2">Severity</th>
                  <th className="p-2">Check</th>
                  <th className="p-2">Window</th>
                  <th className="p-2">Finding</th>
                  <th className="p-2 text-right">Impact</th>
                </tr>
              </thead>
              <tbody>
                {report.findings.map((f, i) => (
                  <tr key={i} className="border-t border-slate-200 align-top dark:border-slate-800">
                    <td className="p-2">
                      <span className={`rounded px-2 py-0.5 text-xs font-medium ${severityClass[f.severity] ?? ""}`}>{f.severity}</span>
                    </td>
                    <td className="p-2 font-mono text-xs">{f.check_id}</td>
                    <td className="p-2 whitespace-nowrap text-xs">
                      {formatTs(f.window.start)}
                      <br />
                      <span className="text-slate-500">{formatDuration(f.window.end - f.window.start)}</span>
                    </td>
                    <td className="p-2">
                      <details>
                        <summary className="cursor-pointer">{f.summary}</summary>
                        <pre className="mt-2 max-w-xl overflow-x-auto rounded bg-slate-100 p-2 text-xs dark:bg-slate-800">{JSON.stringify(f.evidence, null, 2)}</pre>
                      </details>
                    </td>
                    <td className="p-2 text-right tabular-nums">{f.score_impact.toFixed(3)}</td>
                  </tr>
                ))}
                {report.findings.length === 0 && (
                  <tr>
                    <td colSpan={5} className="p-4 text-center text-slate-500">
                      No findings. Every check passed.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
