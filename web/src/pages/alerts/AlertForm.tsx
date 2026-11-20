// The form of a new or existing alert rule (spec 031): what it watches, where it sends, how often.
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import {
  DIMENSIONS,
  METRICS,
  SEVERITIES,
  alertErrors,
  alertKeys,
  alertsApi,
  formOf,
  validateForm,
  type AlertChannels,
  type AlertField,
  type AlertFormModel,
  type AlertRule,
} from "../../alerts";
import { buttonClass } from "../../components/Brand";
import { suitesApi, type CatalogueCheck } from "../../suites";
import { ErrorLine, inputClass, panelClass, selectClass } from "../admin/ui";
import { FieldError, mutedClass } from "../suites/parts";

/** The checks catalogue and the server's channels, loaded once for both form pages. */
export function useAlertFormData() {
  const catalogue = useQuery({ queryKey: ["checks", "catalogue"], queryFn: suitesApi.catalogue, staleTime: 300_000 });
  const channels = useQuery({ queryKey: alertKeys.channels, queryFn: alertsApi.channels, staleTime: 60_000 });
  return { catalogue, channels };
}

/** "Loading" and load errors of the form's data, or nothing once both are there. */
export function AlertFormDataStatus({ data }: { data: ReturnType<typeof useAlertFormData> }) {
  return (
    <>
      {(data.catalogue.isPending || data.channels.isPending) && <p>Loading…</p>}
      <ErrorLine error={data.catalogue.error} prefix="Could not load the checks" />
      <ErrorLine error={data.channels.error} prefix="Could not load the channels" />
    </>
  );
}

type Props = {
  catalogue: CatalogueCheck[];
  channels: AlertChannels;
  /** The rule being edited; absent for a new one. */
  rule?: AlertRule;
  submitLabel: string;
  pending: boolean;
  /** The failed create or update, shown at its fields. */
  error: unknown;
  onSubmit: (form: AlertFormModel) => void;
};

const errorClass = "text-sm text-red-700 dark:text-red-400";
const noteClass = "text-xs text-amber-800 dark:text-amber-300";

/** A labelled field with its error below. */
function Field({ id, label, error, hint, children }: { id: string; label: string; error?: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 text-sm">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint}
      <FieldError id={`${id}-error`} message={error} />
    </div>
  );
}

/** Props that tie an input to its error. */
const described = (id: string, error: string | undefined, extra?: string) => ({
  "aria-invalid": error ? true : undefined,
  "aria-describedby": [error ? `${id}-error` : null, extra ?? null].filter(Boolean).join(" ") || undefined,
});

/** The rule form. It checks what the spec states (name, addresses, https URL, throttle,
 * threshold, secret length); the API's 422 and 409 are shown at their fields. */
export function AlertForm({ catalogue, channels, rule, submitLabel, pending, error, onSubmit }: Props) {
  const [form, setForm] = useState<AlertFormModel>(() => formOf(rule));
  const [local, setLocal] = useState<Partial<Record<AlertField, string>>>({});
  const api = alertErrors(error);
  const err = (field: AlertField) => local[field] ?? api.fields[field];
  const set = <K extends keyof AlertFormModel>(key: K, value: AlertFormModel[K]) => setForm((f) => ({ ...f, [key]: value }));
  const toggle = <T extends string>(list: T[], item: T, on: boolean): T[] => (on ? [...list, item] : list.filter((x) => x !== item));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const found = validateForm(form);
    setLocal(found);
    if (Object.keys(found).length > 0) return;
    onSubmit(form);
  };

  const signing = channels.webhook.signing;
  const removing = form.removeSecret && Boolean(rule?.has_secret);
  const nLocal = Object.keys(local).length;
  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <div className={`${panelClass} space-y-4`}>
        <Field id="alert-name" label="Name" error={err("name")}>
          <input
            id="alert-name"
            maxLength={120}
            className={inputClass}
            value={form.name}
            {...described("alert-name", err("name"))}
            onChange={(e) => set("name", e.target.value)}
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          Enabled (a disabled rule is not evaluated after runs)
        </label>
      </div>

      <fieldset className={`${panelClass} space-y-4`}>
        <legend className="px-1 font-semibold">What it watches</legend>
        <div role="radiogroup" aria-label="Kind" className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="radio" name="alert-kind" checked={form.kind === "finding"} onChange={() => set("kind", "finding")} />
            New findings
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="alert-kind" checked={form.kind === "score"} onChange={() => set("kind", "score")} />
            A score that falls below a threshold
          </label>
        </div>
        {form.kind === "finding" ? (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field id="alert-severity" label="Minimum severity">
                <select
                  id="alert-severity"
                  className={`${selectClass} min-h-11`}
                  value={form.minSeverity}
                  onChange={(e) => set("minSeverity", e.target.value as AlertFormModel["minSeverity"])}
                >
                  {SEVERITIES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                id="alert-prefix"
                label="Asset path prefix (optional)"
                hint={<span id="alert-prefix-hint" className={`text-xs ${mutedClass}`}>Only series whose asset path starts with this, e.g. plant/feeder.</span>}
              >
                <input
                  id="alert-prefix"
                  className={inputClass}
                  value={form.assetPrefix}
                  aria-describedby="alert-prefix-hint"
                  onChange={(e) => set("assetPrefix", e.target.value)}
                />
              </Field>
            </div>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Checks</legend>
              <p className={`text-xs ${mutedClass}`}>None ticked: findings of every check.</p>
              <div className="grid gap-1 text-sm sm:grid-cols-3">
                {catalogue.map((check) => (
                  <label key={check.id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={form.checkIds.includes(check.id)}
                      onChange={(e) => set("checkIds", toggle(form.checkIds, check.id, e.target.checked))}
                    />
                    <span className="font-mono text-xs">{check.id}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Dimensions</legend>
              <p className={`text-xs ${mutedClass}`}>None ticked: findings of every dimension.</p>
              <div className="grid gap-1 text-sm sm:grid-cols-4">
                {DIMENSIONS.map((d) => (
                  <label key={d} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={form.dimensions.includes(d)}
                      onChange={(e) => set("dimensions", toggle(form.dimensions, d, e.target.checked))}
                    />
                    {d}
                  </label>
                ))}
              </div>
            </fieldset>
          </>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="alert-metric" label="Score">
              <select
                id="alert-metric"
                className={`${selectClass} min-h-11`}
                value={form.metric}
                onChange={(e) => set("metric", e.target.value as AlertFormModel["metric"])}
              >
                {METRICS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
            <Field id="alert-below" label="Falls below (0 to 100)" error={err("condition")}>
              <input
                id="alert-below"
                type="number"
                min={0}
                max={100}
                step="any"
                className={inputClass}
                value={form.below}
                {...described("alert-below", err("condition"))}
                onChange={(e) => set("below", e.target.value)}
              />
            </Field>
          </div>
        )}
        {form.kind === "finding" && <FieldError id="alert-condition-error" message={api.fields.condition} />}
        <FieldError id="alert-kind-error" message={api.fields.kind} />
      </fieldset>

      <fieldset className={`${panelClass} space-y-4`}>
        <legend className="px-1 font-semibold">Where it sends</legend>
        <div role="radiogroup" aria-label="Channel" className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <label className="flex items-center gap-2">
              <input type="radio" name="alert-channel" checked={form.channel === "email"} onChange={() => set("channel", "email")} />
              Email
            </label>
            {!channels.email.available && <span className={noteClass}>Email is not configured on this server: notifications would be skipped.</span>}
          </div>
          <label className="flex items-center gap-2">
            <input type="radio" name="alert-channel" checked={form.channel === "webhook"} onChange={() => set("channel", "webhook")} />
            Webhook
          </label>
        </div>
        <FieldError id="alert-channel-error" message={api.fields.channel} />
        {form.channel === "email" ? (
          <Field
            id="alert-recipients"
            label="Recipients"
            error={err("recipients")}
            hint={<span id="alert-recipients-hint" className={`text-xs ${mutedClass}`}>1 to 10 addresses, one per line or separated by commas.</span>}
          >
            <textarea
              id="alert-recipients"
              rows={3}
              className={`${inputClass} py-2`}
              value={form.recipients}
              {...described("alert-recipients", err("recipients"), "alert-recipients-hint")}
              onChange={(e) => set("recipients", e.target.value)}
            />
          </Field>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="alert-url" label="Webhook URL" error={err("url")}>
              <input
                id="alert-url"
                type="url"
                inputMode="url"
                placeholder="https://"
                className={inputClass}
                value={form.url}
                {...described("alert-url", err("url"))}
                onChange={(e) => set("url", e.target.value)}
              />
            </Field>
            <Field
              id="alert-secret"
              label="Signing secret (optional)"
              error={err("secret")}
              hint={
                <span id="alert-secret-hint" className={`text-xs ${signing ? mutedClass : "text-amber-800 dark:text-amber-300"}`}>
                  {!signing
                    ? "Signing is not available: the server has no master key configured."
                    : rule?.has_secret
                      ? "A secret is set. Leave this empty to keep it, or type a new one to replace it."
                      : "8 to 200 characters; each request then carries an HMAC-SHA256 signature."}
                </span>
              }
            >
              <input
                id="alert-secret"
                type="password"
                autoComplete="off"
                className={inputClass}
                value={form.secret}
                disabled={!signing || removing}
                {...described("alert-secret", err("secret"), "alert-secret-hint")}
                onChange={(e) => set("secret", e.target.value)}
              />
            </Field>
            {rule?.has_secret && (
              <label className="flex items-center gap-2 text-sm sm:col-start-2">
                <input
                  type="checkbox"
                  checked={form.removeSecret}
                  onChange={(e) => setForm((f) => ({ ...f, removeSecret: e.target.checked, secret: e.target.checked ? "" : f.secret }))}
                />
                Remove secret
              </label>
            )}
          </div>
        )}
        <div className="sm:max-w-xs">
          <Field
            id="alert-throttle"
            label="Throttle (minutes)"
            error={err("throttle_minutes")}
            hint={<span id="alert-throttle-hint" className={`text-xs ${mutedClass}`}>At most one notification this often; 0 tells every match at once.</span>}
          >
            <input
              id="alert-throttle"
              type="number"
              min={0}
              max={1440}
              step={1}
              className={inputClass}
              value={form.throttle}
              {...described("alert-throttle", err("throttle_minutes"), "alert-throttle-hint")}
              onChange={(e) => set("throttle", e.target.value)}
            />
          </Field>
        </div>
      </fieldset>
      {nLocal > 0 && (
        <p role="alert" className={errorClass}>
          {nLocal === 1 ? "One field needs a change." : `${nLocal} fields need a change.`}
        </p>
      )}
      {api.other && (
        <p role="alert" className={errorClass}>
          Could not save the rule: {api.other}
        </p>
      )}
      <button type="submit" className={buttonClass} disabled={pending}>
        {submitLabel}
      </button>
    </form>
  );
}
