import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Evidence } from "../components/Evidence";

// TZ is Asia/Riyadh (UTC+03:00, no DST) in the test script.
const GAP_START = 1_767_225_600_000_000_000; // 2026-01-01T00:00:00Z

describe("Evidence", () => {
  it("renders instants in the local zone with the UTC offset", () => {
    render(<Evidence value={{ gap_start: GAP_START, peak_ts: GAP_START + 3_600_000_000_000 }} />);
    expect(screen.getByText("2026-01-01 03:00:00 UTC+03:00")).toBeTruthy();
    expect(screen.getByText("2026-01-01 04:00:00 UTC+03:00")).toBeTruthy();
    expect(screen.getByText("gap start")).toBeTruthy();
  });

  it("renders *_ns fields as durations and numbers grouped", () => {
    render(<Evidence value={{ gap_duration_ns: 8_700_000_000_000, count: 12345, nan_ratio: 0.0262345 }} />);
    expect(screen.getByText("2h 25m")).toBeTruthy();
    expect(screen.getByText("12,345")).toBeTruthy();
    expect(screen.getByText("0.02623")).toBeTruthy();
  });

  it("renders nested objects as indented key/value lists, never JSON", () => {
    const { container } = render(
      <Evidence value={{ baseline: { mean: 50.5, source: "profile" }, largest: [{ count: 3 }, { count: 2 }], flag: true, limit: null }} />,
    );
    expect(container.querySelectorAll("dl").length).toBe(4);
    expect(screen.getByText("profile")).toBeTruthy();
    expect(screen.getByText("yes")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
    expect(container.textContent).not.toContain("{");
    expect(container.querySelector("ol")?.children.length).toBe(2);
  });

  it("lists scalar arrays inline and truncates long ones", () => {
    const ts = Array.from({ length: 60 }, (_, i) => GAP_START + i * 60_000_000_000);
    render(<Evidence value={{ ts }} />);
    expect(screen.getByText(/2026-01-01 03:00:00 UTC\+03:00, 2026-01-01 03:01:00/)).toBeTruthy();
    expect(screen.getByText("… 10 more")).toBeTruthy();
  });

  it("keeps small numbers under time-like keys as numbers", () => {
    render(<Evidence value={{ window: 5, start: 3 }} />);
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
  });
});
