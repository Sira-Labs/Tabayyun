import type { QualityClass } from "../../seriesChart";

/** Colours of the quality rug and its legend. */
export const RUG_CLASS: Record<QualityClass, string> = {
  bad: "bg-red-600",
  estimated: "bg-violet-500",
  uncertain: "bg-amber-400",
};
