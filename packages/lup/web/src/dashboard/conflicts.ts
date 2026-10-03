// A file a merge left conflicted, read into the stretches its versions share
// and the conflicts between them, so each version can be read whole.
//
// Git writes a conflict as marker lines: `<<<<<<< <label>` opens our side,
// `||||||| <label>` opens the common ancestor's where the diff3 or zdiff3
// style records it, `=======` opens their side, and `>>>>>>> <label>` closes
// the conflict. A marker is a run of one character at the start of a line —
// seven long, or the length `conflict-marker-size` sets — ended by whitespace
// or the end of the line, and every marker of one conflict has the length of
// the one that opened it: a run of another length inside a conflict, as in
// the inner conflicts a merge of merge bases writes, is a line of its side. A
// conflict left open at the end of the file, or markers out of their order,
// is no conflict anyone can read the versions from, so a file holding one is
// read as holding none.

/** A version of a conflicted file: our side's, the common ancestor's, or their side's. */
export type Version = "ours" | "base" | "theirs";

/** Every version, in the order a conflict lists its sides. */
export const VERSIONS: readonly Version[] = ["ours", "base", "theirs"];

/** A marker line as the file spells it, and the label it carries. */
export type Marker = { text: string; label: string };

/** One side of a conflict: the marker opening it and the lines it holds. */
export type Side = { marker: Marker; lines: string[] };

/** A stretch of a conflicted file: lines every version shares, or one conflict, its sides and the marker closing it. */
export type Region =
  | { kind: "common"; lines: string[] }
  | { kind: "conflict"; ours: Side; base: Side | null; theirs: Side; close: Marker };

/** Where a line stands in a conflict: on a side, or on the marker opening or closing one, with the label naming that side. */
export type Standing = { side: Version; marker: "open" | "close" | null; label: string };

/** One line of a conflicted file: a line of one version, at its index there, or a marker line. */
export type Placed =
  | { kind: "text"; version: Version; at: number; standing: Standing | null }
  | { kind: "marker"; marker: Marker; standing: Standing };

/** A marker: a run of seven or more of one marker character, then whitespace or the end of the line, then its label. */
const MARKER = /^(([<|=>])\2{6,})(?=\s|$)([^]*)$/;

/**
 * A file's regions, in order, or null where it holds no conflict a reader can
 * trust: none at all, one left open at the end, a marker out of its order,
 * or a conflict opened inside another at the same length.
 */
export function parseConflicts(text: string): Region[] | null {
  const regions: Region[] = [];
  let common: string[] = [];
  let open: { size: number; ours: Side; base: Side | null; theirs: Side | null } | null = null;
  for (const line of text.split("\n")) {
    const [, run = "", char = "", rest = ""] = MARKER.exec(line) ?? [];
    const marker = { text: line, label: rest.trim() };
    if (open === null) {
      if (char === "<") {
        if (common.length > 0) regions.push({ kind: "common", lines: common });
        common = [];
        open = { size: run.length, ours: { marker, lines: [] }, base: null, theirs: null };
      } else common.push(line);
      continue;
    }
    if (run.length !== open.size) {
      (open.theirs ?? open.base ?? open.ours).lines.push(line);
      continue;
    }
    switch (char) {
      case "|":
        if (open.base !== null || open.theirs !== null) return null;
        open.base = { marker, lines: [] };
        break;
      case "=":
        if (open.theirs !== null) return null;
        open.theirs = { marker, lines: [] };
        break;
      case ">":
        if (open.theirs === null) return null;
        regions.push({ kind: "conflict", ours: open.ours, base: open.base, theirs: open.theirs, close: marker });
        open = null;
        break;
      default:
        return null;
    }
  }
  if (open !== null) return null;
  if (common.length > 0) regions.push({ kind: "common", lines: common });
  return regions.some((region) => region.kind === "conflict") ? regions : null;
}

/** Whether any conflict records the common ancestor, so the file has a base version to read. */
export function hasBase(regions: Region[]): boolean {
  return regions.some((region) => region.kind === "conflict" && region.base !== null);
}

/** The lines a region gives one version: a common stretch whole, a conflict its side of that version — ours standing in for an ancestor it does not record. */
function linesOf(region: Region, version: Version): string[] {
  return region.kind === "common" ? region.lines : (region[version] ?? region.ours).lines;
}

/** One version of a conflicted file whole, as its lines. */
export function versionLines(regions: Region[], version: Version): string[] {
  return regions.flatMap((region) => linesOf(region, version));
}

/**
 * Each line of a conflicted file, in order, placed in the version it is read
 * from — a common line in ours, a side's line in its own version, each at its
 * index among that version's lines — and every marker line as itself.
 */
export function placed(regions: Region[]): Placed[] {
  const at: Record<Version, number> = { ours: 0, base: 0, theirs: 0 };
  const lines = (version: Version, count: number, standing: Standing | null): Placed[] =>
    Array.from({ length: count }, (_, offset) => ({ kind: "text", version, at: at[version] + offset, standing }));
  return regions.flatMap((region): Placed[] => {
    const out: Placed[] = region.kind === "common" ? lines("ours", region.lines.length, null) : [
      ...VERSIONS.flatMap((version): Placed[] => {
        const side = region[version];
        if (side === null) return [];
        const label = version === "theirs" ? region.close.label : side.marker.label;
        return [
          { kind: "marker", marker: side.marker, standing: { side: version, marker: "open", label } },
          ...lines(version, side.lines.length, { side: version, marker: null, label }),
        ];
      }),
      { kind: "marker", marker: region.close, standing: { side: "theirs", marker: "close", label: region.close.label } },
    ];
    for (const version of VERSIONS) at[version] += linesOf(region, version).length;
    return out;
  });
}

/** What a marker line says of itself after it: the side it opens or closes, and the label naming that side. */
export function standingWords(standing: Standing): string {
  const what = standing.marker === "close" ? `end of ${standing.side}` : standing.side;
  return standing.label === "" ? what : `${what} · ${standing.label}`;
}
