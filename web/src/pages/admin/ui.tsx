// Small building blocks shared by the admin tabs (spec 014).
import { adminErrorText } from "../../admin";

export const inputClass =
  "min-h-11 rounded border border-slate-300 bg-white px-3 text-sm dark:border-slate-700 dark:bg-slate-900";
export const selectClass =
  "min-h-9 rounded border border-slate-300 bg-white px-2 text-sm dark:border-slate-700 dark:bg-slate-900";
export const linkButtonClass =
  "min-h-9 rounded px-2 text-sm font-medium text-sky-800 underline underline-offset-2 hover:bg-slate-100 disabled:opacity-50 dark:text-sky-300 dark:hover:bg-slate-800";
export const dangerButtonClass =
  "min-h-9 rounded px-2 text-sm font-medium text-red-700 underline underline-offset-2 hover:bg-red-50 disabled:opacity-50 dark:text-red-400 dark:hover:bg-red-950";
export const panelClass = "rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900";

/** Confirmation before a destructive action; replaceable in tests (jsdom has no dialogs). */
export const dialogs = {
  confirm(message: string): boolean {
    return window.confirm(message);
  },
};

/** An alert for a failed query or mutation, or nothing. */
export function ErrorLine({ error, prefix }: { error: unknown; prefix: string }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-sm text-red-700 dark:text-red-400">
      {prefix}: {adminErrorText(error)}
    </p>
  );
}

/** A role menu with an accessible name. */
export function RoleSelect<R extends string>({
  label,
  value,
  roles,
  onChange,
  disabled,
}: {
  label: string;
  value: R;
  roles: readonly R[];
  onChange: (role: R) => void;
  disabled?: boolean;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as R)}
      className={selectClass}
    >
      {roles.map((r) => (
        <option key={r} value={r}>
          {r}
        </option>
      ))}
    </select>
  );
}
