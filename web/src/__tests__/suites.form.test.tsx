import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { draftOf, checksOf, paramKind } from "../suiteParams";
import { suiteErrors, suitePatch } from "../suites";
import { ApiError } from "../api";
import { json, renderApp, stubFetch } from "./helpers";
import { asRole } from "./sourcesFake";
import { CATALOGUE, DATASETS, DATASET_ID, SUITE_ID, bodyOf, suite } from "./suitesFake";

type Sent = { method: string; url: string; body: unknown };

/** A fake backend of the form pages; `answer` decides what a write gets. */
function backend(answer: (sent: Sent) => unknown = () => json(suite(), 201), stored = suite()) {
  const sent: Sent[] = [];
  const route = (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/checks/catalogue") return CATALOGUE;
    if (url === "/api/datasets?limit=100") return DATASETS;
    if (method !== "GET") {
      const write = { method, url, body: bodyOf(init) };
      sent.push(write);
      return answer(write);
    }
    if (url === `/api/suites/${SUITE_ID}`) return stored;
    if (url.startsWith("/api/runs?")) return { items: [], next_cursor: null };
    throw new Error(`unexpected ${method} ${url}`);
  };
  return { route, sent };
}

const group = (id: string) => within(screen.getByRole("group", { name: id }));

describe("suites.form (new)", () => {
  it("fills the cron field from the presets and says the schedule in words", async () => {
    stubFetch(backend().route, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/suites/new");
    const cron = (await screen.findByLabelText("Schedule (cron, UTC)")) as HTMLInputElement;
    expect(cron.value).toBe("");
    expect(screen.getByText("Only on request: the suite runs when someone starts it.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Only on request" }).getAttribute("aria-pressed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "Hourly" }));
    expect(cron.value).toBe("5 * * * *");
    expect(screen.getByText("Every hour at :05")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hourly" }).getAttribute("aria-pressed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "Weekly" }));
    expect(cron.value).toBe("0 6 * * 1");
    expect(screen.getByText("Mondays at 06:00 UTC")).toBeTruthy();

    await user.clear(cron);
    await user.type(cron, "*/15 * * * *");
    expect(screen.getByText("Every 15 minutes")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Weekly" }).getAttribute("aria-pressed")).toBe("false");

    await user.click(screen.getByRole("button", { name: "Only on request" }));
    expect(cron.value).toBe("");
  });

  it("creates a suite with only the overrides as params, then opens it", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    const { router } = renderApp("/suites/new");
    await user.type(await screen.findByLabelText("Name"), "Feeders, hourly");
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    await user.click(screen.getByRole("button", { name: "Hourly" }));

    // Defaults and run-time sources are shown, not filled in.
    const threshold = group("spike").getByLabelText("spike: threshold") as HTMLInputElement;
    expect(threshold.value).toBe("");
    expect(threshold.placeholder).toBe("4");
    expect((group("range").getByLabelText("range: min") as HTMLInputElement).placeholder).toBe("from metadata");
    expect(group("range").getByText("Empty: the series' physical minimum")).toBeTruthy();

    await user.type(threshold, "6");
    await user.click(group("spike").getByLabelText("spike: two_sided"));
    await user.type(group("range").getByLabelText("range: max"), "120.5");
    await user.type(group("range").getByLabelText("range: mode"), "operational");
    await user.type(group("range").getByLabelText("range: ignore"), "[[1, 2]");
    await user.click(screen.getByRole("switch", { name: "Run drift" }));
    expect(screen.getByRole("group", { name: "drift" }).querySelector("input:not([role=switch])")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Create suite" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/suites/${SUITE_ID}`));
    expect(sent).toEqual([
      {
        method: "POST",
        url: "/api/suites",
        body: {
          name: "Feeders, hourly",
          dataset_id: DATASET_ID,
          schedule: "5 * * * *",
          enabled: true,
          checks: [
            { id: "spike", enabled: true, params: { threshold: 6, two_sided: false } },
            { id: "range", enabled: true, params: { max: 120.5, mode: "operational", ignore: [1, 2] } },
            { id: "drift", enabled: false, params: {} },
          ],
        },
      },
    ]);
  });

  it("sends no schedule as null and every check without overrides", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/suites/new");
    await user.type(await screen.findByLabelText("Name"), "  On request ");
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    await user.click(screen.getByLabelText(/^Enabled/));
    await user.click(screen.getByRole("button", { name: "Create suite" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({
      name: "On request",
      dataset_id: DATASET_ID,
      schedule: null,
      enabled: false,
      checks: CATALOGUE.map((c) => ({ id: c.id, enabled: true, params: {} })),
    });
  });

  it("refuses a short cron, a bad parameter and no checks without a request", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/suites/new");
    await user.type(await screen.findByLabelText("Name"), "Bad");
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    await user.type(screen.getByLabelText("Schedule (cron, UTC)"), "5 * * *");
    await user.type(screen.getByLabelText("spike: threshold"), "six");
    await user.type(screen.getByLabelText("range: ignore"), "[[1,");
    await user.click(screen.getByRole("button", { name: "Create suite" }));
    expect(await screen.findByText("A schedule has five fields: minute, hour, day of month, month, weekday.")).toBeTruthy();
    expect(group("spike").getByText("Enter a number.")).toBeTruthy();
    expect(group("range").getByText("Enter a number or valid JSON (text in double quotes).")).toBeTruthy();
    expect(screen.getByText("2 parameters are not valid.")).toBeTruthy();
    expect(screen.getByLabelText("spike: threshold").getAttribute("aria-invalid")).toBe("true");

    await user.clear(screen.getByLabelText("spike: threshold"));
    await user.clear(screen.getByLabelText("range: ignore"));
    await user.clear(screen.getByLabelText("Schedule (cron, UTC)"));
    for (const id of ["spike", "range", "drift"]) await user.click(screen.getByRole("switch", { name: `Run ${id}` }));
    await user.click(screen.getByRole("button", { name: "Create suite" }));
    expect(await screen.findByText("Switch on at least one check.")).toBeTruthy();
    expect(sent).toEqual([]);
  });

  it("shows the API's 422 at its fields", async () => {
    const detail = [
      { loc: ["body", "schedule"], msg: "firings must be at least five minutes apart", type: "value_error" },
      { loc: ["body", "dataset_id"], msg: "no such dataset", type: "value_error" },
      { loc: ["body", "checks", 0, "params", "threshold"], msg: "must be positive", type: "value_error" },
      { loc: ["body", "name"], msg: "String should have at most 256 characters", type: "string_too_long" },
    ];
    stubFetch(backend(() => json({ detail }, 422)).route, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/suites/new");
    await user.type(await screen.findByLabelText("Name"), "x");
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    await user.type(screen.getByLabelText("Schedule (cron, UTC)"), "* * * * *");
    await user.click(screen.getByRole("button", { name: "Create suite" }));
    const error = await screen.findByText("firings must be at least five minutes apart");
    expect(error.id).toBe("suite-schedule-error");
    expect(screen.getByLabelText("Schedule (cron, UTC)").getAttribute("aria-describedby")).toContain("suite-schedule-error");
    expect(screen.getByText("no such dataset").id).toBe("suite-dataset-error");
    expect(screen.getByText("0.params.threshold: must be positive").id).toBe("suite-checks-error");
    expect(screen.getByText("String should have at most 256 characters").id).toBe("suite-name-error");
    expect(screen.queryByText(/Could not save the suite/)).toBeNull();
  });

  it("shows a 409 as a name that is taken", async () => {
    stubFetch(backend(() => json({ detail: "name_taken" }, 409)).route, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/suites/new");
    await user.type(await screen.findByLabelText("Name"), "Feeders, hourly");
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    await user.click(screen.getByRole("button", { name: "Create suite" }));
    const error = await screen.findByText("A suite with this name already exists.");
    expect(error.id).toBe("suite-name-error");
    expect(screen.getByLabelText("Name").getAttribute("aria-invalid")).toBe("true");
  });

  it("hints when there are no datasets, and keeps viewers out", async () => {
    stubFetch((url) => {
      if (url === "/api/checks/catalogue") return CATALOGUE;
      if (url === "/api/datasets?limit=100") return { items: [], next_cursor: null };
      throw new Error(`unexpected ${url}`);
    }, asRole("editor"));
    const first = renderApp("/suites/new");
    expect(await screen.findByText(/No datasets yet/)).toBeTruthy();
    first.unmount();
    stubFetch(() => ({}), asRole("viewer"));
    renderApp("/suites/new");
    expect(await screen.findByText("Only editors and admins create suites.")).toBeTruthy();
  });
});

describe("suites.form (edit)", () => {
  it("starts from the suite and sends only what changed", async () => {
    const { route, sent } = backend(() => suite());
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    const { router } = renderApp(`/suites/${SUITE_ID}/edit`);
    expect(((await screen.findByLabelText("Name")) as HTMLInputElement).value).toBe("Feeders, hourly");
    expect((screen.getByLabelText("Dataset") as HTMLSelectElement).value).toBe(DATASET_ID);
    expect((screen.getByLabelText("Schedule (cron, UTC)") as HTMLInputElement).value).toBe("5 * * * *");
    expect((screen.getByLabelText("spike: threshold") as HTMLInputElement).value).toBe("6");
    expect((screen.getByLabelText("spike: window") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("spike: two_sided") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("switch", { name: "Run drift" }) as HTMLInputElement).checked).toBe(false);

    await user.click(screen.getByRole("button", { name: "Only on request" }));
    await user.clear(screen.getByLabelText("spike: threshold"));
    await user.click(screen.getByRole("switch", { name: "Run drift" }));
    await user.type(screen.getByLabelText("drift: tolerance"), "0.5");
    await user.click(screen.getByRole("button", { name: "Save suite" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/suites/${SUITE_ID}`));
    expect(sent).toEqual([
      {
        method: "PATCH",
        url: `/api/suites/${SUITE_ID}`,
        body: {
          schedule: null,
          checks: [
            { id: "spike", enabled: true, params: {} },
            { id: "range", enabled: true, params: {} },
            { id: "drift", enabled: true, params: { tolerance: 0.5 } },
          ],
        },
      },
    ]);
  });

  it("goes back without a request when nothing changed", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    const { router } = renderApp(`/suites/${SUITE_ID}/edit`);
    await user.click(await screen.findByRole("button", { name: "Save suite" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/suites/${SUITE_ID}`));
    expect(sent).toEqual([]);
  });

  it("asks for a dataset when the suite's was deleted, and shows a 422 from the PATCH", async () => {
    const detail = [{ loc: ["body", "schedule"], msg: "does not parse", type: "value_error" }];
    const { route, sent } = backend(() => json({ detail }, 422), suite({ dataset_id: null, dataset_name: null }));
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}/edit`);
    expect(await screen.findByText("The suite's dataset was deleted; choose another.")).toBeTruthy();
    await user.selectOptions(screen.getByLabelText("Dataset"), DATASET_ID);
    const cron = screen.getByLabelText("Schedule (cron, UTC)");
    await user.clear(cron);
    await user.type(cron, "a b c d e");
    await user.click(screen.getByRole("button", { name: "Save suite" }));
    expect((await screen.findByText("does not parse")).id).toBe("suite-schedule-error");
    expect(sent[0]?.body).toEqual({ dataset_id: DATASET_ID, schedule: "a b c d e" });
  });

  it("does not send untouched fields back over a change made elsewhere while the form was open", async () => {
    const sent: Sent[] = [];
    let name = "Alpha";
    const route = (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/checks/catalogue") return CATALOGUE;
      if (url === "/api/datasets?limit=100") return DATASETS;
      if (method !== "GET") {
        sent.push({ method, url, body: bodyOf(init) });
        return suite({ name });
      }
      if (url === `/api/suites/${SUITE_ID}`) return suite({ name });
      throw new Error(`unexpected ${method} ${url}`);
    };
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    const { client } = renderApp(`/suites/${SUITE_ID}/edit`);
    expect(((await screen.findByLabelText("Name")) as HTMLInputElement).value).toBe("Alpha");
    // Someone else renames the suite, and the cached copy is refetched under the open form.
    name = "Beta";
    await client.refetchQueries({ queryKey: ["suites", SUITE_ID, "detail"] });
    await user.click(screen.getByLabelText(/^Enabled/));
    await user.click(screen.getByRole("button", { name: "Save suite" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({ enabled: false });
  });

  it("offers the way back to the default for a switch the suite sets explicitly", async () => {
    const stored = suite();
    const spike = { ...(stored.checks[0] as (typeof stored.checks)[number]), params: { two_sided: true } };
    const { route, sent } = backend(() => suite(), suite({ checks: [spike, ...stored.checks.slice(1)] }));
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}/edit`);
    await user.click(await screen.findByRole("button", { name: "spike: two_sided: use the default" }));
    expect(screen.queryByRole("button", { name: "spike: two_sided: use the default" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save suite" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { checks: { id: string; params: unknown }[] };
    expect(body.checks.find((c) => c.id === "spike")?.params).toEqual({});
  });

  it("keeps viewers out", async () => {
    stubFetch(() => suite(), asRole("viewer"));
    renderApp(`/suites/${SUITE_ID}/edit`);
    expect(await screen.findByText("Only editors and admins change suites.")).toBeTruthy();
  });
});

describe("suite form logic", () => {
  it("picks the input kind from the default", () => {
    expect([4, true, "x", null, [], {}].map(paramKind)).toEqual(["number", "boolean", "string", "json", "json", "json"]);
  });

  it("round-trips stored overrides of every kind", () => {
    const stored = [
      { id: "spike", enabled: true, params: { threshold: 6.5, two_sided: false } },
      { id: "range", enabled: false, params: { min: -5, mode: "operational", ignore: [1, 2] } },
    ];
    const draft = draftOf(CATALOGUE, stored);
    expect(draft.range?.values).toEqual({ min: "-5", max: "", mode: "operational", ignore: "[1,2]" });
    expect(checksOf(CATALOGUE, draft)).toEqual({ checks: [...stored, { id: "drift", enabled: false, params: {} }], errors: {} });
  });

  it("keeps a stored switch that equals its default as an override", () => {
    const stored = [{ id: "spike", enabled: true, params: { two_sided: true } }];
    const draft = draftOf(CATALOGUE, stored);
    expect(checksOf(CATALOGUE, draft).checks[0]).toEqual(stored[0]);
    // A switch the suite never set stays out while it equals the default.
    expect(checksOf(CATALOGUE, draftOf(CATALOGUE, null)).checks[0]?.params).toEqual({});
  });

  it("leaves out a bad value of a check that is off, without an error, and keeps it in the draft", () => {
    const draft = draftOf(CATALOGUE, null);
    const spike = draft.spike as { enabled: boolean; values: Record<string, string | boolean> };
    spike.values.threshold = "six";
    spike.values.window = "30";
    spike.enabled = false;
    const off = checksOf(CATALOGUE, draft);
    expect(off.errors).toEqual({});
    expect(off.checks[0]).toEqual({ id: "spike", enabled: false, params: { window: 30 } });
    spike.enabled = true;
    expect(Object.keys(checksOf(CATALOGUE, draft).errors)).toEqual(["spike.threshold"]);
  });

  it("names the parameters that do not hold a value of their kind", () => {
    const draft = draftOf(CATALOGUE, null);
    (draft.spike as { values: Record<string, string | boolean> }).values.window = "1e";
    (draft.drift as { values: Record<string, string | boolean> }).values.tolerance = "wide";
    expect(Object.keys(checksOf(CATALOGUE, draft).errors)).toEqual(["spike.window", "drift.tolerance"]);
  });

  it("builds a PATCH of the changed fields only", () => {
    const stored = suite();
    const same = {
      name: stored.name,
      dataset_id: DATASET_ID,
      schedule: stored.schedule,
      enabled: true,
      checks: stored.checks.map(({ id, enabled, params }) => ({ id, enabled, params })),
    };
    expect(suitePatch(stored, same)).toEqual({});
    expect(suitePatch(stored, { ...same, name: "New", enabled: false })).toEqual({ name: "New", enabled: false });
  });

  it("sorts errors by field and keeps the rest", () => {
    expect(suiteErrors(null)).toEqual({ fields: {}, other: null });
    expect(suiteErrors(new ApiError(403, "forbidden", { detail: "forbidden" })).other).toBe("You need the editor role to change suites.");
    expect(suiteErrors(new ApiError(500, "boom")).other).toBe("boom");
    const detail = [
      { loc: ["body", "checks"], msg: "at least one" },
      { loc: ["body", "checks", 1, "id"], msg: "unknown check" },
      { loc: ["body", "colour"], msg: "extra field" },
    ];
    expect(suiteErrors(new ApiError(422, "x", { detail }))).toEqual({
      fields: { checks: "at least one; 1.id: unknown check" },
      other: "colour: extra field",
    });
  });
});
