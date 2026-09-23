import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { json, makeRun, renderApp, stubFetch } from "./helpers";

// jsdom does not count files set by user-event toward a file input's `required` constraint,
// so the tests submit the form directly; real browsers validate it before submitting.
/** Submit the form, bypassing jsdom's file `required` check. */
function submit() {
  fireEvent.submit(screen.getByRole("button", { name: "Start run" }).closest("form")!);
}

const CSV = new File(["ts,value\n2026-01-01T00:00:00Z,1\n"], "data.csv", { type: "text/csv" });

describe("RunForm", () => {
  it("shows the server's 422 detail next to the form and keeps the input", async () => {
    const posted: FormData[] = [];
    stubFetch((url, init) => {
      if (url === "/api/runs" && init?.method === "POST") {
        posted.push(init.body as FormData);
        return json({ detail: [{ loc: ["body", "physical_max"], msg: "physical_min must be below physical_max" }] }, 422);
      }
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    renderApp("/runs/new");

    await user.upload(await screen.findByLabelText(/CSV file/), CSV);
    const seriesId = screen.getByLabelText("Series id");
    await user.clear(seriesId);
    await user.type(seriesId, "pump-7");
    await user.type(screen.getByLabelText(/Physical minimum/), "10");
    await user.type(screen.getByLabelText(/Physical maximum/), "5");
    submit();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("physical_max: physical_min must be below physical_max");
    expect((screen.getByLabelText("Series id") as HTMLInputElement).value).toBe("pump-7");
    expect((screen.getByLabelText(/Physical maximum/) as HTMLInputElement).value).toBe("5");
    // Empty optional fields are not sent; filled ones are, with the CSRF header on the request.
    expect(posted[0]?.get("unit")).toBeNull();
    expect(posted[0]?.get("physical_min")).toBe("10");
    expect(posted[0]?.get("ts_unit")).toBe("auto");
  });

  it("opens the run report on 202", async () => {
    const run = makeRun({ status: "queued", stats: {}, duration_ms: null });
    const fetch = stubFetch((url, init) => {
      if (url === "/api/runs" && init?.method === "POST") return json({ id: run.id, status: "queued", created_at: run.created_at }, 202);
      if (url.startsWith(`/api/runs/${run.id}`)) return run;
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    const { router } = renderApp("/runs/new");
    await user.upload(await screen.findByLabelText(/CSV file/), CSV);
    submit();
    await waitFor(() => expect(router.state.location.pathname).toBe(`/runs/${run.id}`));
    expect(await screen.findByText("Waiting for a worker to pick up the run…")).toBeTruthy();
    const headers = fetch.mock.calls.find(([u]) => u === "/api/runs")?.[1]?.headers as Record<string, string>;
    expect(headers["X-Tabayyun-Request"]).toBe("1");
  });
});
