# Spec 031 — Alerts: rules on findings and scores, email and webhook

Sprint 10, story S10-5. Depends on:
- spec 002 and 008: runs, findings and scores that a run stores; spec 011: findings merge
  with recurrences;
- spec 014: the SMTP settings, the mail queue and the audit log; spec 007 and 014: tenancy,
  workspaces and roles;
- spec 021: the address policy for outgoing connections and the master key for stored secrets;
- spec 029: the findings page a notification links to.

Packages: `api/src/tabayyun/` (models, migration 0017, alerts service, delivery, routes,
tasks, run executors, key rotation), `web/src/` (alerts pages), `deploy/`, `docs/`.

## Goal

A workspace admin writes rules such as "tell ops@example.org when a run finds a new critical
finding" or "post to our webhook when a series' score falls below 70". After every run the
rules of its workspace are evaluated on what the run stored; a rule that matches sends one
notification by email or webhook, and no more than one per its throttle time, however many
runs match in between. Every notification and its outcome is kept and shown.

## User story

As a data steward, I set up a rule for critical findings on the feeder assets with my team's
address. The suite of 02:00 finds a stuck sensor; at 02:01 the email is there with the
series, the check, the window and a link to the finding. The four runs that follow find more
of the same and I get one more email an hour later that names them together, not four.

## Interface

### Database (migration 0017)

`alert_rules` (tenant table, row-level security by `org_id`):

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | primary key |
| `name` | text | 1 to 120 characters on one line, unique in the workspace |
| `enabled` | boolean | a disabled rule is not evaluated |
| `kind` | text | `finding` or `score` |
| `condition` | jsonb | see below |
| `channel` | text | `email` or `webhook` |
| `recipients` | jsonb | email: 1 to 10 addresses; webhook: `[]` |
| `url` | text, null | webhook: the `https` URL |
| `secret_key_id`, `secret_nonce`, `secret_ciphertext` | text, bytea, bytea, null | the webhook's signing secret, sealed with the master key |
| `throttle_minutes` | integer | 0 to 1440, default 60 |
| `created_by` | uuid, null | the user who created it |
| `created_at`, `updated_at` | timestamptz | |

`alert_notifications` (tenant table, row-level security):

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | primary key |
| `rule_id` | uuid | the rule; its deletion deletes its notifications |
| `kind` | text | `alert` or `test` |
| `status` | text | `pending`, `sending`, `sent`, `failed` or `skipped` |
| `due_at` | timestamptz | when it may be sent |
| `n_matches` | integer | how many findings or series it gathers |
| `items` | jsonb | the first 50 of them, as they are told |
| `attempts` | integer | sends tried |
| `error` | text, null | why the last try failed, or why it was skipped |
| `claimed_at`, `sent_at`, `created_at` | timestamptz | |

### Conditions

- `finding`: `{"min_severity": "low|medium|high|critical", "check_ids": [], "dimensions": [],
  "asset_prefix": null}`. A finding matches when a run stored it as new (a recurrence that
  was merged into an existing finding does not), its severity is at least `min_severity`,
  and, where given, its check is one of `check_ids`, its dimension one of `dimensions`, and
  its series' asset path starts with `asset_prefix`.
- `score`: `{"metric": "overall" | <dimension>, "below": 0–100}`. A series matches when the
  run stored a score below the threshold and its score before was at or above it, or it had
  none: the rule tells that a series fell below, not that it is still there.

### API

All under `/api/alerts`. Reading needs any role in the workspace; changing needs the
workspace's admin role, because a rule sends data out of the workspace.

- `GET /api/alerts` → `{"items": [Rule], "next_cursor": null}`, by name.
- `GET /api/alerts/channels` → `{"email": {"available": bool}, "webhook": {"available":
  true, "signing": bool}}`: whether SMTP and the master key are configured.
- `POST /api/alerts` → 201 Rule. `GET /api/alerts/{id}` → Rule. `PATCH /api/alerts/{id}` →
  Rule (only the given fields change). `DELETE /api/alerts/{id}` → 204.
- `POST /api/alerts/{id}/test` → 202 `{"notification_id": "…"}`: sends a test notification
  now, whatever the throttle says.
- `GET /api/alerts/{id}/notifications?limit=` → `{"items": [Notification]}`, newest first,
  at most 50.

```json
{
  "id": "…", "name": "Critical on feeders", "enabled": true, "kind": "finding",
  "condition": {"min_severity": "critical", "check_ids": [], "dimensions": [], "asset_prefix": "plant/feeder"},
  "channel": "email", "recipients": ["ops@example.org"],
  "url": null, "url_host": null, "has_secret": false,
  "throttle_minutes": 60,
  "created_at": "…", "updated_at": "…",
  "last_notification": {"id": "…", "kind": "alert", "status": "sent", "due_at": "…", "sent_at": "…",
                        "n_matches": 3, "attempts": 1, "error": null, "created_at": "…"}
}
```

- The request body takes `name`, `enabled`, `kind`, `condition`, `channel`, `recipients`,
  `url`, `secret` and `throttle_minutes`; unknown fields are refused. `secret` is write-only
  (8 to 200 characters; `null` removes it) and needs the master key: 409
  `credentials_unavailable` without it.
- `url` is returned to admins only; everyone gets `url_host`. A webhook URL must be `https`.
- 422 with `{"detail": [{"field": …, "message": …}]}` for a rule that is wrong in itself; 409
  `name_taken`; 404 for a rule of another workspace or org; 403 for a role below admin on a
  changing route.

### Notifications

Email: plain text from `TABAYYUN_SMTP_FROM` to the rule's recipients, subject
`[Tabayyun] <rule name>: <n> new critical findings` (or `… <n> series below <threshold>`),
the first 20 matches with series, check, severity, window and summary, and a link to the
findings page when `TABAYYUN_PUBLIC_URL` is set.

Webhook: `POST <url>` with `Content-Type: application/json`, `User-Agent: Tabayyun/<version>`,
`X-Tabayyun-Event: alert` (or `test`), `X-Tabayyun-Delivery: <notification id>` and, with a
secret, `X-Tabayyun-Signature: sha256=<hex HMAC-SHA256 of the body>`:

```json
{"event": "alert", "delivery": "…", "sent_at": "…",
 "rule": {"id": "…", "name": "…", "kind": "finding"},
 "workspace": {"id": "…", "name": "…"},
 "n_matches": 3, "truncated": false,
 "matches": [{"finding_id": "…", "series_id": "…", "series_name": "…", "asset_path": "…",
              "check_id": "tby.flatline", "dimension": "plausibility", "severity": "critical",
              "window_start": "…", "window_end": "…", "summary": "…", "run_id": "…"}],
 "url": "https://…/findings?…"}
```

A score match is `{"series_id", "series_name", "asset_path", "metric", "score", "previous",
"below", "run_id"}`.

## Behaviour

1. **Evaluation.** After a run succeeded and its results are committed, the enabled rules of
   its workspace are evaluated on what that run stored, in a transaction of their own. An
   error there is logged and never fails the run. The evaluation marks the run
   (`runs.alerts_evaluated_at`) in the same transaction, so a run is evaluated once, however
   often its job is delivered.
2. **Throttle.** A rule that matches while it has a notification waiting adds its matches to
   that one. Otherwise it gets a new notification, due at once, or `throttle_minutes` after
   the last one that told something when that is later: after the time it was sent, or its
   due time while it is being sent. A notification that failed or was skipped told nobody
   and holds nothing back. So the first match is told at once, and later ones together, at
   least the throttle after the send before. `items` keeps the first 50 matches; `n_matches`
   counts all.
3. **Delivery.** The worker's `deliver_alert` job (mail queue) sends a notification when it
   is due: it claims it (`sending`), sends outside any transaction, and records `sent` or the
   error. Three attempts with a growing pause; after the last the notification is `failed`.
   An error the send does not expect is recorded as `failed` too. A notification left
   `sending` for ten minutes (the worker died, or lost the database before recording) is put
   back to `pending` and queued again by the periodic `requeue_stuck_alerts` task (every
   five minutes, through `tabayyun_requeue_stuck_alerts`, which crosses orgs); its own count
   of attempts ends the retries.
4. **Not configured.** An email notification without SMTP settings is `skipped` with the
   reason. A webhook whose secret cannot be opened (no master key, or another key) is
   `failed` with the reason and is not sent unsigned.
5. **Webhook safety.** The URL is resolved under the connectors' address policy (spec 021):
   loopback, link-local and other refused addresses are not contacted, private ranges only
   when allowed, the connection is pinned to the checked address, and redirects are not
   followed. A 2xx answer within 10 seconds is success; the answer's body is not read.
6. **Test.** A test notification carries one made-up match, is due at once and does not
   count for the throttle.
7. **Audit.** Creating, changing, deleting and testing a rule are written to the audit log
   with the acting user; the log names the channel and the host of a webhook, never the
   secret or the full URL.
8. **Key rotation.** `python -m tabayyun.secrets rotate` re-encrypts the rules' secrets
   together with the connectors' credentials.
9. **Inline jobs** (development, tests): the API process delivers what is due at once after
   the run; a throttled notification gets its job and waits for a worker.
10. **Changing a rule's kind** skips what waits for it (`skipped`, with the reason): those
    matches were found by the other kind and cannot be told as this one.

## Acceptance criteria

- [x] A rule for critical findings with an email channel sends one email after a run that
      stores a new critical finding; the email names the finding and links to it.
- [x] A recurrence merged into an existing finding sends nothing; a finding below the
      severity, of another check, dimension or asset sends nothing.
- [x] A score rule sends when a series falls below the threshold and not again while it
      stays below.
- [x] Within the throttle time further matches join one waiting notification, which is due
      the throttle after the one before; with throttle 0 each match is told at once.
- [x] A webhook rule posts the payload with the delivery headers; with a secret the
      signature verifies; a refused address is not contacted and the notification fails
      with the reason; a redirect is not followed.
- [x] A failing send is retried and ends `failed` with the reason after the third attempt;
      a notification stuck in `sending` is taken again after ten minutes.
- [x] Without SMTP an email notification is `skipped`; `GET /api/alerts/channels` says what
      is available.
- [x] Rules are validated (name, kind and condition, channel and target, throttle, secret);
      viewers and editors cannot change rules (403) and do not get the webhook URL; another
      org's or workspace's rule gives 404.
- [x] "Send test" delivers a test notification at once and is audited; so are create,
      change and delete, without the secret.
- [x] Key rotation re-encrypts rule secrets.
- [x] An error while evaluating or delivering never fails the run.
- [x] The web app lists the rules with their last notification, creates and edits them with
      a form for both kinds and channels, shows a rule's notifications, and sends a test.

## Test cases

- **API, no database** (`api/tests/test_alert_rules.py`): `validate_rule` cases,
  `finding_condition_matches`, `score_crossing`, `email_message`, `webhook_request_signs`.
- **API, database** (`api/tests/db/test_alerts.py`): `critical_finding_sends_one_email`,
  `recurrence_and_non_matching_send_nothing`, `score_rule_fires_on_crossing_only`,
  `throttle_gathers_matches`, `webhook_delivery_headers_and_signature`,
  `webhook_refused_address_redirect_and_missing_key`,
  `retries_then_failed_and_stuck_sending_is_retaken`,
  `deferred_delivery_is_queued_for_its_due_time`, `inline_mode_queues_what_is_due_later`,
  `kind_change_skips_what_waits`, `unexpected_send_error_is_recorded`,
  `stuck_notifications_are_requeued`,
  `email_not_configured_is_skipped`, `routes_validation_roles_and_url_visibility`,
  `test_notification_and_audit`, `rotation_reseals_rule_secrets`,
  `evaluation_error_does_not_fail_the_run`.
- **Tenancy**: the routes with a path id join `ROUTES` in `tests/db/test_rls.py`; both tables
  join `EXPECTED_TABLES`; migration 0017 upgrades and downgrades; `deliver_alert` is a
  registered task.
- **Web** (Vitest): `alerts.list`, `alerts.form` (both kinds and channels, validation,
  secret handling, channel availability), `alerts.detail` (notifications, test, enable,
  delete, viewer without actions).

## Implementation notes

- Rules are evaluated after the run's transaction committed, in a transaction of their own:
  an error in alerting cannot undo or fail a run, and the price is that a crash between the
  two loses that run's notifications.
- The throttle is kept in the notifications themselves: a rule has at most one `pending`
  notification, later matches join it (up to 50 items; the count stays exact), and a new one
  is due the throttle after the one before was sent. No counter of its own.
- The delivery job is queued in the evaluation's transaction with `scheduled_at` = the due
  time, and carries the org id, so delivery itself reads nothing across orgs.
  With inline jobs, notifications due now are delivered in the request's background task and
  later ones wait for a worker.
- A notification is claimed (`sending`) before the send and recorded after it; one left
  `sending` for ten minutes is taken again. A send that was accepted but not recorded can
  therefore arrive twice: receivers deduplicate on `X-Tabayyun-Delivery`.
- A rule with a secret whose secret cannot be opened is failed, never sent unsigned.
- Changing rules needs the workspace admin role (they name recipients outside the app and
  URLs the server calls); the full URL is hidden from other roles because incoming-webhook
  URLs carry their token.
- "Send test" neither waits for the throttle nor counts for it.
- The database tests write runs, findings and scores as the owner, so each test decides what
  a run stored; one test goes end to end through an upload.

- From the review:
  - The run's evaluation is claimed with one `UPDATE … WHERE alerts_evaluated_at IS NULL`:
    a snapshot of the run's status before execution did not prove which call ran it.
  - The waiting notification is found by its status, not by the newest `created_at`: an
    evaluation that waited for the rule's lock could store an older time than one sent
    meanwhile.
  - The throttle counts from the send, not from the due time: a notification sent late
    would otherwise be followed too soon.
  - A notification left `sending` had no job that would take it again; the periodic task
    gives it one. `_claim` keeps its ten-minute rule for a job that arrives on its own.
  - The webhook's answer is streamed and closed after the status: a receiver that never
    ends its body cannot hold a worker.
  - Rule names are one line (they become the email subject), and a URL the parser refuses
    is a field error, not a 500.
  - The form drops a field's error when the field changes, and every control of a finding
    condition names the condition's error.

## Out of scope

- Slack and Teams channels, and per-user subscriptions: after R1 (the webhook covers both
  through their incoming webhooks).
- Alerts on events other than a run's results (a source that stopped answering, a suite
  that failed): S10-6 shows connector health; rules on it follow when needed.
- Recurrence alerts ("still failing after n runs"), quiet hours and escalation.
- Showing alerts inside the app (a bell): the findings inbox is the in-app view.
