// The check part of the suite form (spec 027) as plain data: what the inputs hold, and the
// checks the API gets from them. Only parameters the user set become overrides.
import type { CatalogueCheck, SuiteCheckInput } from "./suites";

/** How a parameter is edited, from its default: numbers, text and switches have their own
 * input; lists, objects and parameters decided at run time (a null default) are JSON text. */
export type ParamKind = "number" | "boolean" | "string" | "json";

/** The kind of input a parameter with this default gets. */
export function paramKind(defaultValue: unknown): ParamKind {
  if (typeof defaultValue === "number") return "number";
  if (typeof defaultValue === "boolean") return "boolean";
  if (typeof defaultValue === "string") return "string";
  return "json";
}

/** One check in the form: its switch, and per parameter the input's text (empty = use the
 * default) or the checkbox's state. */
export type CheckDraft = { enabled: boolean; values: Record<string, string | boolean> };
export type ChecksDraft = Record<string, CheckDraft>;

/** The text an input shows for a stored or default value. */
export function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  return value === undefined ? "" : JSON.stringify(value);
}

/** The form's state for the catalogue's checks. Without `checks` (a new suite) every check is
 * on without overrides; with them, a check that is not listed is off. */
export function draftOf(catalogue: CatalogueCheck[], checks: SuiteCheckInput[] | null): ChecksDraft {
  const draft: ChecksDraft = {};
  for (const info of catalogue) {
    const stored = checks?.find((c) => c.id === info.id);
    const values: Record<string, string | boolean> = {};
    for (const [param, defaultValue] of Object.entries(info.defaults)) {
      const override = stored?.params[param];
      if (paramKind(defaultValue) === "boolean") values[param] = typeof override === "boolean" ? override : defaultValue === true;
      else values[param] = valueText(override);
    }
    draft[info.id] = { enabled: checks === null ? true : (stored?.enabled ?? false), values };
  }
  return draft;
}

/** The key of a parameter's error: `check.param`. */
export const paramKey = (checkId: string, param: string) => `${checkId}.${param}`;

/** The checks the API gets, in the catalogue's order, with only the parameters that were set;
 * `errors` names the inputs that do not hold a value of their kind. */
export function checksOf(catalogue: CatalogueCheck[], draft: ChecksDraft): { checks: SuiteCheckInput[]; errors: Record<string, string> } {
  const checks: SuiteCheckInput[] = [];
  const errors: Record<string, string> = {};
  for (const info of catalogue) {
    const entry = draft[info.id];
    const params: Record<string, unknown> = {};
    for (const [param, defaultValue] of Object.entries(info.defaults)) {
      const value = entry?.values[param];
      const kind = paramKind(defaultValue);
      if (kind === "boolean") {
        if (typeof value === "boolean" && value !== defaultValue) params[param] = value;
        continue;
      }
      const text = typeof value === "string" ? value.trim() : "";
      if (text === "") continue;
      if (kind === "string") params[param] = text;
      else if (kind === "number") {
        const n = Number(text);
        if (Number.isFinite(n)) params[param] = n;
        else errors[paramKey(info.id, param)] = "Enter a number.";
      } else {
        try {
          params[param] = JSON.parse(text) as unknown;
        } catch {
          errors[paramKey(info.id, param)] = "Enter a number or valid JSON (text in double quotes).";
        }
      }
    }
    checks.push({ id: info.id, enabled: entry?.enabled ?? false, params });
  }
  return { checks, errors };
}
