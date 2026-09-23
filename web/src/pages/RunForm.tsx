import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import type { FormEvent } from "react";
import { api } from "../api";

const inputClass = "rounded border border-slate-400 bg-white p-2 dark:border-slate-600 dark:bg-slate-950";
// Optional fields: an empty string is not a value for the API (a float field would be a 422).
const OPTIONAL = ["unit", "quality_col", "physical_min", "physical_max"];

/** `/runs/new`: upload a CSV, start a run and open its report. */
export function RunForm() {
  const navigate = useNavigate();
  const create = useMutation({
    mutationFn: api.createRun,
    onSuccess: (run) => navigate({ to: "/runs/$runId", params: { runId: run.id } }),
  });

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    for (const key of OPTIONAL) if (form.get(key) === "") form.delete(key);
    create.mutate(form);
  }

  return (
    <section aria-labelledby="new-run-heading" className="space-y-4">
      <h1 id="new-run-heading" className="text-lg font-semibold">
        New run
      </h1>
      <form onSubmit={onSubmit} className="grid gap-3 rounded-lg border border-slate-200 bg-white p-4 sm:grid-cols-2 dark:border-slate-800 dark:bg-slate-900">
        <label className="flex flex-col gap-1 text-sm sm:col-span-2">
          <span>CSV file (columns: timestamp, value, optional quality)</span>
          <input name="file" type="file" accept=".csv,text/csv" required className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Series id</span>
          <input name="series_id" defaultValue="uploaded" required className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Unit (enables physical limits, e.g. %, m3/h)</span>
          <input name="unit" placeholder="optional" className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Timestamp column</span>
          <input name="ts_col" defaultValue="ts" className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Epoch timestamp unit</span>
          <select name="ts_unit" defaultValue="auto" className={inputClass}>
            <option value="auto">Detect (text dates are read as written)</option>
            <option value="s">Seconds</option>
            <option value="ms">Milliseconds</option>
            <option value="us">Microseconds</option>
            <option value="ns">Nanoseconds</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Value column</span>
          <input name="value_col" defaultValue="value" className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Quality column</span>
          <input name="quality_col" placeholder="optional" className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Physical minimum (range check)</span>
          <input name="physical_min" type="number" step="any" inputMode="decimal" placeholder="optional" className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>Physical maximum (range check)</span>
          <input name="physical_max" type="number" step="any" inputMode="decimal" placeholder="optional" className={inputClass} />
        </label>
        <p className="text-xs text-slate-600 sm:col-span-2 dark:text-slate-400">
          Limits entered here are saved on the series and used by later uploads of the same series id.
        </p>
        <div className="flex items-end">
          <button type="submit" disabled={create.isPending} className="min-h-11 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900">
            {create.isPending ? "Uploading…" : "Start run"}
          </button>
        </div>
        {create.isError && (
          <p role="alert" className="text-sm text-red-700 sm:col-span-2 dark:text-red-400">
            {create.error.message}
          </p>
        )}
      </form>
    </section>
  );
}
