// Findings in the compare view (spec 018, behaviour 6): which are cross-series, which series
// a selected finding emphasises, and the compare link a finding opens. Pure.
import { aroundFinding } from "../seriesChart";
import type { Finding } from "../types";
import { MAX_SERIES, MIN_SERIES, searchOfState, type CompareSearch } from "./url";

type FindingLike = Pick<Finding, "id" | "series_id" | "window" | "evidence">;

const text = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/** The group a cross-series finding belongs to (evidence `group_id`), or null. */
export function groupOf(finding: Pick<Finding, "evidence">): string | null {
  return text(finding.evidence.group_id);
}

/** The other series the finding involves: evidence `members`, and `partner` for a pair. */
export function partnersOf(finding: Pick<Finding, "series_id" | "evidence">): string[] {
  const members = Array.isArray(finding.evidence.members) ? finding.evidence.members.map(text) : [];
  const ids = [...members, text(finding.evidence.partner)].filter((id): id is string => id !== null);
  return [...new Set(ids)].filter((id) => id !== finding.series_id);
}

/** A finding about several series: its evidence names a group or a partner. */
export function isCrossSeries(finding: Pick<Finding, "evidence">): boolean {
  return groupOf(finding) !== null || text(finding.evidence.partner) !== null;
}

/** The series a finding blames: evidence `suspect`, else the series it is attached to. */
export function suspectOf(finding: Pick<Finding, "series_id" | "evidence">): string {
  return text(finding.evidence.suspect) ?? finding.series_id;
}

export type Emphasis = "suspect" | "muted";

/** Opacity of the series a selected finding does not blame. */
export const MUTED_OPACITY = 0.4;

/** How each shown series is drawn while `finding` is selected: its suspect stronger, the rest
 * muted. Empty without a selection, or when the suspect is not among the series shown. */
export function emphasisOf(finding: Pick<Finding, "series_id" | "evidence"> | null | undefined, seriesIds: string[]): Record<string, Emphasis> {
  if (!finding) return {};
  const suspect = suspectOf(finding);
  if (!seriesIds.includes(suspect)) return {};
  return Object.fromEntries(seriesIds.map((id) => [id, id === suspect ? "suspect" : "muted"]));
}

/** The series a finding's compare view shows: its own, then its members or partner, at most
 * eight; a suspect beyond the eighth takes the last place. */
export function compareSeriesOf(finding: Pick<Finding, "series_id" | "evidence">): string[] {
  const all = [finding.series_id, ...partnersOf(finding)];
  const kept = all.slice(0, MAX_SERIES);
  const suspect = suspectOf(finding);
  if (all.includes(suspect) && !kept.includes(suspect)) kept[MAX_SERIES - 1] = suspect;
  return kept;
}

/** The `/compare` search a finding opens: its series, a window centred on it, its group when
 * the evidence names one, and the finding selected. Null when it involves a single series. */
export function compareSearchOfFinding(finding: FindingLike): CompareSearch | null {
  const series = compareSeriesOf(finding);
  if (series.length < MIN_SERIES) return null;
  return searchOfState({
    series,
    range: aroundFinding(finding.window),
    layout: "auto",
    norm: "raw",
    group: groupOf(finding) ?? undefined,
    finding: finding.id,
  });
}
