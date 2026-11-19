// Findings inbox (spec 029): types and API calls of `/findings`, and the pure helpers that turn
// the page's URL search into requests, hold the row selection and word a bulk result.
import { ApiError, apiBlob, apiGet, apiSend } from "./api";
import { formatNumber } from "./format";
import type { Finding, FindingStatus, Page, Severity } from "./types";

export type Assignee = { id: string; display_name: string };
export type Feedback = "false_positive";

/** A finding as the inbox lists it: the stored finding with its series, assignee and marks. */
export type InboxFinding = Finding & {
  series_name: string;
  asset_path: string | null;
  status_at: string | null;
  muted_until: string | null;
  assignee: Assignee | null;
  feedback: Feedback | null;
};

export const STATUSES = ["open", "acked", "muted", "resolved"] as const satisfies readonly FindingStatus[];
export const SEVERITIES = ["critical", "high", "medium", "low"] as const satisfies readonly Severity[];
/** The core's quality dimensions (`Dimension` in `tabayyun-core`). */
export const DIMENSIONS = ["completeness", "timeliness", "validity", "accuracy", "consistency", "plausibility", "integrity"] as const;
export const GROUPINGS = ["check", "series", "asset"] as const;
export type Grouping = (typeof GROUPINGS)[number];
/** What the list shows when the URL names no status. */
const DEFAULT_STATUSES: readonly FindingStatus[] = ["open", "acked"];

/** The page's URL search. `status` and `severity` are comma lists in a fixed order (`status`
 * may be `all`); a missing `status` means open and acknowledged. `open` is the key of the open
 * group and may be empty (series without an asset). */
export type InboxSearch = {
  status?: string;
  severity?: string;
  dimension?: string;
  check_id?: string;
  assignee?: string;
  feedback?: "false_positive" | "none";
  q?: string;
  group?: Grouping;
  open?: string;
};

const FILTER_KEYS = ["status", "severity", "dimension", "check_id", "assignee", "feedback", "q"] as const;
const MAX_Q = 200;
const MAX_KEY = 512;
const ASSIGNEE_RE = /^[\w-]{1,64}$/;

/** A search value as trimmed text: the router parses `q=12` to a number. */
function text(value: unknown): string | undefined {
  if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return undefined;
  const s = String(value).trim();
  return s === "" ? undefined : s;
}

/** The known members of a comma list, once each, in the order of `known`. */
function members<T extends string>(value: unknown, known: readonly T[]): T[] {
  const given = new Set((text(value) ?? "").split(","));
  return known.filter((k) => given.has(k));
}

/** The statuses a search lists. */
export function statusesOf(search: InboxSearch): FindingStatus[] {
  if (search.status === "all") return [...STATUSES];
  const listed = members(search.status, STATUSES);
  return listed.length ? listed : [...DEFAULT_STATUSES];
}

/** The `status` of a search that lists `statuses`: nothing for the default (and for none),
 * `all` for every status. */
export function statusParam(statuses: readonly FindingStatus[]): string | undefined {
  const listed = STATUSES.filter((s) => statuses.includes(s));
  if (listed.length === 0 || listed.join() === DEFAULT_STATUSES.join()) return undefined;
  return listed.length === STATUSES.length ? "all" : listed.join();
}

/** The severities a search is narrowed to; none means any. */
export function severitiesOf(search: InboxSearch): Severity[] {
  return members(search.severity, SEVERITIES);
}

/** The `severity` of a search narrowed to `severities`: nothing for none and for all. */
export function severityParam(severities: readonly Severity[]): string | undefined {
  const listed = SEVERITIES.filter((s) => severities.includes(s));
  return listed.length === 0 || listed.length === SEVERITIES.length ? undefined : listed.join();
}

/** The inbox's URL search: only known keys and values pass, in one spelling per meaning. */
export function validateInboxSearch(raw: Record<string, unknown>): InboxSearch {
  const out: InboxSearch = {};
  const status = raw.status === "all" ? "all" : statusParam(members(raw.status, STATUSES));
  if (status) out.status = status;
  const severity = severityParam(members(raw.severity, SEVERITIES));
  if (severity) out.severity = severity;
  const dimension = text(raw.dimension);
  if (dimension && (DIMENSIONS as readonly string[]).includes(dimension)) out.dimension = dimension;
  const check = text(raw.check_id);
  if (check && check.length <= 100) out.check_id = check;
  const assignee = text(raw.assignee);
  if (assignee && ASSIGNEE_RE.test(assignee)) out.assignee = assignee;
  if (raw.feedback === "false_positive" || raw.feedback === "none") out.feedback = raw.feedback;
  const q = text(raw.q);
  if (q) out.q = q.slice(0, MAX_Q);
  if ((GROUPINGS as readonly unknown[]).includes(raw.group)) {
    out.group = raw.group as Grouping;
    const open = typeof raw.open === "string" ? raw.open : text(raw.open);
    if (open !== undefined && open.length <= MAX_KEY) out.open = open;
  }
  return out;
}

/** Whether a search narrows the list beyond the default. */
export function hasFilters(search: InboxSearch): boolean {
  return FILTER_KEYS.some((key) => search[key] !== undefined);
}

/** The search as a URL search string: filters and grouping, not the open group. This is what
 * a saved filter stores. */
export function searchString(search: InboxSearch): string {
  const q = filterParams(search);
  if (search.group) q.set("group", search.group);
  return q.toString();
}

/** The search a stored search string stands for. */
export function parseSearchString(query: string): InboxSearch {
  const search = validateInboxSearch(Object.fromEntries(new URLSearchParams(query)));
  delete search.open;
  return search;
}

/** The API's filter parameters of a search. */
export function filterParams(search: InboxSearch): URLSearchParams {
  const q = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = search[key];
    if (value !== undefined) q.set(key, value);
  }
  return q;
}

/** One group of a grouping: its findings are the filter's with this key. */
export type GroupFilter = { by: Grouping; key: string };
const GROUP_PARAM: Record<Grouping, string> = { check: "check_id", series: "series_id", asset: "asset_path" };
export const PAGE_SIZE = 200;

/** The list request of a search, narrowed to a group when one is given. */
export function listUrl(search: InboxSearch, group: GroupFilter | null, cursor?: string): string {
  const q = filterParams(search);
  if (group) q.set(GROUP_PARAM[group.by], group.key);
  q.set("limit", String(PAGE_SIZE));
  if (cursor) q.set("cursor", cursor);
  return `/api/findings?${q.toString()}`;
}

/** The groups request of a search. */
export function groupsUrl(search: InboxSearch, by: Grouping): string {
  const filters = filterParams(search).toString();
  return `/api/findings/groups?by=${by}${filters ? `&${filters}` : ""}`;
}

/** The CSV export of a search's filters. */
export function exportUrl(search: InboxSearch): string {
  const filters = filterParams(search).toString();
  return `/api/findings/export${filters ? `?${filters}` : ""}`;
}

// --- selection -------------------------------------------------------------------------------

/** `selected` with `id` added, or removed when it was in. */
export function toggled(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (!next.delete(id)) next.add(id);
  return next;
}

/** How much of `ids` is selected. */
export function selectionState(ids: readonly string[], selected: ReadonlySet<string>): "none" | "some" | "all" {
  if (selected.size === 0 || ids.length === 0) return "none";
  const n = ids.reduce((count, id) => count + (selected.has(id) ? 1 : 0), 0);
  return n === 0 ? "none" : n === ids.length ? "all" : "some";
}

/** "Select all loaded": every id, or none when all were selected. */
export function toggledAll(ids: readonly string[], selected: ReadonlySet<string>): Set<string> {
  return selectionState(ids, selected) === "all" ? new Set() : new Set(ids);
}

// --- changes ---------------------------------------------------------------------------------

/** A change to a finding, as `PATCH /api/findings/{id}` and the bulk route take it. */
export type FindingChange = {
  status?: FindingStatus;
  reason?: string;
  muted_until?: string;
  assignee_id?: string | null;
  feedback?: Feedback | null;
};
export type BulkFailure = { id: string; error: "not_found" | "transition"; status: string | null };
export type BulkResult = { updated: string[]; failed: BulkFailure[] };
/** What a bulk change over several requests did. `updated` and `failed` are what the server
 * said. After a request without an answer its findings are looked up: `found` are now as the
 * change asked (which the request may have done, or something before it), `left` are not
 * (as are those whose request was never sent), a finding that is gone joins `failed`, and
 * `unchecked` could not be looked up either. `error` is the failure that stopped the
 * requests, or null. */
export type BulkOutcome = BulkResult & { found: string[]; left: string[]; unchecked: string[]; error: unknown };
/** Lookups that run at a time when a request without an answer is checked. */
const LOOKUPS = 8;

/** Whether the finding is as the change would leave it: its status with the reason and the
 * end of the mute that were sent, the assignee and the feedback. It says what the finding is,
 * not who made it so. */
export function applied(finding: InboxFinding, change: FindingChange): boolean {
  if (change.status !== undefined && finding.status !== change.status) return false;
  if (change.reason !== undefined && finding.status_reason !== change.reason) return false;
  if (change.muted_until !== undefined && Date.parse(finding.muted_until ?? "") !== Date.parse(change.muted_until)) return false;
  if (change.assignee_id !== undefined && (finding.assignee?.id ?? null) !== change.assignee_id) return false;
  if (change.feedback !== undefined && finding.feedback !== change.feedback) return false;
  return true;
}

/** Whether the server answered and refused: then nothing was changed, and nothing is unknown. */
function refused(error: unknown): boolean {
  return error instanceof ApiError && error.status < 500;
}
/** The API's limit on the ids of one bulk request. */
export const BULK_MAX = 500;

/** `items` in runs of at most `size`. */
export function chunks<T>(items: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

const STATUS_WORD: Record<string, string> = { open: "open", acked: "acknowledged", muted: "muted", resolved: "resolved" };

/** `n finding(s)`. */
function findings(n: number): string {
  return `${formatNumber(n)} ${n === 1 ? "finding" : "findings"}`;
}

/** A bulk result in words: `12 findings acknowledged.` and, when some were refused, why:
 * `3 could not be changed: 2 are resolved, 1 no longer exists.` */
export function bulkMessage(done: string, result: BulkResult): string {
  const head = `${result.updated.length === 0 ? "No findings" : findings(result.updated.length)} ${done}.`;
  if (result.failed.length === 0) return head;
  const counts = new Map<string, number>();
  for (const f of result.failed) {
    const why = f.error === "not_found" ? "gone" : (f.status ?? "refused");
    counts.set(why, (counts.get(why) ?? 0) + 1);
  }
  const parts = [...counts].map(([why, n]) => {
    const count = formatNumber(n);
    if (why === "gone") return `${count} no longer ${n === 1 ? "exists" : "exist"}`;
    if (why === "refused") return `${count} cannot make this change`;
    return `${count} ${n === 1 ? "is" : "are"} ${STATUS_WORD[why] ?? why}`;
  });
  return `${head} ${formatNumber(result.failed.length)} could not be changed: ${parts.join(", ")}.`;
}

/** A mute may end at most this far ahead (spec 029, behaviour 1). */
const MAX_MUTE_DAYS = 366;

/** The end of a mute from a `datetime-local` value, as an ISO instant; an empty value mutes
 * without an end. The end must lie in the future and at most 366 days ahead. */
export function muteEnd(local: string, now: Date = new Date()): { iso: string | null; error: string | null } {
  if (local.trim() === "") return { iso: null, error: null };
  const at = new Date(local);
  if (Number.isNaN(at.getTime())) return { iso: null, error: "Enter a date and time." };
  if (at.getTime() <= now.getTime()) return { iso: null, error: "The end must lie in the future." };
  if (at.getTime() > now.getTime() + MAX_MUTE_DAYS * 86_400_000) return { iso: null, error: "The end can be at most 366 days ahead." };
  return { iso: at.toISOString(), error: null };
}

// --- saved filters and groups ------------------------------------------------------------------

export type SavedFilter = { id: string; name: string; query: string; created_at: string };
export const MAX_FILTER_NAME = 80;

/** Why a filter could not be saved, in words; null for any other error. */
export function savedFilterError(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (error.message === "name_taken") return "You already have a filter with this name.";
  if (error.message === "too_many") return "You have reached the limit of 50 saved filters. Delete one first.";
  return null;
}

export type FindingGroup = {
  key: string;
  label: string;
  count: number;
  severities: Partial<Record<Severity, number>>;
  /** End of the group's newest window, ns. */
  latest: number;
};

const finding = (id: string) => `/api/findings/${encodeURIComponent(id)}`;

/** Sort the findings of a request without an answer into the outcome, by what they are now. */
async function lookUp(ids: readonly string[], change: FindingChange, outcome: BulkOutcome): Promise<void> {
  const queue = [...ids];
  const worker = async () => {
    for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
      try {
        const now = await apiGet<InboxFinding>(finding(id));
        (applied(now, change) ? outcome.found : outcome.left).push(id);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) outcome.failed.push({ id, error: "not_found", status: null });
        else outcome.unchecked.push(id);
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(LOOKUPS, queue.length) }, worker));
}

export const inboxApi = {
  list: (search: InboxSearch, group: GroupFilter | null, cursor?: string) =>
    apiGet<Page<InboxFinding>>(listUrl(search, group, cursor)),
  groups: (search: InboxSearch, by: Grouping) =>
    apiGet<{ items: FindingGroup[]; truncated: boolean }>(groupsUrl(search, by)),
  change: (id: string, change: FindingChange) => apiSend<InboxFinding>("PATCH", finding(id), change),
  /** One change to many findings: at most 500 ids a request, one request after the other.
   * After a request that fails, no further one is sent. A failure without an answer of the
   * server (a lost response, a 5xx) does not say the change was not applied, so those
   * findings are looked up one by one, whatever the list's filter shows, and reported as what
   * they turned out to be. It throws only when the first request failed and every finding
   * was found unchanged, so nothing is to report but the error. */
  bulk: async (ids: readonly string[], change: FindingChange): Promise<BulkOutcome> => {
    const outcome: BulkOutcome = { updated: [], failed: [], found: [], left: [], unchecked: [], error: null };
    const parts = chunks(ids, BULK_MAX);
    for (const [index, part] of parts.entries()) {
      try {
        const answer = await apiSend<BulkResult>("POST", "/api/findings/bulk", { ids: part, change });
        outcome.updated.push(...answer.updated);
        outcome.failed.push(...answer.failed);
      } catch (error) {
        outcome.error = error;
        outcome.left = parts.slice(index + 1).flat();
        if (refused(error)) outcome.left.unshift(...part);
        else await lookUp(part, change, outcome);
        const known = outcome.updated.length + outcome.failed.length + outcome.found.length + outcome.unchecked.length;
        if (known === 0) throw error;
        break;
      }
    }
    return outcome;
  },
  assignees: () => apiGet<{ items: Assignee[] }>("/api/findings/assignees"),
  /** The CSV of a search's filters, and whether the server cut it. */
  exportCsv: async (search: InboxSearch): Promise<{ blob: Blob; truncated: boolean }> => {
    const { blob, headers } = await apiBlob(exportUrl(search));
    return { blob, truncated: headers.get("X-Tabayyun-Truncated") === "true" };
  },
  filters: () => apiGet<{ items: SavedFilter[] }>("/api/finding-filters"),
  saveFilter: (name: string, query: string) => apiSend<SavedFilter>("POST", "/api/finding-filters", { name, query }),
  deleteFilter: (id: string) => apiSend<void>("DELETE", `/api/finding-filters/${encodeURIComponent(id)}`),
};

/** Query keys of the inbox: `inboxKeys.all` covers the list and the groups. */
export const inboxKeys = {
  all: ["findings", "inbox"] as const,
  list: (search: InboxSearch, group: GroupFilter | null) => ["findings", "inbox", "list", listUrl(search, group)] as const,
  groups: (search: InboxSearch, by: Grouping) => ["findings", "inbox", "groups", groupsUrl(search, by)] as const,
  assignees: ["findings", "assignees"] as const,
  filters: ["finding-filters"] as const,
};
