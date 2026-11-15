import { describe, expect, it } from "vitest";
import { CHART_BG, DASH, OKABE_ITO, PALETTE, contrast, luminance, seriesColour, seriesDash, withAlpha } from "../compare/palette";

describe("compare palette", () => {
  it("computes WCAG contrast", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#ffffff", "#ffffff")).toBe(1);
    expect(luminance("#ffffff")).toBeCloseTo(1, 5);
  });

  it("palette_contrast_light_dark: every colour reaches 3:1 against its chart background", () => {
    for (const theme of ["light", "dark"] as const) {
      expect(PALETTE[theme]).toHaveLength(8);
      for (const colour of PALETTE[theme]) {
        expect(contrast(colour, CHART_BG[theme]), `${colour} on ${theme}`).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it("keeps the Okabe-Ito colours that pass, and only darkens (light) or lightens (dark) the rest", () => {
    OKABE_ITO.forEach((base, i) => {
      for (const theme of ["light", "dark"] as const) {
        const used = PALETTE[theme][i]!;
        if (contrast(base, CHART_BG[theme]) >= 3) expect(used).toBe(base);
        else if (theme === "light") expect(luminance(used)).toBeLessThan(luminance(base));
        else expect(luminance(used)).toBeGreaterThan(luminance(base));
      }
    });
  });

  it("dashes series 5 to 8 and takes colours in order", () => {
    expect([0, 1, 2, 3].map(seriesDash)).toEqual([undefined, undefined, undefined, undefined]);
    expect([4, 5, 6, 7].map(seriesDash)).toEqual([DASH, DASH, DASH, DASH]);
    expect(seriesColour(0, "light")).toBe(PALETTE.light[0]);
    expect(seriesColour(7, "dark")).toBe(PALETTE.dark[7]);
    expect(withAlpha("#0072b2", 0.4)).toBe("rgba(0, 114, 178, 0.4)");
  });
});
