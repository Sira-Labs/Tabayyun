// The form of a new or existing suite (spec 027): name, dataset, schedule, enabled, checks.
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { buttonClass } from "../../components/Brand";
import { checksOf, draftOf } from "../../suiteParams";
import { SCHEDULE_PRESETS, describeCron, hasFiveFields } from "../../suiteSchedule";
import { suiteErrors, suitesApi, type CatalogueCheck, type Dataset, type Suite, type SuiteInput } from "../../suites";
import { ErrorLine, inputClass, panelClass, selectClass } from "../admin/ui";
import { CheckFields } from "./CheckFields";
import { FieldError, mutedClass } from "./parts";

type LocalErrors = { schedule?: string; checks?: string; params: Record<string, string> };

/** The catalogue and the datasets the form needs, loaded once for both form pages. */
export function useSuiteFormData() {
  const catalogue = useQuery({ queryKey: ["checks", "catalogue"], queryFn: suitesApi.catalogue, staleTime: 300_000 });
  const datasets = useQuery({ queryKey: ["datasets", "all"], queryFn: suitesApi.datasets });
  return { catalogue, datasets };
}

/** "Loading" and load errors of the form's data, or nothing once both are there. */
export function FormDataStatus({ data }: { data: ReturnType<typeof useSuiteFormData> }) {
  return (
    <>
      {(data.catalogue.isPending || data.datasets.isPending) && <p>Loading…</p>}
      <ErrorLine error={data.catalogue.error} prefix="Could not load the checks" />
      <ErrorLine error={data.datasets.error} prefix="Could not load the datasets" />
    </>
  );
}

type Props = {
  catalogue: CatalogueCheck[];
  datasets: Dataset[];
  /** The suite being edited; absent for a new one. */
  suite?: Suite;
  submitLabel: string;
  pending: boolean;
  /** The failed create or update, shown at its fields. */
  error: unknown;
  onSubmit: (input: SuiteInput) => void;
};

/** The suite form. It checks only what it can without the API (five cron fields, parameter
 * values of their kind, one check on); the API's 422 and 409 are shown at their fields. */
export function SuiteForm({ catalogue, datasets, suite, submitLabel, pending, error, onSubmit }: Props) {
  const [name, setName] = useState(suite?.name ?? "");
  const [datasetId, setDatasetId] = useState(suite?.dataset_id ?? "");
  const [cron, setCron] = useState(suite?.schedule ?? "");
  const [enabled, setEnabled] = useState(suite?.enabled ?? true);
  const [draft, setDraft] = useState(() => draftOf(catalogue, suite?.checks ?? null));
  const [local, setLocal] = useState<LocalErrors>({ params: {} });
  const api = suiteErrors(error);
  const schedule = cron.trim();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const { checks, errors } = checksOf(catalogue, draft);
    const next: LocalErrors = { params: errors };
    if (schedule && !hasFiveFields(schedule)) next.schedule = "A schedule has five fields: minute, hour, day of month, month, weekday.";
    if (!checks.some((c) => c.enabled)) next.checks = "Switch on at least one check.";
    setLocal(next);
    if (next.schedule || next.checks || Object.keys(errors).length > 0) return;
    onSubmit({ name: name.trim(), dataset_id: datasetId, schedule: schedule || null, enabled, checks });
  };

  const scheduleError = local.schedule ?? api.fields.schedule;
  const paramErrors = Object.keys(local.params).length;
  return (
    <form onSubmit={submit} className="space-y-4">
      <div className={`${panelClass} space-y-4`}>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1 text-sm">
            <label htmlFor="suite-name">Name</label>
            <input
              id="suite-name"
              required
              maxLength={256}
              className={inputClass}
              value={name}
              aria-invalid={api.fields.name ? true : undefined}
              aria-describedby={api.fields.name ? "suite-name-error" : undefined}
              onChange={(e) => setName(e.target.value)}
            />
            <FieldError id="suite-name-error" message={api.fields.name} />
          </div>
          <div className="flex flex-col gap-1 text-sm">
            <label htmlFor="suite-dataset">Dataset</label>
            <select
              id="suite-dataset"
              required
              className={`${selectClass} min-h-11`}
              value={datasetId}
              aria-invalid={api.fields.dataset_id ? true : undefined}
              aria-describedby={api.fields.dataset_id ? "suite-dataset-error" : undefined}
              onChange={(e) => setDatasetId(e.target.value)}
            >
              <option value="">Choose a dataset…</option>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.series.length} series)
                </option>
              ))}
            </select>
            {datasets.length === 0 && (
              <span className={`text-xs ${mutedClass}`}>
                No datasets yet. A dataset names the series and the window a suite checks; create one with the API
                (POST /api/datasets) first.
              </span>
            )}
            {suite && suite.dataset_id === null && datasets.length > 0 && (
              <span className={`text-xs ${mutedClass}`}>The suite's dataset was deleted; choose another.</span>
            )}
            <FieldError id="suite-dataset-error" message={api.fields.dataset_id} />
          </div>
        </div>
        <div className="space-y-2">
          <div role="group" aria-label="Schedule presets" className="flex flex-wrap gap-2">
            {SCHEDULE_PRESETS.map((preset) => (
              <button
                key={preset.label}
                type="button"
                aria-pressed={schedule === preset.cron}
                className={`${buttonClass} min-h-9 px-3 py-1 aria-pressed:border-slate-900 aria-pressed:bg-slate-100 dark:aria-pressed:border-slate-100 dark:aria-pressed:bg-slate-800`}
                onClick={() => setCron(preset.cron)}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-1 text-sm sm:max-w-sm">
            <label htmlFor="suite-schedule">Schedule (cron, UTC)</label>
            <input
              id="suite-schedule"
              className={`${inputClass} font-mono`}
              placeholder="5 * * * *"
              value={cron}
              aria-invalid={scheduleError ? true : undefined}
              aria-describedby={scheduleError ? "suite-schedule-error suite-schedule-words" : "suite-schedule-words"}
              onChange={(e) => setCron(e.target.value)}
            />
            <span id="suite-schedule-words" className={`text-xs ${mutedClass}`}>
              {schedule ? describeCron(schedule) : "Only on request: the suite runs when someone starts it."}
            </span>
            <FieldError id="suite-schedule-error" message={scheduleError} />
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Enabled (a disabled suite is not started by its schedule)
        </label>
      </div>
      <section aria-labelledby="suite-checks-heading" className={`${panelClass} space-y-3`}>
        <h2 id="suite-checks-heading" className="font-semibold">
          Checks
        </h2>
        <p className={`text-sm ${mutedClass}`}>
          Checks that are off do not run. An empty parameter uses the check's default.
        </p>
        <FieldError id="suite-checks-error" message={local.checks ?? api.fields.checks} />
        <CheckFields catalogue={catalogue} draft={draft} errors={local.params} onChange={setDraft} />
      </section>
      {paramErrors > 0 && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {paramErrors === 1 ? "One parameter is not valid." : `${paramErrors} parameters are not valid.`}
        </p>
      )}
      {api.other && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          Could not save the suite: {api.other}
        </p>
      )}
      <button type="submit" className={buttonClass} disabled={pending}>
        {submitLabel}
      </button>
    </form>
  );
}
