// One review as the editor reads it: what the call is, what the policy asked
// about, and the whole review as one buffer of rows — the command and its
// steps, then each file as a fold under its header, its hunks, the unchanged
// lines between them folded, and the line comments under the lines they are on.
// Everything here is worked out from fields the server already sends.
import type { CommandSegment, LineComment, ReviewDetail, ReviewFile, ReviewMarker, ReviewRoot, ReviewSuppression, ReviewSummary, UnpreviewedStep } from "../generated/views";

/** A review as the page holds it: its queue row, and its detail once read. */
export type Entry = { row: ReviewSummary; detail: ReviewDetail | null };

/** A line comment being written, which keeps an id of its own until it is sent. */
export type DraftComment = LineComment & { id: string };

/** What the operator is writing on one review: the note, and the line comments drafted on its lines. */
export type Draft = { note: string; comments: DraftComment[] };
export const EMPTY_DRAFT: Draft = { note: "", comments: [] };

/** What a pane shows of a review: the diff, one side whole, or the unified text. */
export type PaneView = "diff" | "before" | "after" | "raw";

/** Which folds of one review are open: per file, its gaps, whole file, and the allowed steps. */
export type ReviewUi = {
  full: boolean;
  whole: ReadonlySet<number>;
  gaps: ReadonlySet<string>;
  closed: ReadonlySet<number>;
  opened: ReadonlySet<number>;
  allowed: boolean;
};
export const CLOSED_UI: ReviewUi = { full: false, whole: new Set(), gaps: new Set(), closed: new Set(), opened: new Set(), allowed: false };

export type Side = "before" | "after";

export const plural = (count: number, one: string, many = `${one}s`) => `${count.toLocaleString("en")} ${count === 1 ? one : many}`;
export const basename = (path: string) => path.slice(path.lastIndexOf("/") + 1) || path;

/** Whether a waiting review is one this page cannot answer, which it then never calls pending. */
export function blocked(row: ReviewSummary): boolean {
  return row.state === "pending" && !row.answerable && row.unanswerable !== "";
}

/** A review's state as the page names it: the relay's `rejected` is declined, and one this page cannot answer says so. */
export function stateLabel(row: ReviewSummary): string {
  if (blocked(row)) return "can't answer here";
  return row.state === "rejected" ? "declined" : row.state;
}

export function stateClass(row: ReviewSummary): string {
  return blocked(row) ? "blocked" : row.state;
}

const STATE_SIGN: Record<string, string> = { pending: "●", approved: "✓", completed: "✓", dispatched: "➜", preparing: "➜", rejected: "✗", stale: "≠", expired: "◷", cancelled: "−", failed: "!", in_doubt: "?", blocked: "⊘" };

/** Every state has a glyph as well as a colour. */
export const stateSign = (row: ReviewSummary) => STATE_SIGN[stateClass(row)] ?? "·";

/** What a verdict's effect is called on the page. */
export function reviewLabel(effect: string): string {
  switch (effect) {
    case "allow": return "Automatic";
    case "defer": return "Native decision";
    case "ask": return "Needs approval";
    case "deny": return "Blocked";
    default: return "Unclassified";
  }
}

export const EFFECT_SIGN: Record<string, string> = { allow: "✓", defer: "→", ask: "?", deny: "✗", unknown: "·" };

export type MarkerKind = "note" | "defer" | "solved" | "template" | "ignore";
export const markerKind = (marker: ReviewMarker): MarkerKind => marker.kind;
export const MARKER_LETTER: Record<MarkerKind, string> = { note: "N", defer: "D", solved: "S", template: "T", ignore: "X" };

/** What a marker is called on the page, by its kind. */
export function markerLabel(marker: ReviewMarker): string {
  switch (marker.kind) {
    case "defer": return marker.condition === null ? "deferred" : `deferred[${marker.condition}]`;
    case "solved": return "solved";
    case "template": return "template";
    case "ignore": return "exception";
    default: return "open note";
  }
}

/** Where the agent's own words about a call were found, said as the claim it is. */
export const SOURCES: Record<string, string> = {
  description: "agent's note",
  justification: "agent's reason to leave the sandbox",
  preceding: "agent said before this call",
  doing: "session is on",
  proposal: "the proposal says (--why)",
};

/** A path as the operator reads it beside its repository: `tree/feature` under the directory holding its checkouts. */
export function checkoutLabel(path: string, root: ReviewRoot | undefined): string {
  const repository = root?.repository ?? "";
  const home = repository.endsWith("/.git") ? repository.slice(0, -"/.git".length) : repository;
  if (home !== "" && path === home) return basename(home);
  if (home !== "" && path.startsWith(`${home}/`)) return path.slice(home.length + 1);
  return basename(path);
}

/** A path relative to the checkout a review changes, where it lies under it. */
export const relative = (path: string, target: string) => target !== "" && path.startsWith(`${target}/`) ? path.slice(target.length + 1) : path;

/** The checkout a review's files lie in, where it is not the one keeping its queue. */
export function changedElsewhere(row: ReviewSummary, root: ReviewRoot | undefined): string {
  return row.target !== "" && root !== undefined && row.target !== root.path ? checkoutLabel(row.target, root) : "";
}

/** What went stale, in a sentence per file. */
export function staleSentences(row: ReviewSummary): string[] {
  return row.stale.map((moved) => {
    const name = basename(moved.path);
    switch (moved.cause) {
      case "created": return `${name} was created since this was recorded`;
      case "deleted": return `${name} was deleted since this was recorded`;
      case "directory": return `a directory now stands at ${name}`;
      default: return `${name} changed since this was recorded`;
    }
  });
}

/** Who asked, as the roster calls it where it knows. */
export const askedBy = (row: ReviewSummary) => row.session || row.requester || "an unknown session";

/** What a review's call is, in plain terms: its kind, how much it covers, and where. */
export type Head = { kind: string; what: string; where: string; short: string };

function names(paths: string[]): string {
  const base = paths.map(basename);
  return base.length <= 3 ? base.join(", ") : `${base[0] ?? ""} and ${base.length - 1} more`;
}

function payloadText(payload: unknown, name: string): string {
  if (typeof payload !== "object" || payload === null || !(name in payload)) return "";
  const value = (payload as Record<string, unknown>)[name];
  return typeof value === "string" ? value : "";
}

/**
 * The head of a review: from its detail where it was read, else from its queue
 * row alone, which says the server's own title. `Proposal · 2 files ·
 * host.py, relay.py`, `Shell command · 8 steps, 2 need approval · changes 2
 * files`, `Write · new file · pulse.py`.
 */
export function headOf(entry: Entry): Head {
  const { row, detail } = entry;
  if (detail === null) return { kind: row.title, what: "", where: "", short: "" };
  const tool = detail.question.operation.tool;
  const files = detail.files;
  const target = row.target;
  const payload = detail.question.operation.payload;
  const paths = files.map((file) => file.path);
  switch (tool) {
    case "Propose": {
      const count = files.length;
      return { kind: "Proposal", what: plural(count, "file"), where: count === 1 ? relative(paths[0] ?? "", target) : "", short: names(paths) };
    }
    case "Edit": case "MultiEdit": {
      const path = paths[0] ?? payloadText(payload, "file_path");
      return { kind: "Edit", what: "", where: relative(path, target), short: basename(path) };
    }
    case "Write": {
      const path = paths[0] ?? payloadText(payload, "file_path");
      return { kind: "Write", what: files[0]?.operation === "create" ? "new file" : "replaces the file", where: relative(path, target), short: basename(path) };
    }
    case "Bash": {
      const segments = detail.question.segments ?? [];
      const asking = segments.filter((segment) => segment.effect !== "allow").length;
      const lines = (detail.command ?? "").split("\n").length;
      const what = segments.length > 1 ? `${plural(segments.length, "step")}, ${asking} ${asking === 1 ? "needs" : "need"} approval` : lines > 1 ? `${lines}-line script` : "1 step";
      const touches = files.some((file) => needsReview(file)) ? `changes ${plural(files.length, "file")}` : "";
      return { kind: "Shell command", what, where: touches, short: touches !== "" ? `${what} · ${names(paths) || touches}` : what };
    }
    case "apply_patch": return { kind: "Patch", what: plural(files.length, "file"), where: "", short: names(paths) };
    default: return { kind: `${tool} call`, what: "", where: "", short: "" };
  }
}

export const headText = (head: Head) => [head.kind, head.what, head.where].filter((part) => part !== "").join(" · ");

/** The head in short form, as the tree and the finder show it. */
export function headShort(head: Head): string {
  if (head.kind === "Shell command") return [head.kind, head.short].filter((part) => part !== "").join(" · ");
  return [head.kind, head.what, head.short].filter((part) => part !== "").join(" · ");
}

/** A file the review asks about, rather than one the policy let through on its own or left to the runtime. */
export const needsReview = (file: ReviewFile) => file.review_effect !== "allow" && file.review_effect !== "defer";

/** One part of what the policy asked about. */
export type Judged =
  | { kind: "segment"; si: number; segment: CommandSegment; rule: string; reason: string }
  | { kind: "command"; rule: string; reason: string }
  | { kind: "file"; fi: number; file: ReviewFile; rule: string; reason: string; lines: { side: Side; line: number }[] };

/** The lines of a file the policy asked about: the `# lup:` lines a question names, else the changed lines; a new file is judged whole. */
function judgedLines(file: ReviewFile, row: ReviewSummary): { side: Side; line: number }[] {
  if (file.before === null) return [];
  const named = /line (\d+):/.test(file.review_reason) || /line (\d+):/.test(row.reason);
  const added = new Set(file.hunks.flatMap((hunk) => hunk.lines.filter((line) => line.kind === "add").map((line) => line.new_line)));
  const markers = file.markers.filter((marker) => marker.side === "after" && added.has(marker.line));
  if (named && markers.length > 0) {
    return markers.flatMap((marker) => Array.from({ length: marker.end_line - marker.line + 1 }, (_, offset) => ({ side: "after" as const, line: marker.line + offset })));
  }
  return file.hunks.flatMap((hunk) => hunk.lines.flatMap((line): { side: Side; line: number }[] => {
    if (line.kind === "add" && line.new_line !== null) return [{ side: "after" as const, line: line.new_line }];
    if (line.kind === "remove" && line.old_line !== null) return [{ side: "before" as const, line: line.old_line }];
    return [];
  }));
}

/** The exact part of the call the policy asked about: the steps that asked, or the files and lines. */
export function judgedOf(detail: ReviewDetail, row: ReviewSummary): Judged[] {
  const out: Judged[] = [];
  const segments = detail.question.segments ?? [];
  if (detail.command !== null) {
    if (segments.length > 1) {
      segments.forEach((segment, si) => {
        if (segment.effect !== "allow") out.push({ kind: "segment", si, segment, rule: segment.rule, reason: segment.reason });
      });
    } else out.push({ kind: "command", rule: row.rule, reason: row.reason });
  }
  detail.files.forEach((file, fi) => {
    if (!needsReview(file)) return;
    out.push({ kind: "file", fi, file, rule: detail.command === null ? row.rule : "", reason: file.review_reason, lines: judgedLines(file, row) });
  });
  return out;
}

/** What the editor's window bar says of what the policy asked about, in the warning colour. */
export function judgedSummary(judged: Judged[], row: ReviewSummary, walk: string): string {
  const first = judged[0];
  if (first === undefined) return "";
  if (judged.length === 1) {
    const where = first.kind === "file" ? `${basename(first.file.path)}: ` : first.kind === "segment" ? `step ${first.si + 1}: ` : "";
    return `? ${first.rule || row.rule || "unattributed"} — ${where}${first.reason}`;
  }
  const rules = [...new Set(judged.map((item) => item.rule || row.rule).filter((rule) => rule !== ""))];
  const parts = judged.map((item) => item.kind === "segment" ? `step ${item.si + 1}` : item.kind === "file" ? basename(item.file.path) : "the line");
  return `? the policy asks about ${judged.length} parts (${rules.join(", ") || "unattributed"}): ${parts.join(", ")} · ${walk}`;
}

/** A run of line numbers as the context lists them: `L972-1082, 1086 and 25 more`. */
export function lineSummary(lines: { line: number }[]): string {
  const numbers = [...new Set(lines.map((line) => line.line))].sort((a, b) => a - b);
  const [first] = numbers;
  if (first === undefined) return "";
  const spans: string[] = [];
  let start = first;
  let last = first;
  for (const number of numbers.slice(1)) {
    if (number === last + 1) { last = number; continue; }
    spans.push(start === last ? `${start}` : `${start}-${last}`);
    start = number;
    last = number;
  }
  spans.push(start === last ? `${start}` : `${start}-${last}`);
  return `L${spans.slice(0, 4).join(", ")}${spans.length > 4 ? ` and ${spans.length - 4} more` : ""}`;
}

/** A line comment as a review's thread holds it, with who said it and when. */
export type SentComment = LineComment & { author: string; at: string };

/** Every line comment already sent on a review, by the operator's remarks and answer. */
export function sentComments(detail: ReviewDetail): SentComment[] {
  return detail.thread.filter((entry) => entry.kind !== "reply").flatMap((entry) => entry.comments.map((comment) => ({ ...comment, author: entry.author, at: entry.at })));
}

/** The rule exceptions a file shows: those asked about, or every one in the full operation. */
export function exceptionsOf(file: ReviewFile, full: boolean): ReviewSuppression[] {
  return file.suppressions.filter((suppression) => full || (suppression.review_effect !== "allow" && suppression.review_effect !== "defer"));
}

/** The rule ids a suppression is about, as the page names them. */
export function exceptionRules(suppression: ReviewSuppression, full = false): string {
  const ids = full ? suppression.rule_ids : suppression.review_rule_ids ?? suppression.rule_ids;
  if (ids === null) return "unscoped";
  return ids.length === 0 ? "no rule ids" : ids.join(", ");
}

export type MarkerStop = { fi: number; file: ReviewFile; marker: ReviewMarker };
export type ExceptionStop = { fi: number; file: ReviewFile; suppression: ReviewSuppression };

/** Each `# lup:` marker `m` stops at: one on an unchanged line is stopped at once, on the after side. */
export function markerStops(detail: ReviewDetail, full: boolean): MarkerStop[] {
  return detail.files.flatMap((file, fi) => {
    if (!full && !needsReview(file)) return [];
    const removed = new Set(file.hunks.flatMap((hunk) => hunk.lines.filter((line) => line.kind === "remove").map((line) => line.old_line)));
    return file.markers.filter((marker) => marker.side === "after" || removed.has(marker.line)).map((marker) => ({ fi, file, marker }));
  });
}

export function exceptionStops(detail: ReviewDetail, full: boolean): ExceptionStop[] {
  return detail.files.flatMap((file, fi) => exceptionsOf(file, full).map((suppression) => ({ fi, file, suppression })));
}

/** How many lines a document has, a trailing newline ending the last rather than opening another. */
export function lineCount(text: string | null): number {
  if (text === null || text === "") return 0;
  const parts = text.split("\n").length;
  return text.endsWith("\n") ? parts - 1 : parts;
}

/** Who holds a path now, as a file header and the context name them. */
export type Holder = { name: string; lock: boolean };

/** One row of a buffer. Every row is one cursor stop. */
export type Row = { i: number; key: string; jg?: boolean } & (
  | { t: "msg"; tone: "" | "err" | "muted" | "warn"; text: string }
  | { t: "sec"; text: string; sub: string }
  | { t: "cmd"; text: string; marks: [number, number][] }
  | { t: "seg"; segment: CommandSegment; si: number; total: number; allowed: boolean }
  | { t: "segwhy"; segment: CommandSegment }
  | { t: "fold"; open: boolean; text: string }
  | { t: "step"; step: UnpreviewedStep; verdict: CommandSegment["effect"] | "" }
  | { t: "raw"; text: string; fi?: number }
  | { t: "file"; fi: number; file: ReviewFile; open: boolean; needs: boolean; exceptions: number; comments: number; holders: Holder[] }
  | { t: "verdict"; fi: number; file: ReviewFile }
  | { t: "note"; fi: number; text: string }
  | { t: "hunk"; fi: number; text: string }
  | { t: "gap"; fi: number; id: string; count: number }
  | { t: "line"; fi: number; kind: "context" | "add" | "remove" | "meta"; old: number | null; new: number | null; text: string; side: Side; num: number | null; marker: ReviewMarker | null; markerStart: boolean; exception: ReviewSuppression | null; jgFirst: boolean; chg: boolean; single: boolean; tokenSide: Side; rng: boolean }
  | { t: "cm"; fi: number; comment: DraftComment | SentComment; draft: boolean; single: boolean }
  | { t: "kv"; k: string; v: string }
  | { t: "json"; text: string }
  | { t: "said"; text: string }
  | { t: "mail"; m: import("../generated/views").LiveMessage; unread: boolean }
  | { t: "log"; at: string; text: string }
  | { t: "earlier"; repository: string; before: number }
  | { t: "verb"; command: string; what: string; server: string }
  | { t: "post"; post: import("./threads").Post; answered: import("./threads").Post | null; unread: boolean }
  | { t: "code"; n: number; text: string }
);

export type RowOf<T extends Row["t"]> = Extract<Row, { t: T }>;

/** A buffer: its rows, and where each file line stands among them, by `file:side:line`. */
export type Buffer = { rows: Row[]; index: Map<string, number> };

/** Everything a review's buffer is built from, beyond the review itself. */
export type BufferInput = {
  entry: Entry & { detail: ReviewDetail };
  view: PaneView;
  ui: ReviewUi;
  drafts: DraftComment[];
  holders: (path: string) => Holder[];
  runsIn: string;
};

/** A row before it is placed: every kind of row, without the index the builder gives it. */
export type Unplaced<R> = R extends unknown ? Omit<R, "i"> : never;

/** A row builder: each row pushed gets its index and a key unique in the buffer. */
export class Rows {
  readonly rows: Row[] = [];
  readonly index = new Map<string, number>();
  /** Place a row at the end, and say where it went. */
  push(row: Unplaced<Row>): number {
    const at = this.rows.length;
    const placed: Row = { ...row, i: at };
    this.rows.push(placed);
    return at;
  }
  buffer(): Buffer {
    return { rows: this.rows, index: this.index };
  }
}

/**
 * The whole review as one buffer: a stale notice, then the command whole with
 * its asking steps and the allowed ones folded into one line, then each file
 * as a fold under its header. A file the policy lets through on its own is a
 * folded header, so it is seen to exist without being read.
 */
export function reviewBuffer(input: BufferInput): Buffer {
  const { entry, view, ui, drafts, holders } = input;
  const { row, detail } = entry;
  const out = new Rows();
  const target = row.target;
  const sent = sentComments(detail);
  const judged = judgedOf(detail, row);
  const judgedSet = new Set(judged.flatMap((item) => item.kind === "file" ? item.lines.map((line) => `${item.fi}:${line.side}:${line.line}`) : []));
  const firstJudged = new Set(judged.flatMap((item) => item.kind === "file" && item.lines[0] !== undefined ? [`${item.fi}:${item.lines[0].side}:${item.lines[0].line}`] : []));

  if (row.state === "stale") out.push({ t: "msg", key: "stale", tone: "err", text: `Retired as stale: ${staleSentences(row).join("; ")}. No approval could release it any more; its session was told to re-read the file and ask again.` });
  if (detail.preview_unavailable !== "") out.push({ t: "msg", key: "unavailable", tone: "muted", text: detail.preview_unavailable });

  if (detail.command !== null) commandRows(out, detail, row, input.runsIn, ui.allowed, judged, target);

  if (detail.files.length === 0 && detail.command === null && detail.preview_unavailable === "") {
    const tool = detail.question.operation.tool;
    if (["Propose", "Edit", "MultiEdit", "Write", "apply_patch"].includes(tool)) {
      // A call that writes files but shows none is a projection that failed, never a reason to make its input the body.
      out.push({ t: "msg", key: "undocumented", tone: "err", text: `No document could be worked out for this ${headOf(entry).kind.toLowerCase()}: the dashboard does not read the form its input was recorded in. Nothing here is what an approval would write; its input is under full context (I).` });
    } else {
      out.push({ t: "sec", key: "called", text: `${tool} call · what it was called with`, sub: "" });
      const payload = detail.question.operation.payload;
      for (const [name, value] of Object.entries(payload)) {
        out.push({ t: "kv", key: `arg:${name}`, k: name, v: "" });
        const shown = typeof value === "string" ? value : JSON.stringify(value, null, 2);
        shown.split("\n").forEach((text, at) => out.push({ t: "raw", key: `arg:${name}:${at}`, text: `    ${text}` }));
      }
    }
  }

  detail.files.forEach((file, fi) => {
    const needs = needsReview(file);
    const open = needs ? !ui.closed.has(fi) : ui.opened.has(fi) || ui.full;
    const exceptions = exceptionsOf(file, ui.full);
    const fileDrafts = drafts.filter((comment) => comment.path === file.path);
    out.push({ t: "file", key: `file:${fi}`, fi, file, open, needs, exceptions: exceptions.length, comments: fileDrafts.length, holders: holders(file.path), jg: needs });
    if (!open) return;
    if (needs && file.review_reason !== "") out.push({ t: "verdict", key: `verdict:${fi}`, fi, file });
    if (file.about !== "") out.push({ t: "note", key: `note:${fi}`, fi, text: file.about });
    fileRows(out, { file, fi, view, ui, sent: sent.filter((comment) => comment.path === file.path), drafts: fileDrafts, judgedSet, firstJudged, exceptions });
  });
  return out.buffer();
}

/**
 * A shell command as the editor reads it. What asked comes first, each asking
 * step with its own rule and reason (the command as a whole where the policy
 * judged it whole); then the command itself, the asking parts marked; then,
 * folded together, the steps that were allowed on their own and the ones whose
 * effect no document can show before they run — information, never a cause.
 */
function commandRows(out: Rows, detail: ReviewDetail, row: ReviewSummary, runsIn: string, allowedOpen: boolean, judged: Judged[], target: string): void {
  const command = detail.command ?? "";
  const segments = detail.question.segments ?? [];
  const steps = detail.question.unpreviewed ?? [];
  const asking = segments.map((segment, si) => ({ segment, si })).filter(({ segment }) => segment.effect !== "allow");
  const allowed = segments.map((segment, si) => ({ segment, si })).filter(({ segment }) => segment.effect === "allow");
  const whole = segments.length <= 1;
  const verdictOf = (step: UnpreviewedStep) => segments.find((segment) => segment.command === step.command)?.effect;
  const quiet = steps.filter((step) => whole || verdictOf(step) !== "ask" && verdictOf(step) !== "deny");
  out.push({ t: "sec", key: "command", text: `$ command · runs in ${runsIn}`, sub: segments.length > 1 ? `${plural(segments.length, "step")} · ${asking.length} ${asking.length === 1 ? "asks" : "ask"} · ${allowed.length} allowed on their own` : "" });
  if (whole) {
    const asked: CommandSegment = { command: "", effect: "ask", rule: row.rule, reason: row.reason };
    out.push({ t: "seg", key: "seg:whole", segment: asked, si: 0, total: 1, allowed: false, jg: true });
    out.push({ t: "segwhy", key: "segwhy:whole", segment: asked });
  } else {
    for (const { segment, si } of asking) {
      out.push({ t: "seg", key: `seg:${si}`, segment, si, total: segments.length, allowed: false, jg: judged.some((item) => item.kind === "segment" && item.si === si) });
      out.push({ t: "segwhy", key: `segwhy:${si}`, segment });
      steps.forEach((step, at) => { if (step.command === segment.command) out.push({ t: "step", key: `step:${at}`, step, verdict: segment.effect }); });
    }
  }
  const marks: [number, number][] = asking.flatMap(({ segment }) => {
    const at = command.indexOf(segment.command);
    return segment.command !== "" && at >= 0 ? [[at, at + segment.command.length] as [number, number]] : [];
  });
  let offset = 0;
  command.split("\n").forEach((text, at) => {
    const start = offset;
    offset += text.length + 1;
    const local = marks.filter(([a, b]) => a < start + text.length && b > start).map(([a, b]): [number, number] => [Math.max(0, a - start), Math.min(text.length, b - start)]);
    out.push({ t: "cmd", key: `cmd:${at}`, text, marks: local });
  });
  const folded = (whole ? 0 : allowed.length) + quiet.length;
  if (folded === 0) return;
  const parts = [whole ? "" : `${plural(allowed.length, "step")} allowed on ${allowed.length === 1 ? "its" : "their"} own`, quiet.length > 0 ? `${plural(quiet.length, "step")} whose effect shows only after it runs` : ""];
  out.push({ t: "fold", key: "allowed", open: allowedOpen, text: parts.filter((part) => part !== "").join(" · ") });
  if (!allowedOpen) return;
  if (!whole) for (const { segment, si } of allowed) out.push({ t: "seg", key: `seg:${si}`, segment, si, total: segments.length, allowed: true });
  for (const step of quiet) {
    const at = steps.indexOf(step);
    out.push({ t: "step", key: `step:${at}`, step, verdict: verdictOf(step) ?? "" });
    step.paths.forEach((path, pi) => out.push({ t: "msg", key: `step:${at}:${pi}`, tone: "muted", text: `      leaves ${relative(path, target)}` }));
  }
}

type FileInput = {
  file: ReviewFile;
  fi: number;
  view: PaneView;
  ui: ReviewUi;
  sent: SentComment[];
  drafts: DraftComment[];
  judgedSet: Set<string>;
  firstJudged: Set<string>;
  exceptions: ReviewSuppression[];
};

type SourceLine = { kind: "context" | "add" | "remove" | "meta"; text: string; old_line: number | null; new_line: number | null; chg?: boolean };

function fileRows(out: Rows, input: FileInput): void {
  const { file, fi, view, ui, sent, drafts, judgedSet, firstJudged, exceptions } = input;
  const markers = { before: new Map<number, ReviewMarker>(), after: new Map<number, ReviewMarker>() };
  for (const marker of file.markers) {
    for (let line = marker.line; line <= marker.end_line; line += 1) markers[marker.side].set(line, marker);
  }
  const excepted = new Map(exceptions.map((suppression) => [suppression.line, suppression]));
  const ranged = [...sent, ...drafts];
  const commentRows = (side: Side, line: number, single: boolean) => {
    sent.filter((each) => each.side === side && each.end === line).forEach((comment, at) => out.push({ t: "cm", key: `cm:${fi}:${side}:${line}:sent:${at}`, fi, comment, draft: false, single }));
    drafts.filter((each) => each.side === side && each.end === line).forEach((comment) => out.push({ t: "cm", key: `cm:${comment.id}`, fi, comment, draft: true, single }));
  };
  const lineRow = (line: SourceLine, single: Side | null) => {
    const sides: [Side, number | null][] = line.kind === "remove" ? [["before", line.old_line]] : line.kind === "add" ? [["after", line.new_line]]
      : single !== null ? [[single, single === "before" ? line.old_line : line.new_line]] : [["after", line.new_line], ["before", line.old_line]];
    const [side, number] = sides[0] ?? ["after", null];
    const marker = number === null ? null : markers[side].get(number) ?? null;
    const exception = side === "after" && number !== null ? excepted.get(number) ?? null : null;
    const key = `l:${fi}:${line.old_line ?? "-"}:${line.new_line ?? "-"}:${line.kind}`;
    const placed = out.push({
      t: "line", key, fi, kind: line.kind, old: line.old_line, new: line.new_line, text: line.text.replace(/\n$/, ""),
      side, num: number, marker, markerStart: marker !== null && marker.line === number, exception,
      jg: sides.some(([each, at]) => judgedSet.has(`${fi}:${each}:${at}`)),
      jgFirst: sides.some(([each, at]) => firstJudged.has(`${fi}:${each}:${at}`)),
      chg: line.chg ?? false, single: single !== null, tokenSide: line.kind === "remove" ? "before" : single ?? "after",
      rng: sides.some(([each, at]) => at !== null && ranged.some((comment) => comment.side === each && at >= comment.start && at <= comment.end)),
    });
    for (const [each, at] of sides) if (at !== null) out.index.set(`${fi}:${each}:${at}`, placed);
    for (const [each, at] of sides) if (at !== null) commentRows(each, at, single !== null);
  };

  if (view === "raw") {
    file.unified.replace(/\n$/, "").split("\n").forEach((text, at) => out.push({ t: "raw", key: `raw:${fi}:${at}`, fi, text }));
    return;
  }
  if (view === "before" || view === "after") {
    const text = file[view];
    if (text === null) { out.push({ t: "msg", key: `absent:${fi}`, tone: "muted", text: view === "before" ? "File absent before the change." : "File absent after the change." }); return; }
    if (text === "") { out.push({ t: "msg", key: `empty:${fi}`, tone: "muted", text: "Empty file." }); return; }
    for (let number = 1; number <= lineCount(text); number += 1) {
      lineRow({ kind: "context", text: "", old_line: view === "before" ? number : null, new_line: view === "after" ? number : null }, view);
    }
    return;
  }
  if (file.unchanged) { out.push({ t: "msg", key: `unchanged:${fi}`, tone: "muted", text: "This leaves the file unchanged." }); return; }
  if (file.hunks.length === 0) { out.push({ t: "msg", key: `empty:${fi}`, tone: "muted", text: file.before === null ? "Creates an empty file." : "Deletes an empty file." }); return; }
  const after = (file.after ?? "").split("\n");
  const whole = ui.whole.has(fi);
  let nextOld = 1;
  let nextNew = 1;
  const gap = (count: number, id: string) => {
    if (count <= 0) return;
    if (!whole && !ui.gaps.has(`${fi}:${id}`)) { out.push({ t: "gap", key: `gap:${fi}:${id}`, fi, id, count }); return; }
    for (let offset = 0; offset < count; offset += 1) {
      lineRow({ kind: "context", text: after[nextNew - 1 + offset] ?? "", old_line: nextOld + offset, new_line: nextNew + offset }, null);
    }
  };
  file.hunks.forEach((hunk, hi) => {
    gap(hunk.new_start - nextNew, `gap-${hi}`);
    if (!whole) out.push({ t: "hunk", key: `hunk:${fi}:${hi}`, fi, text: hunk.header });
    let removing = false;
    for (const line of hunk.lines) {
      if (line.kind === "remove") removing = true;
      else if (line.kind !== "add") removing = false;
      lineRow({ ...line, chg: line.kind === "add" && removing }, null);
    }
    nextOld = hunk.old_end + 1;
    nextNew = hunk.new_end + 1;
  });
  gap(lineCount(file.after) - nextNew + 1, "gap-end");
}

/** The words a row shows, without its gutter or virtual text: what a search reads. */
export function rowText(row: Row): string {
  switch (row.t) {
    case "line": case "cmd": case "raw": case "msg": case "hunk": case "note": case "said": case "json": case "log": return row.text;
    case "sec": return `${row.text} ${row.sub}`;
    case "fold": return row.text;
    case "seg": return row.segment.command;
    case "segwhy": return `${row.segment.rule} ${row.segment.reason}`;
    case "step": return row.step.command;
    case "file": return row.file.path;
    case "verdict": return row.file.review_reason;
    case "gap": return "";
    case "cm": return row.comment.note;
    case "kv": return `${row.k} ${row.v}`;
    case "mail": return `${row.m.sender} ${row.m.recipient} ${row.m.text}`;
    case "earlier": return "Load earlier messages";
    case "verb": return `${row.command} ${row.what}`;
    case "post": return `${row.post.sender} ${row.post.text}`;
    case "code": return row.text;
  }
}

/** A document a window shows read-only after `gd`, as rows: one a line, numbered from one. */
export function codeBuffer(text: string): Buffer {
  const out = new Rows();
  text.replace(/\n$/, "").split("\n").forEach((line, at) => out.push({ t: "code", key: `code:${at + 1}`, n: at + 1, text: line }));
  return out.buffer();
}

/** The row a change or step move `{`/`}` stops at: a hunk, a file, a step, or the first changed line after unchanged ones. */
export function changeStop(rows: Row[], at: number): boolean {
  const row = rows[at];
  if (row === undefined) return false;
  if (row.t === "hunk" || row.t === "file" || row.t === "seg" || row.t === "sec") return true;
  const before = rows[at - 1];
  return row.t === "line" && row.kind !== "context" && before?.t === "line" && before.kind === "context";
}

/** Claims as the roster spells them, `at <path>` touched or `under <path>` locked, and whether one covers a path. */
export function claimCovers(claim: string, path: string): boolean {
  if (claim.startsWith("under ")) {
    const held = claim.slice("under ".length);
    return path === held || path.startsWith(`${held}/`);
  }
  return claim.startsWith("at ") && claim.slice("at ".length) === path;
}

export function claimPath(claim: string): string {
  if (claim.startsWith("under ")) return claim.slice("under ".length);
  if (claim.startsWith("at ")) return claim.slice("at ".length);
  return claim;
}
