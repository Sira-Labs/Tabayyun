// Series colours of the compare view (spec 018, behaviour 8): the Okabe-Ito order with a
// light and a dark variant. Colours below 3:1 against the chart background (WCAG 1.4.11) were
// darkened (light) or lightened (dark) until they pass; `compare.palette.test.ts` checks all.

export type Theme = "light" | "dark";

/** The chart panel's background per theme (`bg-white`, `dark:bg-slate-900`). */
export const CHART_BG: Record<Theme, string> = { light: "#ffffff", dark: "#0f172a" };

/** Okabe-Ito as published: orange, sky blue, bluish green, yellow, blue, vermillion, reddish purple, black. */
export const OKABE_ITO = ["#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7", "#000000"] as const;

export const PALETTE: Record<Theme, readonly string[]> = {
  // Orange, sky blue, yellow and reddish purple are darkened.
  light: ["#c18600", "#4a9bc8", "#009e73", "#9e962c", "#0072b2", "#d55e00", "#cc79a7", "#000000"],
  // Black is lightened.
  dark: ["#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7", "#666666"],
};

/** Series from the fifth on are dashed as well, so colour is never the only cue. */
export const DASH = [6, 4];
const FIRST_DASHED = 4;

/** The colour of the series at `index` (display order). */
export function seriesColour(index: number, theme: Theme): string {
  const colours = PALETTE[theme];
  return colours[index % colours.length]!;
}

/** The dash pattern of the series at `index`, or undefined for a solid line. */
export function seriesDash(index: number): number[] | undefined {
  return index % PALETTE.light.length >= FIRST_DASHED ? DASH : undefined;
}

function channels(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** `#rrggbb` as `rgba()` with the given opacity. */
export function withAlpha(hex: string, alpha: number): string {
  const [r, g, b] = channels(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** WCAG relative luminance of `#rrggbb`. */
export function luminance(hex: string): number {
  const [r, g, b] = channels(hex).map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio of two `#rrggbb` colours, from 1 to 21. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
