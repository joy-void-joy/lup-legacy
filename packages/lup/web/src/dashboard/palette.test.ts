import { describe, expect, test } from "bun:test";
import { contrast, grounds, MODE_TEXT, PALETTE, paletteCss, worst, type Theme } from "./palette";

const THEMES: Theme[] = ["dark", "light"];

describe("the palette", () => {
  test("every text colour reads at 7:1 or better on every background it is declared to sit on", () => {
    const short = PALETTE.flatMap((token) => THEMES.flatMap((theme) => {
      const reads = worst(token, theme);
      return reads !== null && reads < 7 ? [`${theme} ${token.name} ${token[theme].hex}: ${reads.toFixed(2)}:1`] : [];
    }));
    expect(short).toEqual([]);
  });

  test("text sits on the page, floats, and added and removed lines; a gutter sign on the page and floats", () => {
    const text = PALETTE.find((token) => token.name === "fg");
    const sign = PALETTE.find((token) => token.name === "add-sign");
    if (text === undefined || sign === undefined) throw new Error("fg and add-sign are palette tokens");
    expect(grounds(text)).toEqual(["bg", "float-bg", "add-bg", "del-bg"]);
    expect(grounds(sign)).toEqual(["bg", "float-bg"]);
  });

  test("a mode block reads against the text fixed on it", () => {
    const normal = PALETTE.find((token) => token.name === "mode-normal");
    if (normal === undefined) throw new Error("mode-normal is a palette token");
    expect(worst(normal, "light")).toBeCloseTo(contrast("#0F4A85", MODE_TEXT.light), 5);
  });

  test("the contrast ratio is WCAG's: black on white is 21:1, a colour on itself 1:1", () => {
    expect(contrast("#000000", "#FFFFFF")).toBeCloseTo(21, 5);
    expect(contrast("#6FC3DF", "#6FC3DF")).toBeCloseTo(1, 5);
  });

  test("a colour moved to reach 7:1 started below it, and one kept as sourced did not need to move", () => {
    const wrong = PALETTE.flatMap((token) => THEMES.flatMap((theme) => {
      const shade = token[theme];
      const before = worst(token, theme, shade.source);
      if (before === null) return [];
      return shade.hex !== shade.source && before >= 7 ? [`${theme} ${token.name} moved from ${shade.source}, which read ${before.toFixed(2)}:1`] : [];
    }));
    expect(wrong).toEqual([]);
  });

  test("the stylesheet's custom properties are generated from the table, light by default and dark where the system asks", () => {
    const css = paletteCss();
    expect(css.startsWith(":root{--bg:#FFFFFF;")).toBe(true);
    expect(css).toContain("@media (prefers-color-scheme: dark){:root{--bg:#000000;");
    for (const token of PALETTE) expect(css).toContain(`--${token.name}:${token.dark.hex};`);
    expect(css).toContain("--mode-normal-fg:#000000;");
  });
});
