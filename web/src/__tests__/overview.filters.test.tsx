import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { validateOverviewSearch } from "../overview";
import { renderApp } from "./helpers";
import { PI_ID, overview, overviewBackend } from "./overviewFake";

type App = ReturnType<typeof renderApp>;
const searchOf = (app: App) => app.router.state.location.search as Record<string, unknown>;
const last = (asked: URLSearchParams[]) => Object.fromEntries(asked.at(-1) ?? []);

describe("overview.filters", () => {
  it("reads the filters from the URL and asks for them", async () => {
    const backend = overviewBackend();
    renderApp(`/?days=7&asset=plant/&source=${PI_ID}`);
    await screen.findByRole("region", { name: "Scores" });
    expect(last(backend.asked)).toEqual({ days: "7", asset: "plant/", source: PI_ID });
    expect((screen.getByRole("combobox", { name: "Window" }) as HTMLSelectElement).value).toBe("7");
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Source" }) as HTMLSelectElement).value).toBe(PI_ID));
    expect((screen.getByRole("textbox", { name: "Asset prefix" }) as HTMLInputElement).value).toBe("plant/");
  });

  it("asks for 30 days without filters by default, and keeps the default out of the URL", async () => {
    const backend = overviewBackend();
    const app = renderApp("/");
    await screen.findByRole("region", { name: "Scores" });
    expect(last(backend.asked)).toEqual({ days: "30" });
    expect(searchOf(app)).toEqual({});
    expect((screen.getByRole("combobox", { name: "Window" }) as HTMLSelectElement).value).toBe("30");
  });

  it("falls back to 30 days for a window it does not know, and drops a source that is no UUID", async () => {
    const backend = overviewBackend();
    const user = userEvent.setup();
    const app = renderApp("/?days=14&source=historian");
    await screen.findByRole("region", { name: "Scores" });
    expect(last(backend.asked)).toEqual({ days: "30" });
    expect((screen.getByRole("combobox", { name: "Window" }) as HTMLSelectElement).value).toBe("30");
    // The next change writes the URL in its one spelling: the unknown values are gone.
    await screen.findByRole("option", { name: "Historian" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "Historian");
    await waitFor(() => expect(searchOf(app)).toEqual({ source: PI_ID }));
  });

  it("puts a changed window, source and asset prefix into the URL and the request, keeping the others", async () => {
    const backend = overviewBackend();
    const user = userEvent.setup();
    const app = renderApp("/");
    await screen.findByRole("region", { name: "Scores" });
    const length = app.router.history.length;

    await user.selectOptions(screen.getByRole("combobox", { name: "Window" }), "90");
    await waitFor(() => expect(searchOf(app)).toEqual({ days: 90 }));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "90" }));

    await screen.findByRole("option", { name: "Historian" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Source" }), "Historian");
    await waitFor(() => expect(searchOf(app)).toEqual({ days: 90, source: PI_ID }));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "90", source: PI_ID }));

    // Typing alone does not ask; Enter does.
    const asset = screen.getByRole("textbox", { name: "Asset prefix" });
    const asks = backend.asked.length;
    await user.type(asset, "plant/");
    expect(backend.asked.length).toBe(asks);
    expect(searchOf(app)).toEqual({ days: 90, source: PI_ID });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(searchOf(app)).toEqual({ days: 90, source: PI_ID, asset: "plant/" }));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "90", source: PI_ID, asset: "plant/" }));

    // Back to the default window: it leaves the URL; the other filters stay.
    await user.selectOptions(screen.getByRole("combobox", { name: "Window" }), "30");
    await waitFor(() => expect(searchOf(app)).toEqual({ source: PI_ID, asset: "plant/" }));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "30", source: PI_ID, asset: "plant/" }));

    // Filters replace the history entry rather than adding one.
    expect(app.router.history.length).toBe(length);
  });

  it("marks the numbers of the filter before as updating until the new ones arrive", async () => {
    let release: (body: unknown) => void = () => {};
    const late = new Promise<unknown>((resolve) => (release = resolve));
    const backend = overviewBackend((query) => (query.get("days") === "7" ? late : overview()));
    renderApp("/");
    await screen.findByRole("region", { name: "Scores" });
    expect(screen.queryByRole("status")).toBeNull();
    await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "Window" }), "7");
    expect((await screen.findByRole("status")).textContent).toBe("Updating for the changed filter…");
    expect(screen.getByRole("region", { name: "Scores" }).closest("[aria-busy]")?.getAttribute("aria-busy")).toBe("true");
    release(overview());
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(screen.getByRole("region", { name: "Scores" }).closest("[aria-busy]")?.getAttribute("aria-busy")).toBe("false");
    expect(last(backend.asked)).toEqual({ days: "7" });
  });

  it("applies the asset prefix when the field is left, and clears it when emptied", async () => {
    const backend = overviewBackend();
    const user = userEvent.setup();
    const app = renderApp("/?asset=plant/");
    await screen.findByRole("region", { name: "Scores" });
    const asset = screen.getByRole("textbox", { name: "Asset prefix" });
    await user.clear(asset);
    await user.type(asset, "site/");
    await user.tab();
    await waitFor(() => expect(searchOf(app)).toEqual({ asset: "site/" }));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "30", asset: "site/" }));
    await user.clear(asset);
    await user.tab();
    await waitFor(() => expect(searchOf(app)).toEqual({}));
    await waitFor(() => expect(last(backend.asked)).toEqual({ days: "30" }));
  });
});

describe("validateOverviewSearch", () => {
  it("keeps the known windows but the default, a short asset prefix and a UUID source", () => {
    expect(validateOverviewSearch({ days: 7 })).toEqual({ days: 7 });
    expect(validateOverviewSearch({ days: "90" })).toEqual({ days: 90 });
    expect(validateOverviewSearch({ days: 30 })).toEqual({});
    expect(validateOverviewSearch({ days: 31, other: "x" })).toEqual({});
    expect(validateOverviewSearch({ asset: "  plant/ " })).toEqual({ asset: "plant/" });
    expect(validateOverviewSearch({ asset: "a".repeat(201) })).toEqual({});
    expect(validateOverviewSearch({ asset: 5 })).toEqual({});
    expect(validateOverviewSearch({ source: PI_ID })).toEqual({ source: PI_ID });
    expect(validateOverviewSearch({ source: "nope" })).toEqual({});
  });
});
