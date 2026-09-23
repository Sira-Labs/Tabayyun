import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom has no layout: the router's scroll restoration calls window.scrollTo.
window.scrollTo = (() => undefined) as typeof window.scrollTo;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
