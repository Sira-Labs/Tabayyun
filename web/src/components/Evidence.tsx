import { formatDuration, formatLocalTime, formatNumber } from "../format";

// Evidence is each check's machine-readable facts (docs/checks/catalogue.md). It is shown as
// key/value lists, never as a JSON string: nanosecond instants in the browser's zone, `*_ns`
// fields as durations, numbers grouped, nested objects and arrays indented.

const MAX_ITEMS = 50;
// ns since the epoch between 1973 and 2286: durations are far smaller, so a value in this
// range under a time-like key is an instant.
const MIN_INSTANT_NS = 1e17;
const MAX_INSTANT_NS = 1e19;
const TIME_KEY = /(^|_)(ts|start|end|at|time|newest|oldest|first|last)$/;

function isInstant(key: string, v: number): boolean {
  return TIME_KEY.test(key) && Number.isInteger(v) && v >= MIN_INSTANT_NS && v < MAX_INSTANT_NS;
}

function formatScalar(key: string, v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (isInstant(key, v)) return formatLocalTime(v);
    if (key.endsWith("_ns") && v >= 0) return formatDuration(v);
    return formatNumber(v);
  }
  return String(v);
}

function label(key: string): string {
  return key.replace(/_/g, " ");
}

function isScalar(v: unknown): boolean {
  return v === null || typeof v !== "object";
}

function Value({ name, value }: { name: string; value: unknown }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-slate-500">none</span>;
    const shown = value.slice(0, MAX_ITEMS);
    const more = value.length - shown.length;
    if (shown.every(isScalar)) {
      return (
        <span>
          {shown.map((v) => formatScalar(name, v)).join(", ")}
          {more > 0 && <span className="text-slate-500"> … {more} more</span>}
        </span>
      );
    }
    return (
      <ol className="ml-4 list-decimal space-y-1">
        {shown.map((v, i) => (
          <li key={i}>
            <Value name={name} value={v} />
          </li>
        ))}
        {more > 0 && <li className="list-none text-slate-500">… {more} more</li>}
      </ol>
    );
  }
  if (value !== null && typeof value === "object") {
    return <Evidence value={value as Record<string, unknown>} nested />;
  }
  return <span className="tabular-nums">{formatScalar(name, value)}</span>;
}

/** Key/value rendering of a finding's evidence. */
export function Evidence({ value, nested = false }: { value: Record<string, unknown>; nested?: boolean }) {
  const entries = Object.entries(value);
  if (entries.length === 0) return <p className="text-slate-500">No evidence recorded.</p>;
  return (
    <dl
      className={`grid grid-cols-[minmax(8rem,max-content)_1fr] gap-x-4 gap-y-1 text-sm ${
        nested ? "border-l border-slate-300 pl-3 dark:border-slate-700" : ""
      }`}
    >
      {entries.map(([key, v]) => (
        <div key={key} className="contents">
          <dt className="text-slate-600 dark:text-slate-400">{label(key)}</dt>
          <dd className="min-w-0 break-words">
            <Value name={key} value={v} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
