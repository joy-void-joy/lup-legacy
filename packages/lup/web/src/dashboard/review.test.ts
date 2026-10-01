import { describe, expect, test } from "bun:test";
import type { ReviewDetail, ReviewFile, ReviewSummary } from "../generated/views";
import { changeStop, CLOSED_UI, headOf, headShort, headText, judgedOf, judgedSummary, lineSummary, markerStops, reviewBuffer, rowText, staleSentences, type DraftComment, type ReviewUi } from "./review";
import { stepWords } from "./Buffer";

const row: ReviewSummary = {
  key: "k", root_id: "root", id: "q1", state: "pending", requester: "lead", reason: "a protected path", operation: "Write in /p", rule: "edit:protected",
  title: "Update src/host.py", paths: ["src/host.py"], total_files: 1, created: "2026-09-24T12:00:00Z", answerable: true, unanswerable: "", said: 0,
  target: "/p", session: "lead", settled: null, archived: false, stale: [],
};

const before = Array.from({ length: 30 }, (_, at) => `line ${at + 1}`).join("\n") + "\n";
const after = before.replace("line 10\n", "line ten\n# lup: defer: later\n");

function file(fields: Partial<ReviewFile> = {}): ReviewFile {
  return {
    path: "/p/src/host.py", operation: "modify", before, after, review_effect: "ask", review_reason: "a protected path", unified: "", unchanged: false,
    additions: 2, deletions: 1, about: "the agent's own words", suppressions: [], review_label: { kind: "protected", words: "protected" },
    markers: [{ side: "after", line: 11, end_line: 11, kind: "defer", condition: null, text: "later" }],
    hunks: [{ header: "@@ -7,7 +7,8 @@", old_start: 7, old_end: 13, new_start: 7, new_end: 14, lines: [
      ...[7, 8, 9].map((at) => ({ kind: "context" as const, text: `line ${at}\n`, old_line: at, new_line: at, suppression: false })),
      { kind: "remove", text: "line 10\n", old_line: 10, new_line: null, suppression: false },
      { kind: "add", text: "line ten\n", old_line: null, new_line: 10, suppression: false },
      { kind: "add", text: "# lup: defer: later\n", old_line: null, new_line: 11, suppression: false },
      ...[11, 12, 13].map((at) => ({ kind: "context" as const, text: `line ${at}\n`, old_line: at, new_line: at + 1, suppression: false })),
    ] }],
    ...fields,
  };
}

function detail(fields: Partial<ReviewDetail> = {}, tool = "Write"): ReviewDetail {
  return {
    summary: row, files: [file()], command: null, thread: [], preview_unavailable: "", preview_notice: "", notification: null,
    question: {
      account: [], agent: "", answer: null, chain_resolved: true, changed: null, checkpoint_failure: "", completed: null, created: row.created, eligible: [], escalation: "",
      execution_id: "", execution_payload: null, expires: null, file_reviews: null, fingerprint: "f", id: "q1", member: "", moved: [], outcome: "", policy_identity: "",
      preconditions: {}, purpose: null, reason: row.reason, requirement: "human_only", resolved: {}, resumption: "native_retry", rule: row.rule, scheme: null,
      segments: null, state: "pending", stored: [], unpreviewed: null,
      operation: { cwd: "/p", escalation_normalized: "", escalation_raw: "", external: { effects: [] }, id: "o", kind: "unknown", mutations: { targets: [], effects: [] }, nested: [], network: { destinations: [], effects: [] }, payload: { file_path: "/p/src/host.py" }, placement: "ambient", provider: "", reads: { targets: [] }, requester: "lead", session: "s", supervisor: "", tool, worktree: "/p" },
    } as unknown as ReviewDetail["question"],
    ...fields,
  };
}

const build = (shown: ReviewDetail, ui: ReviewUi = CLOSED_UI, drafts: DraftComment[] = []) =>
  reviewBuffer({ entry: { row, detail: shown }, view: "diff", ui, drafts, holders: () => [], runsIn: "tree/dev" });

describe("a review as the editor reads it", () => {
  test("the head says what the call is, from its detail where it was read and its title before", () => {
    expect(headText(headOf({ row, detail: detail() }))).toBe("Write · replaces the file · src/host.py");
    expect(headShort(headOf({ row, detail: null }))).toBe("Update src/host.py");
    const segments = [{ command: "cd x", effect: "allow" as const, reason: "", rule: "" }, { command: "rm -rf build", effect: "ask" as const, reason: "removes a tree", rule: "shell:rm" }];
    const shell = detail({ command: "cd x && rm -rf build", files: [], question: { ...detail().question, segments } }, "Bash");
    shell.question.operation.tool = "Bash";
    expect(headText(headOf({ row, detail: shell }))).toBe("Shell command · 2 steps, 1 needs approval");
  });

  test("what the policy asks about: the changed lines of a file, or the steps that asked", () => {
    const judged = judgedOf(detail(), row);
    expect(judged).toHaveLength(1);
    const first = judged[0];
    expect(first?.kind === "file" && lineSummary(first.lines)).toBe("L10-11");
    expect(judgedSummary(judged, row, "gd walks them")).toBe("? edit:protected — host.py: a protected path");
  });

  test("one buffer: the file under its header, the agent's note, the gaps folded, and the changed lines judged", () => {
    const { rows, index } = build(detail());
    expect(rows.map((each) => each.t).slice(0, 5)).toEqual(["file", "verdict", "note", "gap", "hunk"]);
    expect(rows.find((each) => each.t === "gap" && each.count === 6)).toBeDefined();
    const changed = rows.filter((each) => each.t === "line" && each.jg === true).map((each) => rowText(each));
    expect(changed).toEqual(["line 10", "line ten", "# lup: defer: later"]);
    const marked = rows.find((each) => each.t === "line" && each.marker !== null);
    expect(marked?.t === "line" && marked.markerStart && marked.new === 11).toBe(true);
    expect(index.get("0:after:12")).toBeDefined();
    expect(markerStops(detail(), false)).toHaveLength(1);
    expect(rows.findIndex((_, at) => changeStop(rows, at))).toBe(0);
  });

  test("folds: a file folds to its header, a gap opens in place, the whole file shows every line", () => {
    expect(build(detail(), { ...CLOSED_UI, closed: new Set([0]) }).rows.map((each) => each.t)).toEqual(["file"]);
    const opened = build(detail(), { ...CLOSED_UI, gaps: new Set(["0:gap-0"]) }).rows;
    expect(opened.filter((each) => each.t === "line" && each.kind === "context" && (each.new ?? 0) < 7)).toHaveLength(6);
    const whole = build(detail(), { ...CLOSED_UI, whole: new Set([0]) }).rows;
    expect(whole.some((each) => each.t === "gap" || each.t === "hunk")).toBe(false);
    expect(whole.filter((each) => each.t === "line")).toHaveLength(32);
  });

  test("a file the policy let through on its own is a folded header until opened", () => {
    const allowed = detail({ files: [file({ review_effect: "allow" })] });
    expect(build(allowed).rows.map((each) => each.t)).toEqual(["file"]);
    expect(build(allowed, { ...CLOSED_UI, full: true }).rows.length).toBeGreaterThan(1);
  });

  test("a draft comment sits under the last line of its range, which is marked", () => {
    const { rows } = build(detail(), CLOSED_UI, [{ id: "d1", path: "/p/src/host.py", side: "after", start: 10, end: 11, note: "why?" }]);
    const at = rows.findIndex((each) => each.t === "cm");
    const above = rows[at - 1];
    expect(above?.t === "line" && above.new === 11 && above.rng).toBe(true);
  });

  test("a call that writes files but shows none says so rather than making its input the body", () => {
    const { rows } = build(detail({ files: [] }, "Propose"));
    expect(rowText(rows[0] ?? { i: 0, key: "", t: "msg", tone: "", text: "" })).toContain("No document could be worked out for this proposal");
  });

  test("a shell command shows what asked first; steps no document shows are folded, quiet, with what is known of them", () => {
    // Shaped like b722c695: the redirection asked, the policy judged the line whole, and two later steps show their effect only once run.
    const asked = { ...row, rule: "", reason: "the redirection creates /other/tree/tmp/history/doc.py, an outside path" };
    const heredoc = "cat > /other/tree/tmp/history/doc.py <<'EOF'\nprint('x')\nEOF\nuv run python tmp/history/doc.py && uv run python tmp/history/notes.py";
    const unpreviewed = [{ cause: "run" as const, command: "uv run python tmp/history/doc.py", paths: [] }, { cause: "run" as const, command: "uv run python tmp/history/notes.py", paths: [] }];
    const shell = detail({ command: heredoc, files: [file({ review_effect: "allow", review_label: { kind: "automatic", words: "automatic" } })], question: { ...detail().question, segments: [], unpreviewed } }, "Bash");
    shell.question.operation.tool = "Bash";
    const shown = (ui: ReviewUi) => reviewBuffer({ entry: { row: asked, detail: shell }, view: "diff", ui, drafts: [], holders: () => [], runsIn: "tree/dev" }).rows;
    const closed = shown(CLOSED_UI);
    expect(closed.slice(0, 3).map((each) => each.t)).toEqual(["sec", "seg", "segwhy"]);
    expect(closed[1]?.jg).toBe(true);
    expect(closed.some((each) => each.t === "step")).toBe(false);
    const fold = closed.find((each) => each.t === "fold");
    expect(fold?.t === "fold" && fold.text).toBe("2 steps whose effect shows only after it runs");
    expect(judgedSummary(judgedOf(shell, asked), asked, "gd walks them")).toBe("? unattributed — the redirection creates /other/tree/tmp/history/doc.py, an outside path");
    expect(headText(headOf({ row: asked, detail: shell }))).toBe("Shell command · 4-line script");
    const opened = shown({ ...CLOSED_UI, allowed: true }).filter((each) => each.t === "step");
    expect(opened.map((each) => each.t === "step" && stepWords(each))).toEqual(["effect shown only after it runs", "effect shown only after it runs"]);
  });

  test("a step no document shows says it was allowed on its own where its verdict says so, and the step that asked comes first", () => {
    // Shaped like 65bf2101: the formatter and the tests ran on their own verdicts; the asking step is first.
    const segments = [
      { command: "cat > /other/tmp/t.py", effect: "ask" as const, reason: "the redirection creates /other/tmp/t.py, an outside path", rule: "shell:outside-redirection" },
      { command: "uv run ruff format t.py", effect: "allow" as const, reason: "", rule: "shell:formatter" },
      { command: "uv run pytest -q t.py", effect: "allow" as const, reason: "", rule: "shell:tests" },
    ];
    const unpreviewed = [{ cause: "run" as const, command: "uv run ruff format t.py", paths: [] }, { cause: "run" as const, command: "uv run pytest -q t.py", paths: [] }];
    const shell = detail({ command: segments.map((each) => each.command).join(" && "), files: [], question: { ...detail().question, segments, unpreviewed } }, "Bash");
    const rows = reviewBuffer({ entry: { row, detail: shell }, view: "diff", ui: { ...CLOSED_UI, allowed: true }, drafts: [], holders: () => [], runsIn: "tree/dev" }).rows;
    expect(rows.slice(1, 3).map((each) => each.t === "seg" || each.t === "segwhy" ? `${each.t}:${each.segment.rule}` : each.t)).toEqual(["seg:shell:outside-redirection", "segwhy:shell:outside-redirection"]);
    expect(rows.filter((each) => each.t === "step").map((each) => each.t === "step" && stepWords(each))).toEqual(["effect shown only after it runs · allowed on its own", "effect shown only after it runs · allowed on its own"]);
    expect(judgedSummary(judgedOf(shell, row), row, "gd walks them")).toBe("? shell:outside-redirection — step 1: the redirection creates /other/tmp/t.py, an outside path");
  });

  test("what went stale reads as a sentence per file", () => {
    expect(staleSentences({ ...row, stale: [{ path: "/p/a.py", cause: "deleted", read: false }] })).toEqual(["a.py was deleted since this was recorded"]);
  });
});
