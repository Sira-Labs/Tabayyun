import { screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import {
  NAME_TAKEN,
  NO_MASTER_KEY,
  alertErrors,
  alertPatch,
  bodyOfForm,
  formOf,
  parseRecipients,
  ruleWords,
  statusWords,
  targetWords,
  throttleWords,
  validateForm,
} from "../alerts";
import { ApiError } from "../api";
import { json, renderApp, stubFetch } from "./helpers";
import { CHANNELS, HOOK_ID, RULE_ID, alertsBackend, emailRule, hookRule, type Sent } from "./alertsFake";
import { asRole } from "./sourcesFake";

const ADMIN = asRole("admin");

async function retype(user: UserEvent, label: string | RegExp, text: string) {
  const input = screen.getByLabelText(label);
  await user.clear(input);
  if (text) await user.type(input, text);
}

describe("alerts.form (new)", () => {
  it("creates a findings rule with an email channel, then opens it", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    const { router } = renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "  Critical on feeders ");
    await user.selectOptions(screen.getByLabelText("Minimum severity"), "high");
    await user.click(screen.getByRole("checkbox", { name: "spike" }));
    await user.click(screen.getByRole("checkbox", { name: "plausibility" }));
    await user.click(screen.getByRole("checkbox", { name: "completeness" }));
    await user.type(screen.getByLabelText("Asset path prefix (optional)"), "plant/feeder");
    await user.type(screen.getByLabelText("Recipients"), "ops@example.org,\noncall@example.org");
    await retype(user, "Throttle (minutes)", "30");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/alerts/${RULE_ID}`));
    expect(sent[0]).toEqual({
      method: "POST",
      url: "/api/alerts",
      body: {
        name: "Critical on feeders",
        enabled: true,
        kind: "finding",
        condition: { min_severity: "high", check_ids: ["spike"], dimensions: ["completeness", "plausibility"], asset_prefix: "plant/feeder" },
        channel: "email",
        recipients: ["ops@example.org", "oncall@example.org"],
        url: null,
        throttle_minutes: 30,
      },
    });
  });

  it("creates a score rule with a signed webhook", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "Validity drop");
    await user.click(screen.getByRole("radio", { name: "A score that falls below a threshold" }));
    expect(screen.queryByLabelText("Minimum severity")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Score"), "validity");
    await retype(user, "Falls below (0 to 100)", "55.5");
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    expect(screen.queryByLabelText("Recipients")).toBeNull();
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.com/x");
    const secret = screen.getByLabelText("Signing secret (optional)") as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.autocomplete).toBe("off");
    await user.type(secret, "s3cret-value");
    await retype(user, "Throttle (minutes)", "0");
    await user.click(screen.getByLabelText(/^Enabled/));
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({
      name: "Validity drop",
      enabled: false,
      kind: "score",
      condition: { metric: "validity", below: 55.5 },
      channel: "webhook",
      recipients: [],
      url: "https://hooks.example.com/x",
      secret: "s3cret-value",
      throttle_minutes: 0,
    });
  });

  it("sends no secret for a webhook without one", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "Hook");
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.com/x");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).not.toHaveProperty("secret");
  });

  it("refuses what the spec does not allow without a request", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await screen.findByLabelText("Name");
    await user.type(screen.getByLabelText("Recipients"), "ops@example.org, not-an-address");
    await retype(user, "Throttle (minutes)", "2000");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect(await screen.findByText("A name has 1 to 120 characters.")).toBeTruthy();
    expect(screen.getByText("Not an email address: not-an-address").id).toBe("alert-recipients-error");
    expect(screen.getByText("The throttle is a whole number of minutes from 0 to 1440.")).toBeTruthy();
    expect(screen.getByText("3 fields need a change.")).toBeTruthy();
    expect(screen.getByLabelText("Recipients").getAttribute("aria-invalid")).toBe("true");

    await user.type(screen.getByLabelText("Name"), "Bad");
    await user.click(screen.getByRole("radio", { name: "A score that falls below a threshold" }));
    await retype(user, "Falls below (0 to 100)", "150");
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    await user.type(screen.getByLabelText("Webhook URL"), "http://hooks.example.com/x");
    await user.type(screen.getByLabelText("Signing secret (optional)"), "short");
    await retype(user, "Throttle (minutes)", "1.5");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect(await screen.findByText("A webhook URL starts with https://.")).toBeTruthy();
    expect(screen.getByText("A signing secret has 8 to 200 characters.")).toBeTruthy();
    expect(screen.getByText("The threshold is a number from 0 to 100.")).toBeTruthy();
    expect(screen.getByText("The throttle is a whole number of minutes from 0 to 1440.")).toBeTruthy();
    expect(screen.queryByText("A name has 1 to 120 characters.")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("says when email or signing is not available on the server", async () => {
    const channels = { email: { available: false }, webhook: { available: true, signing: false } };
    stubFetch(alertsBackend({ channels }).route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    expect(await screen.findByText("Email is not configured on this server: notifications would be skipped.")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    expect((screen.getByLabelText("Signing secret (optional)") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("Signing is not available: the server has no master key configured.")).toBeTruthy();
  });

  it("shows no availability note when both channels work", async () => {
    stubFetch(alertsBackend({ channels: CHANNELS }).route, ADMIN);
    renderApp("/alerts/new");
    await screen.findByLabelText("Name");
    expect(screen.queryByText(/Email is not configured/)).toBeNull();
  });

  it("shows a 409 name_taken at the name and credentials_unavailable at the secret", async () => {
    let code = "name_taken";
    stubFetch(alertsBackend({ answer: () => json({ detail: code }, 409) }).route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "Taken");
    await user.type(screen.getByLabelText("Recipients"), "ops@example.org");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect((await screen.findByText(NAME_TAKEN)).id).toBe("alert-name-error");
    expect(screen.getByLabelText("Name").getAttribute("aria-invalid")).toBe("true");
    // Changing the name answers the error: it goes, before any new request.
    await user.type(screen.getByLabelText("Name"), " 2");
    expect(screen.queryByText(NAME_TAKEN)).toBeNull();
    expect(screen.getByLabelText("Name").getAttribute("aria-invalid")).toBeNull();

    code = "credentials_unavailable";
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.com/x");
    await user.type(screen.getByLabelText("Signing secret (optional)"), "long-enough");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect((await screen.findByText(NO_MASTER_KEY)).id).toBe("alert-secret-error");
  });

  it("shows a 422 per field, and what belongs to no field as a list", async () => {
    const detail = [
      { field: "recipients[1]", message: "not an address" },
      { field: "condition.check_ids", message: "unknown check: nope" },
      { field: "colour", message: "unknown field" },
    ];
    stubFetch(alertsBackend({ answer: () => json({ detail }, 422) }).route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "X");
    await user.type(screen.getByLabelText("Recipients"), "ops@example.org");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect((await screen.findByText("1: not an address")).id).toBe("alert-recipients-error");
    expect(screen.getByText("check_ids: unknown check: nope").id).toBe("alert-condition-error");
    expect(screen.getByText("Could not save the rule: colour: unknown field")).toBeTruthy();
    // Every control of the condition names its error; the recipients keep theirs until changed.
    const check = screen.getByRole("checkbox", { name: "completeness" });
    expect(check.getAttribute("aria-invalid")).toBe("true");
    expect(check.getAttribute("aria-describedby")).toBe("alert-condition-error");
    expect(screen.getByLabelText("Minimum severity").getAttribute("aria-describedby")).toBe("alert-condition-error");
    expect(screen.getByLabelText("Asset path prefix (optional)").getAttribute("aria-describedby")).toBe(
      "alert-condition-error alert-prefix-hint",
    );
    await user.click(check);
    expect(screen.queryByText("check_ids: unknown check: nope")).toBeNull();
    expect(check.getAttribute("aria-invalid")).toBeNull();
    expect(screen.getByText("1: not an address")).toBeTruthy();
  });

  it("shows the errors of an answer at fields edited while it was on its way", async () => {
    let release: (answer: Response) => void = () => {};
    const late = new Promise<Response>((resolve) => (release = resolve));
    stubFetch(alertsBackend({ answer: () => late }).route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await user.type(await screen.findByLabelText("Name"), "Taken");
    await user.type(screen.getByLabelText("Recipients"), "ops@example.org");
    await user.click(screen.getByRole("button", { name: /rule/ }));
    // Edited before the answer is there: the answer's error at this field must still show.
    await user.type(screen.getByLabelText("Name"), "!");
    release(json({ detail: "name_taken" }, 409));
    expect((await screen.findByText(NAME_TAKEN)).id).toBe("alert-name-error");
    await user.type(screen.getByLabelText("Name"), "?");
    expect(screen.queryByText(NAME_TAKEN)).toBeNull();
  });

  it("clears a field's own error when the field changes, and keeps the others", async () => {
    stubFetch(alertsBackend().route, ADMIN);
    const user = userEvent.setup();
    renderApp("/alerts/new");
    await screen.findByLabelText("Name");
    await retype(user, "Throttle (minutes)", "2000");
    await user.click(screen.getByRole("button", { name: "Create rule" }));
    expect(await screen.findByText("A name has 1 to 120 characters.")).toBeTruthy();
    await user.type(screen.getByLabelText("Name"), "N");
    expect(screen.queryByText("A name has 1 to 120 characters.")).toBeNull();
    expect(screen.getByLabelText("Name").getAttribute("aria-invalid")).toBeNull();
    expect(screen.getByText("The throttle is a whole number of minutes from 0 to 1440.")).toBeTruthy();
    expect(screen.getByText("Enter 1 to 10 addresses.")).toBeTruthy();
  });

  it("keeps viewers and editors out", async () => {
    for (const role of ["viewer", "editor"] as const) {
      stubFetch(alertsBackend().route, asRole(role));
      const view = renderApp("/alerts/new");
      expect(await screen.findByText("Only workspace admins create alert rules.")).toBeTruthy();
      view.unmount();
    }
    stubFetch(alertsBackend().route, asRole("editor"));
    renderApp(`/alerts/${RULE_ID}/edit`);
    expect(await screen.findByText("Only workspace admins change alert rules.")).toBeTruthy();
  });
});

describe("alerts.form (edit)", () => {
  it("starts from the rule and sends only what changed", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    const { router } = renderApp(`/alerts/${RULE_ID}/edit`);
    expect(((await screen.findByLabelText("Name")) as HTMLInputElement).value).toBe("Critical on feeders");
    expect((screen.getByLabelText("Minimum severity") as HTMLSelectElement).value).toBe("critical");
    expect((screen.getByLabelText("Asset path prefix (optional)") as HTMLInputElement).value).toBe("plant/feeder");
    expect((screen.getByLabelText("Recipients") as HTMLTextAreaElement).value).toBe("ops@example.org");
    await retype(user, "Throttle (minutes)", "15");
    await user.click(screen.getByRole("checkbox", { name: "range" }));
    await user.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/alerts/${RULE_ID}`));
    expect(sent[0]).toEqual({
      method: "PATCH",
      url: `/api/alerts/${RULE_ID}`,
      body: {
        condition: { min_severity: "critical", check_ids: ["range"], dimensions: [], asset_prefix: "plant/feeder" },
        throttle_minutes: 15,
      },
    });
  });

  it("goes back without a request when nothing changed", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    const { router } = renderApp(`/alerts/${HOOK_ID}/edit`);
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/alerts/${HOOK_ID}`));
    expect(sent).toEqual([]);
  });

  it("keeps, replaces or removes the webhook's secret", async () => {
    const runs: Sent[][] = [];
    for (const step of ["keep", "replace", "remove"] as const) {
      const { route, sent } = alertsBackend();
      stubFetch(route, ADMIN);
      const user = userEvent.setup();
      const view = renderApp(`/alerts/${HOOK_ID}/edit`);
      expect(((await screen.findByLabelText("Webhook URL")) as HTMLInputElement).value).toBe("https://hooks.example.com/tabayyun?k=1");
      expect(screen.getByText(/A secret is set/)).toBeTruthy();
      const secret = screen.getByLabelText("Signing secret (optional)") as HTMLInputElement;
      expect(secret.value).toBe("");
      if (step === "keep") await retype(user, "Name", "Score drop 2");
      if (step === "replace") await user.type(secret, "a-new-secret");
      if (step === "remove") {
        await user.click(screen.getByRole("checkbox", { name: "Remove secret" }));
        expect(secret.disabled).toBe(true);
      }
      await user.click(screen.getByRole("button", { name: "Save rule" }));
      await waitFor(() => expect(sent).toHaveLength(1));
      runs.push(sent);
      view.unmount();
    }
    expect(runs.map((sent) => sent[0]?.body)).toEqual([{ name: "Score drop 2" }, { secret: "a-new-secret" }, { secret: null }]);
  });

  it("changes a rule from email to webhook, and keeps what it watches", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp(`/alerts/${RULE_ID}/edit`);
    const score = (await screen.findByRole("radio", { name: "A score that falls below a threshold" })) as HTMLInputElement;
    expect(score.disabled).toBe(true);
    expect((screen.getByRole("radio", { name: "New findings" }) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(/A rule keeps what it watches/).id).toBe("alert-kind-fixed");
    await user.click(screen.getByRole("radio", { name: "Webhook" }));
    expect(screen.queryByRole("checkbox", { name: "Remove secret" })).toBeNull();
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.com/x");
    await user.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({
      channel: "webhook",
      recipients: [],
      url: "https://hooks.example.com/x",
    });
  });

  it("shows a 422 from the PATCH at its field", async () => {
    const detail = [{ field: "throttle_minutes", message: "must be at most 1440" }];
    stubFetch(alertsBackend({ answer: () => json({ detail }, 422) }).route, ADMIN);
    const user = userEvent.setup();
    renderApp(`/alerts/${RULE_ID}/edit`);
    await screen.findByLabelText("Throttle (minutes)");
    await retype(user, "Throttle (minutes)", "10");
    await user.click(screen.getByRole("button", { name: "Save rule" }));
    expect((await screen.findByText("must be at most 1440")).id).toBe("alert-throttle-error");
  });
});

describe("alert rule logic", () => {
  it("says a rule in words", () => {
    expect(ruleWords(emailRule())).toBe("New findings of severity critical or higher on plant/feeder…");
    expect(ruleWords(hookRule())).toBe("Series whose overall score falls below 70");
    expect(ruleWords({ kind: "score", condition: { metric: "timeliness", below: 90 } })).toBe("Series whose timeliness score falls below 90");
    expect(
      ruleWords({ kind: "finding", condition: { min_severity: "low", check_ids: ["spike", "range"], dimensions: ["validity"], asset_prefix: null } }),
    ).toBe("New findings of any severity from spike, range in validity");
    expect(targetWords(emailRule({ recipients: ["a@x.org", "b@x.org"] }))).toBe("a@x.org, b@x.org");
    expect(targetWords(hookRule())).toBe("hooks.example.com");
    expect([0, 1, 45, 60, 120].map(throttleWords)).toEqual(["none", "1 minute", "45 minutes", "1 hour", "2 hours"]);
    expect(["pending", "sending", "sent", "failed", "skipped"].map((s) => statusWords(s as "sent"))).toEqual([
      "waiting to be sent",
      "sending",
      "sent",
      "failed",
      "skipped",
    ]);
  });

  it("reads recipients one per line or comma separated", () => {
    expect(parseRecipients(" a@x.org,b@x.org\n\nc@x.org ,")).toEqual(["a@x.org", "b@x.org", "c@x.org"]);
  });

  it("validates as the spec says", () => {
    const ok = { ...formOf(), name: "R", recipients: "a@x.org" };
    expect(validateForm(ok)).toEqual({});
    expect(validateForm({ ...ok, name: "x".repeat(121) }).name).toBeTruthy();
    expect(validateForm({ ...ok, recipients: Array.from({ length: 11 }, (_, i) => `u${i}@x.org`).join(",") }).recipients).toBe(
      "Enter 1 to 10 addresses.",
    );
    expect(validateForm({ ...ok, recipients: "" }).recipients).toBe("Enter 1 to 10 addresses.");
    expect(validateForm({ ...ok, throttle: "1440" })).toEqual({});
    expect(validateForm({ ...ok, throttle: "-1" }).throttle_minutes).toBeTruthy();
    expect(validateForm({ ...ok, kind: "score", below: "0" })).toEqual({});
    expect(validateForm({ ...ok, kind: "score", below: "" }).condition).toBeTruthy();
    const hook = { ...ok, channel: "webhook" as const, url: "https://h.example.com" };
    expect(validateForm(hook)).toEqual({});
    expect(validateForm({ ...hook, secret: "x".repeat(201) }).secret).toBeTruthy();
    expect(validateForm({ ...hook, secret: "x".repeat(8) })).toEqual({});
    expect(validateForm({ ...hook, url: "https://" }).url).toBeTruthy();
  });

  it("round-trips a stored rule and builds a PATCH of the changes", () => {
    for (const rule of [emailRule(), hookRule()]) expect(alertPatch(rule, formOf(rule))).toEqual({});
    const form = { ...formOf(emailRule()), enabled: false, dimensions: ["validity" as const] };
    expect(alertPatch(emailRule(), form)).toEqual({
      enabled: false,
      condition: { min_severity: "critical", check_ids: [], dimensions: ["validity"], asset_prefix: "plant/feeder" },
    });
    expect(bodyOfForm({ ...formOf(hookRule()), secret: "typed-secret" }).secret).toBe("typed-secret");
    // A typed secret is not sent once the channel is email.
    expect(bodyOfForm({ ...formOf(hookRule()), channel: "email", recipients: "a@x.org", secret: "typed-secret" })).not.toHaveProperty("secret");
  });

  it("sorts API errors by field", () => {
    expect(alertErrors(null)).toEqual({ fields: {}, other: null });
    expect(alertErrors(new ApiError(409, "name_taken", { detail: "name_taken" })).fields.name).toBe(NAME_TAKEN);
    expect(alertErrors(new ApiError(409, "credentials_unavailable", { detail: "credentials_unavailable" })).fields.secret).toBe(NO_MASTER_KEY);
    expect(alertErrors(new ApiError(403, "forbidden", { detail: "forbidden" })).other).toBe("Only workspace admins change alert rules.");
    expect(alertErrors(new ApiError(500, "boom")).other).toBe("boom");
    const detail = [
      { field: "url", message: "must be https" },
      { loc: ["body", "name"], msg: "too long" },
      { field: "url", message: "refused host" },
    ];
    expect(alertErrors(new ApiError(422, "x", { detail }))).toEqual({
      fields: { url: "must be https; refused host", name: "too long" },
      other: null,
    });
  });
});
