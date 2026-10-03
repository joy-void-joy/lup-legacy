import { describe, expect, test } from "bun:test";
import { parseConflicts, placed, standingWords, versionLines, type Region } from "./conflicts";

/** A file from its lines. Markers are built from strings so no line of this file starts with one. */
const file = (...lines: string[]) => lines.join("\n");
const OPEN = "<<<<<<<";
const BASE = "|||||||";
const SPLIT = "=======";
const CLOSE = ">>>>>>>";

/** The regions of a file that must hold a conflict. */
function regionsOf(text: string): Region[] {
  const regions = parseConflicts(text);
  if (regions === null) throw new Error("the file holds a conflict");
  return regions;
}

describe("reading a conflicted file", () => {
  test("a file without markers holds no conflict", () => {
    expect(parseConflicts(file("a = 1", "b = 2", ""))).toBeNull();
    expect(parseConflicts("")).toBeNull();
  });

  test("a conflict is the stretch between the common text before and after it, our side, then theirs, each marker's label read", () => {
    expect(regionsOf(file("before", `${OPEN} HEAD`, "ours", SPLIT, "theirs", `${CLOSE} feat-x`, "after"))).toEqual([
      { kind: "common", lines: ["before"] },
      {
        kind: "conflict",
        ours: { marker: { text: `${OPEN} HEAD`, label: "HEAD" }, lines: ["ours"] },
        base: null,
        theirs: { marker: { text: SPLIT, label: "" }, lines: ["theirs"] },
        close: { text: `${CLOSE} feat-x`, label: "feat-x" },
      },
      { kind: "common", lines: ["after"] },
    ]);
  });

  test("diff3's common ancestor is a side of its own between ours and theirs", () => {
    const [conflict] = regionsOf(file(`${OPEN} HEAD`, "ours", `${BASE} merged common ancestors`, "base", SPLIT, "theirs", `${CLOSE} feat-x`));
    expect(conflict).toEqual({
      kind: "conflict",
      ours: { marker: { text: `${OPEN} HEAD`, label: "HEAD" }, lines: ["ours"] },
      base: { marker: { text: `${BASE} merged common ancestors`, label: "merged common ancestors" }, lines: ["base"] },
      theirs: { marker: { text: SPLIT, label: "" }, lines: ["theirs"] },
      close: { text: `${CLOSE} feat-x`, label: "feat-x" },
    });
  });

  test("a side may be empty, and conflicts may follow each other with nothing between", () => {
    const regions = regionsOf(file(`${OPEN} HEAD`, SPLIT, "added", `${CLOSE} b`, `${OPEN} HEAD`, "kept", SPLIT, `${CLOSE} b`));
    expect(regions.map((region) => region.kind)).toEqual(["conflict", "conflict"]);
    expect(versionLines(regions, "ours")).toEqual(["kept"]);
    expect(versionLines(regions, "theirs")).toEqual(["added"]);
  });

  test("each version is the common text with that version's side of every conflict, ours standing in for an ancestor a conflict does not record", () => {
    const regions = regionsOf(file(
      "head",
      `${OPEN} HEAD`, "ours 1", `${BASE} base`, "base 1", SPLIT, "theirs 1", `${CLOSE} feat-x`,
      "middle",
      `${OPEN} HEAD`, "ours 2", SPLIT, "theirs 2", `${CLOSE} feat-x`,
      "tail",
    ));
    expect(versionLines(regions, "ours")).toEqual(["head", "ours 1", "middle", "ours 2", "tail"]);
    expect(versionLines(regions, "theirs")).toEqual(["head", "theirs 1", "middle", "theirs 2", "tail"]);
    expect(versionLines(regions, "base")).toEqual(["head", "base 1", "middle", "ours 2", "tail"]);
  });

  test("text that only looks like a marker is a line of the side or the common text it stands in", () => {
    const regions = regionsOf(file(
      `    ${OPEN} indented`,
      `x = "${OPEN} HEAD"`,
      `${OPEN}HEAD with no space`,
      `${SPLIT}= one longer, outside any conflict`,
      SPLIT,
      `${CLOSE} a closing marker outside any conflict`,
      `${OPEN} HEAD`,
      `  ${SPLIT}`,
      `${CLOSE}${CLOSE}`,
      SPLIT,
      `print("${CLOSE} feat-x")`,
      `${CLOSE} feat-x`,
    ));
    expect(regions.length).toBe(2);
    expect(regions[0]).toEqual({ kind: "common", lines: [`    ${OPEN} indented`, `x = "${OPEN} HEAD"`, `${OPEN}HEAD with no space`, `${SPLIT}= one longer, outside any conflict`, SPLIT, `${CLOSE} a closing marker outside any conflict`] });
    expect(versionLines(regions, "ours").slice(-2)).toEqual([`  ${SPLIT}`, `${CLOSE}${CLOSE}`]);
    expect(versionLines(regions, "theirs").slice(-1)).toEqual([`print("${CLOSE} feat-x")`]);
  });

  test("a merge of merge bases writes its inner conflict with longer markers, which are lines of the side holding them", () => {
    const regions = regionsOf(file(
      `${OPEN} HEAD`, "ours",
      `${BASE} merged common ancestors`, `${OPEN}<< Temporary merge branch 1`, "one", `${SPLIT}==`, "two", `${CLOSE}>> Temporary merge branch 2`,
      SPLIT, "theirs", `${CLOSE} feat-x`,
    ));
    expect(versionLines(regions, "base")).toEqual([`${OPEN}<< Temporary merge branch 1`, "one", `${SPLIT}==`, "two", `${CLOSE}>> Temporary merge branch 2`]);
  });

  test("conflict-marker-size's longer markers open and close a conflict at their own length", () => {
    const long = (marker: string) => marker + marker.slice(0, 3);
    const regions = regionsOf(file(`${long(OPEN)} HEAD`, SPLIT, `${long(SPLIT)}`, "theirs", `${long(CLOSE)} feat-x`));
    expect(versionLines(regions, "ours")).toEqual([SPLIT]);
    expect(versionLines(regions, "theirs")).toEqual(["theirs"]);
  });

  test("a conflict left open, or markers out of their order, is no conflict a reader can trust", () => {
    expect(parseConflicts(file("a", `${OPEN} HEAD`, "ours", SPLIT, "theirs"))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, "ours"))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, "ours", `${CLOSE} feat-x`))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, SPLIT, `${BASE} base`, `${CLOSE} feat-x`))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, `${BASE} a`, `${BASE} b`, SPLIT, `${CLOSE} feat-x`))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, SPLIT, SPLIT, `${CLOSE} feat-x`))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, `${OPEN} HEAD`, SPLIT, `${CLOSE} feat-x`))).toBeNull();
    expect(parseConflicts(file(`${OPEN} HEAD`, "x", SPLIT, "y", `${CLOSE} feat-x`, `${OPEN} HEAD`, "open to the end"))).toBeNull();
  });

  test("CRLF lines keep their carriage return in the text, and a label is read without it", () => {
    const regions = regionsOf([`${OPEN} HEAD`, "ours", SPLIT, "theirs", `${CLOSE} feat-x`, ""].join("\r\n"));
    expect(regions[0]).toEqual({
      kind: "conflict",
      ours: { marker: { text: `${OPEN} HEAD\r`, label: "HEAD" }, lines: ["ours\r"] },
      base: null,
      theirs: { marker: { text: `${SPLIT}\r`, label: "" }, lines: ["theirs\r"] },
      close: { text: `${CLOSE} feat-x\r`, label: "feat-x" },
    });
    expect(regions[1]).toEqual({ kind: "common", lines: [""] });
  });

  test("every line is placed in the version it is read from, at its index there, and each marker as itself naming its side", () => {
    const regions = regionsOf(file("a", `${OPEN} HEAD`, "o1", "o2", `${BASE} base`, "b1", SPLIT, "t1", `${CLOSE} feat-x`, "z"));
    const ours = { side: "ours", marker: null, label: "HEAD" } as const;
    expect(placed(regions)).toEqual([
      { kind: "text", version: "ours", at: 0, standing: null },
      { kind: "marker", marker: { text: `${OPEN} HEAD`, label: "HEAD" }, standing: { ...ours, marker: "open" } },
      { kind: "text", version: "ours", at: 1, standing: ours },
      { kind: "text", version: "ours", at: 2, standing: ours },
      { kind: "marker", marker: { text: `${BASE} base`, label: "base" }, standing: { side: "base", marker: "open", label: "base" } },
      { kind: "text", version: "base", at: 1, standing: { side: "base", marker: null, label: "base" } },
      { kind: "marker", marker: { text: SPLIT, label: "" }, standing: { side: "theirs", marker: "open", label: "feat-x" } },
      { kind: "text", version: "theirs", at: 1, standing: { side: "theirs", marker: null, label: "feat-x" } },
      { kind: "marker", marker: { text: `${CLOSE} feat-x`, label: "feat-x" }, standing: { side: "theirs", marker: "close", label: "feat-x" } },
      { kind: "text", version: "ours", at: 3, standing: null },
    ]);
  });

  test("where an earlier conflict records no ancestor, the lines our side stood in with count before a later ancestor's", () => {
    const regions = regionsOf(file(`${OPEN} HEAD`, "o1", "o2", SPLIT, "t1", `${CLOSE} b`, "c", `${OPEN} HEAD`, "o3", `${BASE} base`, "b1", SPLIT, `${CLOSE} b`));
    expect(placed(regions).find((place) => place.kind === "text" && place.version === "base")).toEqual({ kind: "text", version: "base", at: 3, standing: { side: "base", marker: null, label: "base" } });
    expect(versionLines(regions, "base")[3]).toBe("b1");
  });

  test("a common line after a conflict is placed past our side's lines, whatever theirs held", () => {
    const regions = regionsOf(file(`${OPEN} HEAD`, SPLIT, "t1", "t2", "t3", `${CLOSE} feat-x`, "after"));
    expect(placed(regions).at(-1)).toEqual({ kind: "text", version: "ours", at: 0, standing: null });
  });

  test("a marker says which side it opens or closes, and the label naming that side", () => {
    expect(standingWords({ side: "ours", marker: "open", label: "HEAD" })).toBe("ours · HEAD");
    expect(standingWords({ side: "base", marker: "open", label: "merged common ancestors" })).toBe("base · merged common ancestors");
    expect(standingWords({ side: "theirs", marker: "open", label: "feat-x" })).toBe("theirs · feat-x");
    expect(standingWords({ side: "theirs", marker: "close", label: "feat-x" })).toBe("end of theirs · feat-x");
    expect(standingWords({ side: "theirs", marker: "close", label: "" })).toBe("end of theirs");
  });
});
