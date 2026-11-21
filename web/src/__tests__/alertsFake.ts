// Fixtures and a fake backend of the alerts pages' tests (spec 031).
import type { AlertChannels, AlertNotification, AlertRule } from "../alerts";
import { json } from "./helpers";
import { CATALOGUE, bodyOf } from "./suitesFake";

export { CATALOGUE, bodyOf };

export const RULE_ID = "aaaaaaaa-0000-0000-0000-00000000a001";
export const HOOK_ID = "aaaaaaaa-0000-0000-0000-00000000a002";
export const NOTE_ID = "bbbbbbbb-0000-0000-0000-00000000b001";

export const CHANNELS: AlertChannels = { email: { available: true }, webhook: { available: true, signing: true } };

/** A notification with sensible defaults: an alert sent at 08:01. */
export function notification(overrides: Partial<AlertNotification> = {}): AlertNotification {
  return {
    id: NOTE_ID,
    kind: "alert",
    status: "sent",
    due_at: "2026-10-09T08:01:00Z",
    sent_at: "2026-10-09T08:01:05Z",
    n_matches: 3,
    attempts: 1,
    error: null,
    created_at: "2026-10-09T08:01:00Z",
    ...overrides,
  };
}

/** The email rule of the spec's example: critical findings on the feeders. */
export function emailRule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    id: RULE_ID,
    name: "Critical on feeders",
    enabled: true,
    kind: "finding",
    condition: { min_severity: "critical", check_ids: [], dimensions: [], asset_prefix: "plant/feeder" },
    channel: "email",
    recipients: ["ops@example.org"],
    url: null,
    url_host: null,
    has_secret: false,
    throttle_minutes: 60,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    last_notification: notification(),
    ...overrides,
  } as AlertRule;
}

/** A score rule posting to a signed webhook. */
export function hookRule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    id: HOOK_ID,
    name: "Score drop",
    enabled: true,
    kind: "score",
    condition: { metric: "overall", below: 70 },
    channel: "webhook",
    recipients: [],
    url: "https://hooks.example.com/tabayyun?k=1",
    url_host: "hooks.example.com",
    has_secret: true,
    throttle_minutes: 0,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    last_notification: null,
    ...overrides,
  } as AlertRule;
}

export type Sent = { method: string; url: string; body: unknown };

/** A fake alerts API over `rules`: writes are recorded in `sent`; `answer` may replace the
 * answer to a write (a 409 or 422); `notes` gives a rule's notifications at each request. */
export function alertsBackend({
  rules = [emailRule(), hookRule()],
  channels = CHANNELS,
  answer,
  notes = () => [notification()],
}: {
  rules?: AlertRule[];
  channels?: AlertChannels;
  answer?: (sent: Sent) => unknown;
  notes?: (ruleId: string) => AlertNotification[];
} = {}) {
  const sent: Sent[] = [];
  const store = new Map(rules.map((r) => [r.id, r]));
  const route = (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/checks/catalogue") return CATALOGUE;
    if (url === "/api/alerts/channels") return channels;
    if (method !== "GET") {
      const write = { method, url, body: bodyOf(init) };
      sent.push(write);
      const replaced = answer?.(write);
      if (replaced !== undefined) return replaced;
    }
    if (url === "/api/alerts" && method === "GET") return { items: [...store.values()], next_cursor: null };
    if (url === "/api/alerts" && method === "POST") {
      const created = { ...emailRule(), ...(bodyOf(init) as object), id: RULE_ID } as AlertRule;
      store.set(created.id, created);
      return json(created, 201);
    }
    const match = /^\/api\/alerts\/([^/?]+)(\/[a-z]+)?(\?.*)?$/.exec(url);
    const rule = match ? store.get(decodeURIComponent(match[1] as string)) : undefined;
    if (!rule) return json({ detail: "not_found" }, 404);
    const tail = match?.[2];
    if (!tail && method === "GET") return rule;
    if (!tail && method === "PATCH") {
      const next = { ...rule, ...(bodyOf(init) as object) } as AlertRule;
      store.set(rule.id, next);
      return next;
    }
    if (!tail && method === "DELETE") {
      store.delete(rule.id);
      return new Response(null, { status: 204 });
    }
    if (tail === "/test" && method === "POST") return json({ notification_id: "bbbbbbbb-0000-0000-0000-00000000b009" }, 202);
    if (tail === "/notifications" && method === "GET") return { items: notes(rule.id) };
    throw new Error(`unexpected ${method} ${url}`);
  };
  return { route, sent, store };
}
