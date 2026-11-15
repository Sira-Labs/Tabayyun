import { useSyncExternalStore } from "react";
import type { Theme } from "./palette";

const QUERY = "(prefers-color-scheme: dark)";

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(QUERY);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

const snapshot = (): Theme => (typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches ? "dark" : "light");

/** The theme the page is shown in; the app follows the system setting (`color-scheme: light dark`). */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, snapshot, () => "light" as Theme);
}
