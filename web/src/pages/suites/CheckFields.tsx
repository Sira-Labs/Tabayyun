// The checks of the suite form (spec 027): a switch per check and an input per parameter.
import { paramKey, paramKind, valueText, type CheckDraft, type ChecksDraft } from "../../suiteParams";
import type { CatalogueCheck } from "../../suites";
import { inputClass } from "../admin/ui";
import { FieldError, mutedClass } from "./parts";

type ParamProps = {
  check: CatalogueCheck;
  param: string;
  value: string | boolean | undefined;
  error: string | undefined;
  onChange: (value: string | boolean) => void;
};

/** One parameter: a checkbox for a switch, else an input whose placeholder is the default (or
 * where the value comes from at run time); empty means "use the default". */
function ParamField({ check, param, value, error, onChange }: ParamProps) {
  const defaultValue = check.defaults[param];
  const kind = paramKind(defaultValue);
  const auto = check.auto[param];
  const name = `${check.id}: ${param}`;
  const errorId = `error-${check.id}-${param}`;
  if (kind === "boolean") {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" aria-label={name} checked={value === true} onChange={(e) => onChange(e.target.checked)} />
        <span className="font-mono text-xs">{param}</span>
        <span className={`text-xs ${mutedClass}`}>(default {defaultValue ? "on" : "off"})</span>
      </label>
    );
  }
  const placeholder = auto ? `from ${auto.source}` : valueText(defaultValue);
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-mono text-xs">{param}</span>
      <input
        className={`${inputClass} ${kind === "json" ? "font-mono text-xs" : ""}`}
        aria-label={name}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        inputMode={kind === "number" ? "decimal" : undefined}
        placeholder={placeholder}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className={`text-xs ${mutedClass}`}>
        {auto ? `Empty: ${auto.note}` : `Default: ${valueText(defaultValue)}`}
        {kind === "json" && !auto && " (JSON)"}
      </span>
      <FieldError id={errorId} message={error} />
    </label>
  );
}

type CheckProps = {
  check: CatalogueCheck;
  draft: CheckDraft;
  errors: Record<string, string>;
  onChange: (draft: CheckDraft) => void;
};

/** One check: its switch, and while it is on its parameters. */
function CheckField({ check, draft, errors, onChange }: CheckProps) {
  const params = Object.keys(check.defaults);
  return (
    <fieldset aria-label={check.id} className="rounded border border-slate-200 p-3 dark:border-slate-800">
      <label className="flex flex-wrap items-center gap-2 text-sm">
        <input
          type="checkbox"
          role="switch"
          aria-label={`Run ${check.id}`}
          checked={draft.enabled}
          onChange={(e) => onChange({ ...draft, enabled: e.target.checked })}
        />
        <span className="font-mono font-medium">{check.id}</span>
        <span className={`text-xs ${mutedClass}`}>{check.scope === "group" ? "across series" : "per series"}</span>
      </label>
      {draft.enabled && params.length > 0 && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {params.map((param) => (
            <ParamField
              key={param}
              check={check}
              param={param}
              value={draft.values[param]}
              error={errors[paramKey(check.id, param)]}
              onChange={(value) => onChange({ ...draft, values: { ...draft.values, [param]: value } })}
            />
          ))}
        </div>
      )}
    </fieldset>
  );
}

type Props = {
  catalogue: CatalogueCheck[];
  draft: ChecksDraft;
  errors: Record<string, string>;
  onChange: (draft: ChecksDraft) => void;
};

/** Every check of the catalogue with its switch and parameters, controlled. */
export function CheckFields({ catalogue, draft, errors, onChange }: Props) {
  return (
    <div className="space-y-2">
      {catalogue.map((check) => (
        <CheckField
          key={check.id}
          check={check}
          draft={draft[check.id] ?? { enabled: false, values: {} }}
          errors={errors}
          onChange={(next) => onChange({ ...draft, [check.id]: next })}
        />
      ))}
    </div>
  );
}
