// Alert rules (spec 031): types, API calls and the pure logic of the alerts pages.
import { ApiError, apiGet, apiSend } from "./api";
import type { Page } from "./types";

export const SEVERITIES = ["low", "medium", "high", "critical"] as const;
export type AlertSeverity = (typeof SEVERITIES)[number];

export const DIMENSIONS = [
  "completeness",
  "timeliness",
  "validity",
  "accuracy",
  "consistency",
  "plausibility",
  "integrity",
] as const;
export type AlertDimension = (typeof DIMENSIONS)[number];

export const METRICS = ["overall", ...DIMENSIONS] as const;
export type AlertMetric = (typeof METRICS)[number];

export type AlertKind = "finding" | "score";
export type AlertChannel = "email" | "webhook";

/** A rule on new findings: at least `min_severity`, and where given of these checks,
 * dimensions and assets. */
export type FindingCondition = {
  min_severity: AlertSeverity;
  check_ids: string[];
  dimensions: AlertDimension[];
  asset_prefix: string | null;
};

/** A rule on a series whose score falls below `below`. */
export type ScoreCondition = { metric: AlertMetric; below: number };

export type AlertCondition = FindingCondition | ScoreCondition;

export type NotificationStatus = "pending" | "sending" | "sent" | "failed" | "skipped";

/** One notification of a rule and its outcome. */
export type AlertNotification = {
  id: string;
  kind: "alert" | "test";
  status: NotificationStatus;
  due_at: string;
  sent_at: string | null;
  n_matches: number;
  attempts: number;
  error: string | null;
  created_at: string;
};

type RuleCommon = {
  id: string;
  name: string;
  enabled: boolean;
  channel: AlertChannel;
  recipients: string[];
  /** The full webhook URL; admins only. */
  url: string | null;
  url_host: string | null;
  has_secret: boolean;
  throttle_minutes: number;
  created_at: string;
  updated_at: string;
  last_notification: AlertNotification | null;
};

export type AlertRule = RuleCommon & ({ kind: "finding"; condition: FindingCondition } | { kind: "score"; condition: ScoreCondition });

/** `GET /api/alerts/channels`: what the server can send with. */
export type AlertChannels = { email: { available: boolean }; webhook: { available: boolean; signing: boolean } };

/** The body of a create or a PATCH; `secret: null` removes the signing secret. */
export type AlertRuleBody = {
  name?: string;
  enabled?: boolean;
  kind?: AlertKind;
  condition?: AlertCondition;
  channel?: AlertChannel;
  recipients?: string[];
  url?: string | null;
  secret?: string | null;
  throttle_minutes?: number;
};

const base = (id: string) => `/api/alerts/${encodeURIComponent(id)}`;

export const alertsApi = {
  list: () => apiGet<Page<AlertRule>>("/api/alerts"),
  channels: () => apiGet<AlertChannels>("/api/alerts/channels"),
  get: (id: string) => apiGet<AlertRule>(base(id)),
  create: (body: AlertRuleBody) => apiSend<AlertRule>("POST", "/api/alerts", body),
  patch: (id: string, body: AlertRuleBody) => apiSend<AlertRule>("PATCH", base(id), body),
  remove: (id: string) => apiSend<void>("DELETE", base(id)),
  test: (id: string) => apiSend<{ notification_id: string }>("POST", `${base(id)}/test`),
  notifications: (id: string, limit = 50) =>
    apiGet<{ items: AlertNotification[] }>(`${base(id)}/notifications?limit=${limit}`),
};

/** Query keys of the alerts pages; all start with `"alerts"`. */
export const alertKeys = {
  all: ["alerts"] as const,
  list: ["alerts", "list"] as const,
  channels: ["alerts", "channels"] as const,
  detail: (id: string) => ["alerts", id, "detail"] as const,
  notifications: (id: string) => ["alerts", id, "notifications"] as const,
};

// ---- form model ----

/** What the rule form edits: text fields stay text until they are sent. */
export type AlertFormModel = {
  name: string;
  enabled: boolean;
  kind: AlertKind;
  minSeverity: AlertSeverity;
  checkIds: string[];
  dimensions: AlertDimension[];
  assetPrefix: string;
  metric: AlertMetric;
  below: string;
  channel: AlertChannel;
  recipients: string;
  url: string;
  /** A new signing secret; empty keeps the stored one. */
  secret: string;
  removeSecret: boolean;
  throttle: string;
};

/** The form of a new rule, or of `rule` as stored. */
export function formOf(rule?: AlertRule): AlertFormModel {
  const form: AlertFormModel = {
    name: "",
    enabled: true,
    kind: "finding",
    minSeverity: "critical",
    checkIds: [],
    dimensions: [],
    assetPrefix: "",
    metric: "overall",
    below: "70",
    channel: "email",
    recipients: "",
    url: "",
    secret: "",
    removeSecret: false,
    throttle: "60",
  };
  if (!rule) return form;
  form.name = rule.name;
  form.enabled = rule.enabled;
  form.kind = rule.kind;
  if (rule.kind === "finding") {
    form.minSeverity = rule.condition.min_severity;
    form.checkIds = [...rule.condition.check_ids];
    form.dimensions = [...rule.condition.dimensions];
    form.assetPrefix = rule.condition.asset_prefix ?? "";
  } else {
    form.metric = rule.condition.metric;
    form.below = String(rule.condition.below);
  }
  form.channel = rule.channel;
  form.recipients = rule.recipients.join("\n");
  form.url = rule.url ?? "";
  form.throttle = String(rule.throttle_minutes);
  return form;
}

/** The addresses of a recipients field: one per line or comma separated. */
export function parseRecipients(text: string): string[] {
  return text
    .split(/[\n,;]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** The condition the form describes. */
function conditionOf(form: AlertFormModel): AlertCondition {
  if (form.kind === "score") return { metric: form.metric, below: Number(form.below) };
  const prefix = form.assetPrefix.trim();
  return {
    min_severity: form.minSeverity,
    check_ids: [...form.checkIds],
    dimensions: DIMENSIONS.filter((d) => form.dimensions.includes(d)),
    asset_prefix: prefix || null,
  };
}

/** The create body of a valid form; `secret` only when one was typed. */
export function bodyOfForm(form: AlertFormModel): AlertRuleBody {
  const webhook = form.channel === "webhook";
  const body: AlertRuleBody = {
    name: form.name.trim(),
    enabled: form.enabled,
    kind: form.kind,
    condition: conditionOf(form),
    channel: form.channel,
    recipients: webhook ? [] : parseRecipients(form.recipients),
    url: webhook ? form.url.trim() : null,
    throttle_minutes: Number(form.throttle),
  };
  if (webhook && form.secret) body.secret = form.secret;
  return body;
}

/** A condition with its keys in one order, for comparing. */
function conditionKey(condition: AlertCondition): string {
  if ("metric" in condition) return JSON.stringify([condition.metric, condition.below]);
  return JSON.stringify([condition.min_severity, condition.check_ids, condition.dimensions, condition.asset_prefix]);
}

/** The fields of a valid form that differ from the stored rule: the body of a PATCH. The
 * secret is sent when one was typed, `null` when its removal is ticked. */
export function alertPatch(rule: AlertRule, form: AlertFormModel): AlertRuleBody {
  const next = bodyOfForm(form);
  const patch: AlertRuleBody = {};
  if (next.name !== rule.name) patch.name = next.name;
  if (next.enabled !== rule.enabled) patch.enabled = next.enabled;
  if (next.kind !== rule.kind) patch.kind = next.kind;
  if (next.kind !== rule.kind || conditionKey(next.condition as AlertCondition) !== conditionKey(rule.condition)) {
    patch.condition = next.condition;
  }
  if (next.channel !== rule.channel) patch.channel = next.channel;
  if (JSON.stringify(next.recipients) !== JSON.stringify(rule.recipients)) patch.recipients = next.recipients;
  if ((next.url ?? null) !== (rule.url ?? null)) patch.url = next.url;
  if (next.throttle_minutes !== rule.throttle_minutes) patch.throttle_minutes = next.throttle_minutes;
  if (next.secret !== undefined) patch.secret = next.secret;
  else if (form.channel === "webhook" && form.removeSecret && rule.has_secret) patch.secret = null;
  return patch;
}

export const ALERT_FIELDS = ["name", "enabled", "kind", "condition", "channel", "recipients", "url", "secret", "throttle_minutes"] as const;
export type AlertField = (typeof ALERT_FIELDS)[number];

/** Errors by form field; `other` is what belongs to none. */
export type AlertErrors = { fields: Partial<Record<AlertField, string>>; other: string | null };

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** What the form can check without the API, as the spec states it; empty when it is valid. */
export function validateForm(form: AlertFormModel): Partial<Record<AlertField, string>> {
  const out: Partial<Record<AlertField, string>> = {};
  const name = form.name.trim();
  if (name.length < 1 || name.length > 120) out.name = "A name has 1 to 120 characters.";
  if (form.kind === "score") {
    const below = form.below.trim() === "" ? Number.NaN : Number(form.below);
    if (!Number.isFinite(below) || below < 0 || below > 100) out.condition = "The threshold is a number from 0 to 100.";
  }
  if (form.channel === "email") {
    const addresses = parseRecipients(form.recipients);
    const bad = addresses.filter((a) => !EMAIL.test(a));
    if (addresses.length < 1 || addresses.length > 10) out.recipients = "Enter 1 to 10 addresses.";
    else if (bad.length) out.recipients = `Not an email address: ${bad.join(", ")}`;
  } else {
    const url = form.url.trim();
    if (!/^https:\/\/[^\s/]+/i.test(url)) out.url = "A webhook URL starts with https://.";
    if (form.secret && (form.secret.length < 8 || form.secret.length > 200)) {
      out.secret = "A signing secret has 8 to 200 characters.";
    }
  }
  const throttle = form.throttle.trim() === "" ? Number.NaN : Number(form.throttle);
  if (!Number.isInteger(throttle) || throttle < 0 || throttle > 1440) {
    out.throttle_minutes = "The throttle is a whole number of minutes from 0 to 1440.";
  }
  return out;
}

type FieldItem = { field?: string; message?: string; loc?: (string | number)[]; msg?: string };

/** The field of the form an API error names: the first part of `condition.below` or `recipients[0]`. */
function fieldOf(item: FieldItem): { field: string | undefined; rest: string } {
  if (typeof item.field === "string") {
    const [head, ...tail] = item.field.split(/[.[]/);
    return { field: head, rest: tail.join(".").replace(/]/g, "") };
  }
  const [head, ...tail] = (item.loc ?? []).filter((p) => p !== "body");
  return { field: head === undefined ? undefined : String(head), rest: tail.join(".") };
}

export const NAME_TAKEN = "A rule with this name already exists.";
export const NO_MASTER_KEY =
  "A signing secret needs the server's master key (TABAYYUN_MASTER_KEY), which is not configured. Save the rule without a secret, or ask the operator to set it.";

/** The API's answer to a refused rule, by field: 409 `name_taken` and
 * `credentials_unavailable`, 422 `[{field, message}]`, 403 for a role below admin. */
export function alertErrors(error: unknown): AlertErrors {
  const out: AlertErrors = { fields: {}, other: null };
  if (!error) return out;
  if (!(error instanceof ApiError)) {
    out.other = error instanceof Error ? error.message : String(error);
    return out;
  }
  const detail = (error.body as { detail?: unknown } | null)?.detail;
  const code = typeof detail === "string" ? detail : error.message;
  if (code === "name_taken") {
    out.fields.name = NAME_TAKEN;
    return out;
  }
  if (code === "credentials_unavailable") {
    out.fields.secret = NO_MASTER_KEY;
    return out;
  }
  if (error.status === 403) {
    out.other = "Only workspace admins change alert rules.";
    return out;
  }
  if (error.status !== 422 || !Array.isArray(detail)) {
    out.other = error.message;
    return out;
  }
  const rest: string[] = [];
  for (const item of detail as FieldItem[]) {
    const { field, rest: inner } = fieldOf(item);
    const msg = item.message ?? item.msg ?? "invalid";
    const line = inner ? `${inner}: ${msg}` : msg;
    if ((ALERT_FIELDS as readonly unknown[]).includes(field)) {
      const key = field as AlertField;
      out.fields[key] = out.fields[key] ? `${out.fields[key]}; ${line}` : line;
    } else {
      rest.push(field === undefined ? msg : `${field}: ${line}`);
    }
  }
  if (rest.length) out.other = rest.join("; ");
  return out;
}

// ---- words ----

/** What a rule watches, in a sentence. */
export function ruleWords(rule: Pick<AlertRule, "kind" | "condition">): string {
  if (rule.kind === "score") {
    const c = rule.condition as ScoreCondition;
    const metric = c.metric === "overall" ? "overall score" : `${c.metric} score`;
    return `Series whose ${metric} falls below ${c.below}`;
  }
  const c = rule.condition as FindingCondition;
  let out = c.min_severity === "low" ? "New findings of any severity" : `New findings of severity ${c.min_severity} or higher`;
  if (c.check_ids.length) out += ` from ${c.check_ids.join(", ")}`;
  if (c.dimensions.length) out += ` in ${c.dimensions.join(", ")}`;
  if (c.asset_prefix) out += ` on ${c.asset_prefix}…`;
  return out;
}

/** Where a rule sends: its recipients, or the webhook's host. */
export function targetWords(rule: Pick<AlertRule, "channel" | "recipients" | "url_host">): string {
  if (rule.channel === "email") return rule.recipients.join(", ");
  return rule.url_host ?? "—";
}

/** A throttle in words. */
export function throttleWords(minutes: number): string {
  if (minutes === 0) return "none";
  if (minutes % 60 === 0) return minutes === 60 ? "1 hour" : `${minutes / 60} hours`;
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

const STATUS_WORDS: Record<NotificationStatus, string> = {
  pending: "waiting to be sent",
  sending: "sending",
  sent: "sent",
  failed: "failed",
  skipped: "skipped",
};

/** A notification's status in words. */
export function statusWords(status: NotificationStatus): string {
  return STATUS_WORDS[status] ?? status;
}

/** Whether a notification may still change: the detail page polls while one is. */
export function inFlight(n: Pick<AlertNotification, "status">): boolean {
  return n.status === "pending" || n.status === "sending";
}
