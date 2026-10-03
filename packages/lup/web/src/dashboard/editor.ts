// What the keys do inside the windows: the cursor in a pane, the tree's row,
// the context's item; folds, the jumps between files, exceptions, markers and
// what the policy asked about; line comments and visual ranges; splits; the
// `/` search; hover. Each reads the controller's state and moves it.
import type { LiveMessage, ReviewRoot } from "../generated/views";
import { lastColumn, lineText, words } from "./caret";
import { draftId, type Dashboard } from "./dashboard";
import { basename, changeStop, exceptionRules, exceptionStops, judgedOf, lineCount, markerLabel, markerStops, needsReview, rowText, type PaneView, type Row, type Side } from "./review";
import type { Win } from "./state";
import { activityBrief, counterpart, inboxOf, memberById, type TreeItem } from "./supervision";
import { discussionLine, type Post } from "./threads";

type How = "nearest" | "center" | "top";

export function rowsOf(d: Dashboard, pane: 0 | 1 = d.state.pane): Row[] {
  return d.buffer(pane).rows;
}

export function rowHere(d: Dashboard): Row | undefined {
  const state = d.state;
  return rowsOf(d)[state.editor[state.pane].cur];
}

export function setCursor(d: Dashboard, pane: 0 | 1, cur: number, how: How = "nearest"): void {
  const rows = rowsOf(d, pane);
  if (rows.length === 0) return;
  const clamped = Math.min(Math.max(0, cur), rows.length - 1);
  d.scroll[pane] = how;
  d.set((state) => {
    const editor: typeof state.editor = [...state.editor];
    editor[pane] = { ...editor[pane], cur: clamped };
    return { editor };
  });
}

/**
 * Down or up where focus is (decision 118): the tree's next row, opening it
 * (History's next review, the next discussion in Threads); the context's next
 * item; the buffer's next line, keeping the column the operator chose.
 */
export function moveCursor(d: Dashboard, delta: number): void {
  const state = d.state;
  switch (state.focus) {
    case "context": moveContext(d, delta); return;
    case "queue":
      if (state.view === "history") moveReview(d, delta, "queue");
      else if (state.view === "threads") d.moveThread(delta);
      else moveTree(d, delta);
      return;
    case "setup": d.set({ setupAt: Math.min(Math.max(0, state.setupAt + delta), Math.max(0, (state.panes?.length ?? 1) - 1)) }); return;
    default: setCursor(d, state.pane, state.editor[state.pane].cur + delta);
  }
}

// ───────────────────────────── the cursor's column ─────────────────────────────

/** The row element a pane draws for a buffer row. */
export const rowElement = (d: Dashboard, pane: 0 | 1, index: number) => d.elements.panes[pane]?.querySelector(`.r[data-i="${index}"]`) ?? null;

/** The column the caret stands on: what the pane wants, as far as its line reaches. */
export function columnOf(d: Dashboard, pane: 0 | 1 = d.state.pane): number {
  const at = d.state.editor[pane];
  return Math.min(at.want, lastColumn(lineText(rowElement(d, pane, at.cur))));
}

/** Put the cursor on a row and a column, the column being what it wants from then on. */
export function placeCursor(d: Dashboard, pane: 0 | 1, cur: number, column: number, how: How = "nearest"): void {
  d.set((state) => {
    const editor: typeof state.editor = [...state.editor];
    editor[pane] = { ...editor[pane], want: Math.max(0, column) };
    return { editor };
  });
  setCursor(d, pane, cur, how);
}

/** `h`/`l`: a character left or right on the line, no further than its ends. */
export function cursorColumn(d: Dashboard, delta: number): void {
  const pane = d.state.pane;
  const text = lineText(rowElement(d, pane, d.state.editor[pane].cur));
  placeCursor(d, pane, d.state.editor[pane].cur, Math.min(Math.max(0, columnOf(d, pane) + delta), lastColumn(text)));
}

/** `0`, `^` and `$`: the line's start, its first non-blank, or its end, which `$` keeps wanting on lines after. */
export function lineEdge(d: Dashboard, where: "start" | "first" | "end"): void {
  const pane = d.state.pane;
  const cur = d.state.editor[pane].cur;
  const text = lineText(rowElement(d, pane, cur));
  const column = where === "start" ? 0 : where === "first" ? Math.max(0, text.search(/\S/)) : Number.POSITIVE_INFINITY;
  placeCursor(d, pane, cur, column);
}

/** `w`, `b` and `e`, as Vim's small words, across lines; a count repeats the move. */
export function wordMotion(d: Dashboard, kind: "next" | "back" | "end", count: number): void {
  const pane = d.state.pane;
  const total = rowsOf(d, pane).length;
  let row = d.state.editor[pane].cur;
  let column = columnOf(d, pane);
  for (let step = 0; step < count; step += 1) {
    for (let guard = 0; guard <= total; guard += 1) {
      const found = words(lineText(rowElement(d, pane, row)));
      const target = kind === "next" ? found.find((word) => word.start > column)?.start
        : kind === "end" ? found.find((word) => word.end > column)?.end
          : [...found].reverse().find((word) => word.start < column)?.start;
      if (target !== undefined) { column = target; break; }
      if (kind === "back") {
        if (row === 0) { column = 0; break; }
        row -= 1;
        column = Number.POSITIVE_INFINITY;
      } else {
        if (row >= total - 1) break;
        row += 1;
        column = -1;
      }
    }
  }
  placeCursor(d, pane, row, Math.max(0, Math.min(column, lastColumn(lineText(rowElement(d, pane, row))))));
}

export function moveContext(d: Dashboard, delta: number): void {
  const count = d.elements.context?.querySelectorAll(".it").length ?? 0;
  if (count === 0) return;
  d.set((state) => ({ ctxCur: Math.min(Math.max(0, (state.ctxCur < 0 ? (delta > 0 ? -1 : count) : state.ctxCur) + delta), count - 1) }));
}

/** How many rows half the focused pane shows. */
function halfRows(d: Dashboard): number {
  const element = d.elements.panes[d.state.pane];
  const line = element === null ? 18 : Number.parseFloat(getComputedStyle(element).lineHeight) || 18;
  return Math.max(1, Math.floor((element?.clientHeight ?? 360) / line / 2));
}

export function halfPage(d: Dashboard, sign: 1 | -1): void {
  moveCursor(d, sign * halfRows(d));
}

/** The top or the bottom of the focused window. */
export function edge(d: Dashboard, where: "top" | "bottom"): void {
  const state = d.state;
  if (state.focus === "queue") {
    if (state.view === "history") {
      const walked = d.walked();
      const target = where === "top" ? walked[0] : walked[walked.length - 1];
      if (target !== undefined) d.openReview(target.key, { mode: "queue" });
      return;
    }
    if (state.view === "threads") {
      const all = d.discussions();
      const target = where === "top" ? all[0] : all[all.length - 1];
      if (target !== undefined) d.openThread(target.key, "queue");
      return;
    }
    const items = d.tree();
    d.set({ treeCur: where === "top" ? 0 : Math.max(0, items.length - 1) });
    moveTree(d, 0);
    return;
  }
  if (state.focus === "context") {
    const count = d.elements.context?.querySelectorAll(".it").length ?? 0;
    d.set({ ctxCur: where === "top" ? 0 : Math.max(0, count - 1) });
    return;
  }
  setCursor(d, state.pane, where === "top" ? 0 : rowsOf(d).length - 1, where === "top" ? "top" : "nearest");
}

/** Walk the tree's rows, opening each as it goes and keeping focus in the tree. */
export function moveTree(d: Dashboard, delta: number): void {
  const items = d.tree();
  if (items.length === 0) return;
  const cur = Math.min(Math.max(0, d.state.treeCur + delta), items.length - 1);
  d.set({ treeCur: cur });
  const item = items[cur];
  if (item !== undefined) openItem(d, item, true);
}

export function openItem(d: Dashboard, item: TreeItem, stay: boolean): void {
  switch (item.t) {
    case "repo": d.openOther("repo", item.key, stay ? "queue" : "normal"); return;
    case "you": d.openOther("you", item.key, stay ? "queue" : "normal"); return;
    case "member": d.openOther("member", item.key, stay ? "queue" : "normal"); return;
    case "review": d.openReview(item.key, { mode: stay ? "queue" : "box" }); return;
    case "folded": toggleStopped(d, item.key); return;
  }
}

export function toggleStopped(d: Dashboard, repository: string): void {
  d.set((state) => {
    if (state.showStopped) return { showStopped: false, stoppedOpen: new Set() };
    const open = new Set(state.stoppedOpen);
    if (open.has(repository)) open.delete(repository);
    else open.add(repository);
    return { stoppedOpen: open };
  });
}

/** `za` in the tree: fold an agent's subtree or a repository, or show a repository's stopped agents. */
export function foldTree(d: Dashboard): void {
  const item = d.tree()[d.state.treeCur];
  if (item === undefined) return;
  if (item.t === "folded") { toggleStopped(d, item.key); return; }
  if (item.t !== "member" && item.t !== "repo") return;
  d.set((state) => {
    const collapsed = new Set(state.collapsed);
    if (collapsed.has(item.key)) collapsed.delete(item.key);
    else collapsed.add(item.key);
    return { collapsed };
  });
}

/** The next or previous review in the list `j`/`k` walk. */
export function moveReview(d: Dashboard, delta: number, how: "normal" | "box" | "keep" | "queue" = "normal"): void {
  const walked = d.walked();
  const open = d.state.sel.kind === "review" ? d.state.sel.key : "";
  const position = walked.findIndex((row) => row.key === open);
  const target = walked[position < 0 ? (delta > 0 ? 0 : walked.length - 1) : position + delta];
  if (target !== undefined) d.openReview(target.key, { mode: how });
  else d.say(walked.length === 0 ? (d.state.view === "history" ? "no answered requests yet" : "nothing waits on you") : delta > 0 ? "last request in this list" : "first request in this list");
}

/** `(`/`)`: the previous or next agent in the tree's order. */
export function moveAgent(d: Dashboard, delta: number): void {
  const agents = d.tree().filter((item) => item.t === "member");
  if (agents.length === 0) return;
  const state = d.state;
  const open = d.current();
  const from = state.sel.kind === "member" ? state.sel.key : open !== null ? d.asker(open.row)?.key ?? "" : "";
  const position = agents.findIndex((item) => item.key === from);
  const target = agents[position < 0 ? (delta > 0 ? 0 : agents.length - 1) : Math.min(Math.max(0, position + delta), agents.length - 1)];
  if (target !== undefined) d.openOther("member", target.key, state.focus === "queue" ? "queue" : "normal");
}

/** In the inbox, the previous or next message. */
export function moveInbox(d: Dashboard, delta: number): void {
  const rows = rowsOf(d, 0);
  const mails = rows.filter((row) => row.t === "mail").map((row) => row.i);
  if (mails.length === 0) return;
  const cur = d.state.editor[0].cur;
  const position = mails.findIndex((index) => index >= cur);
  const at = position < 0 ? mails.length - 1 : mails[position] === cur ? position : position - (delta > 0 ? 1 : 0);
  const target = mails[Math.min(Math.max(0, at + delta), mails.length - 1)];
  if (target !== undefined) setCursor(d, 0, target);
}

function fileRow(rows: Row[], fi: number): number {
  return rows.findIndex((row) => row.t === "file" && row.fi === fi);
}

/** Focus the editor and put its cursor on a row, centred. */
export function jumpTo(d: Dashboard, row: number, how: How = "center"): boolean {
  if (row < 0) return false;
  if (d.state.focus !== "editor") d.focusWin("editor");
  setCursor(d, d.state.pane, row, how);
  return true;
}

export function moveFile(d: Dashboard, delta: number): void {
  const entry = d.current();
  if (entry?.detail === null || entry === null || entry.detail.files.length === 0) { d.say("this review changes no file"); return; }
  const rows = rowsOf(d);
  const at = rowHere(d);
  const here = at !== undefined && "fi" in at && at.fi !== undefined ? at.fi : delta > 0 ? -1 : entry.detail.files.length;
  const next = here + delta;
  if (next < 0 || next >= entry.detail.files.length) { d.say(delta > 0 ? "last file" : "first file"); return; }
  jumpTo(d, fileRow(rows, next), "top");
}

/** Show one line of one file where it stands: its fold opened, and the whole file where no hunk holds it. */
export function reveal(d: Dashboard, fi: number, side: Side, line: number): boolean {
  const entry = d.current();
  if (entry === null || entry.detail === null) return false;
  const file = entry.detail.files[fi];
  if (file === undefined) return false;
  const pane = d.state.editor[d.state.pane];
  const shown = file.hunks.some((hunk) => hunk.lines.some((row) => side === "after" ? row.new_line === line : row.old_line === line));
  d.setUi(entry.row.key, (ui) => {
    const closed = new Set(ui.closed);
    closed.delete(fi);
    const opened = new Set(ui.opened);
    if (!needsReview(file)) opened.add(fi);
    const whole = new Set(ui.whole);
    if (pane.view === "diff" && !shown) whole.add(fi);
    return { closed, opened, whole };
  });
  const row = d.buffer(d.state.pane).index.get(`${fi}:${side}:${line}`);
  return row !== undefined && jumpTo(d, row);
}

export function moveException(d: Dashboard, delta: number): void {
  const entry = d.current();
  if (entry === null || entry.detail === null) return;
  const full = d.ui(entry.row.key).full;
  const stops = exceptionStops(entry.detail, full);
  if (stops.length === 0) { d.say(`no rule exceptions in this review${full ? "" : "; F shows existing ones"}`); return; }
  const at = d.state.exceptionAt < 0 ? (delta > 0 ? 0 : stops.length - 1) : (d.state.exceptionAt + delta + stops.length) % stops.length;
  const stop = stops[at];
  if (stop === undefined) return;
  d.set({ exceptionAt: at });
  reveal(d, stop.fi, "after", stop.suppression.line);
  d.say(`exception ${at + 1}/${stops.length}: ${exceptionRules(stop.suppression)} — ${stop.suppression.reason || "no reason supplied"}`);
}

export function moveMarker(d: Dashboard, delta: number): void {
  const entry = d.current();
  if (entry === null || entry.detail === null) return;
  const stops = markerStops(entry.detail, d.ui(entry.row.key).full);
  if (stops.length === 0) { d.say("no # lup: markers in this review's files"); return; }
  const at = d.state.markerAt < 0 ? (delta > 0 ? 0 : stops.length - 1) : (d.state.markerAt + delta + stops.length) % stops.length;
  const stop = stops[at];
  if (stop === undefined) return;
  d.set({ markerAt: at });
  reveal(d, stop.fi, stop.marker.side, stop.marker.line);
  d.say(`marker ${at + 1}/${stops.length}: ${markerLabel(stop.marker)} — ${stop.marker.text.split("\n")[0] ?? ""}`);
}

/** `gd`: what the policy asked about, and again for the next part. */
export function gotoJudged(d: Dashboard, index?: number): void {
  const entry = d.current();
  if (entry === null || entry.detail === null) return;
  const judged = judgedOf(entry.detail, entry.row);
  if (judged.length === 0) { d.say("the policy asked about nothing in particular here"); return; }
  const at = index ?? (d.state.judgedAt < 0 ? 0 : (d.state.judgedAt + 1) % judged.length);
  const item = judged[at];
  if (item === undefined) return;
  d.set({ judgedAt: at });
  const rows = rowsOf(d);
  switch (item.kind) {
    case "segment": jumpTo(d, rows.findIndex((row) => row.t === "seg" && row.si === item.si)); break;
    case "command": jumpTo(d, rows.findIndex((row) => row.key === "seg:whole")); break;
    case "file": {
      const first = item.lines[0];
      if (first !== undefined) reveal(d, item.fi, first.side, first.line);
      else jumpTo(d, fileRow(rows, item.fi), "top");
    }
  }
  d.say(`${at + 1}/${judged.length} the policy asks: ${item.rule || entry.row.rule || "unattributed"} — ${item.reason || entry.row.reason}`);
}

/** `f`: the whole file with its changes marked in place, or back to the diff. */
export function toggleWhole(d: Dashboard): void {
  const entry = d.current();
  if (entry === null || entry.detail === null || entry.detail.files.length === 0) return;
  const at = rowHere(d);
  const fi = at !== undefined && "fi" in at && at.fi !== undefined ? at.fi : 0;
  const keep = at?.t === "line" ? `${fi}:${at.side}:${at.num ?? ""}` : "";
  let whole = false;
  d.setUi(entry.row.key, (ui) => {
    const next = new Set(ui.whole);
    if (next.has(fi)) next.delete(fi);
    else next.add(fi);
    whole = next.has(fi);
    return { whole: next };
  });
  const buffer = d.buffer(d.state.pane);
  setCursor(d, d.state.pane, (keep === "" ? undefined : buffer.index.get(keep)) ?? fileRow(buffer.rows, fi), "center");
  const file = entry.detail.files[fi];
  d.say(whole ? `whole file: ${basename(file?.path ?? "")}, changes marked in place` : "back to the diff");
}

/** `F`: the full operation, automatic files and existing exceptions too. */
export function toggleFull(d: Dashboard): void {
  const entry = d.current();
  if (entry === null) return;
  let full = false;
  d.setUi(entry.row.key, (ui) => { full = !ui.full; return { full }; });
  d.say(full ? "full operation: every file, and existing exceptions" : "needs review: automatic files folded, new exceptions only");
}

/** Fold or unfold what the cursor is on: a file, a gap, the allowed steps; or every file at once. */
export function fold(d: Dashboard, action: "toggle" | "open" | "close" | "all-open" | "all-close"): void {
  const entry = d.current();
  if (entry === null || entry.detail === null) return;
  const detail = entry.detail;
  const at = rowHere(d);
  if (action === "all-open" || action === "all-close") {
    const every = new Set(detail.files.map((_, fi) => fi));
    d.setUi(entry.row.key, () => action === "all-open" ? { closed: new Set(), opened: every, allowed: true } : { closed: every, opened: new Set(), allowed: false });
    return;
  }
  if (at === undefined) return;
  const pane = d.state.pane;
  const cur = d.state.editor[pane].cur;
  switch (at.t) {
    case "gap": d.setUi(entry.row.key, (ui) => ({ gaps: new Set([...ui.gaps, `${at.fi}:${at.id}`]) })); break;
    case "fold": d.setUi(entry.row.key, (ui) => ({ allowed: action === "toggle" ? !ui.allowed : action === "open" })); break;
    default: {
      if (!("fi" in at) || at.fi === undefined) return;
      const fi = at.fi;
      const file = detail.files[fi];
      if (file === undefined) return;
      d.setUi(entry.row.key, (ui) => {
        const open = needsReview(file) ? !ui.closed.has(fi) : ui.opened.has(fi) || ui.full;
        const want = action === "toggle" ? !open : action === "open";
        const closed = new Set(ui.closed);
        const opened = new Set(ui.opened);
        if (needsReview(file)) {
          if (want) closed.delete(fi);
          else closed.add(fi);
        } else if (want) opened.add(fi);
        else opened.delete(fi);
        return { closed, opened };
      });
      if (action !== "open") { setCursor(d, pane, fileRow(rowsOf(d), fi)); return; }
    }
  }
  setCursor(d, pane, cur);
}

/** `{`/`}`: the previous or next change, step or file in a review; the previous or next section, post or message anywhere else. */
export function changeJump(d: Dashboard, delta: 1 | -1): void {
  const rows = rowsOf(d);
  const cur = d.state.editor[d.state.pane].cur;
  const review = d.centerKind() === "review";
  const stops = (at: number) => review ? changeStop(rows, at) : ["sec", "post", "mail"].includes(rows[at]?.t ?? "");
  for (let at = cur + delta; at >= 0 && at < rows.length; at += delta) {
    if (stops(at)) { placeCursor(d, d.state.pane, at, review ? d.state.editor[d.state.pane].want : 0, "center"); return; }
  }
  d.say(review ? (delta > 0 ? "no change below" : "no change above") : delta > 0 ? "no section below" : "no section above");
}

// ───────────────────────────── line comments ─────────────────────────────

/** Open a draft comment on the lines picked: one line, or a range from the last one picked. */
export function pickLine(d: Dashboard, side: Side, line: number, extend: boolean, fi: number): void {
  const entry = d.current();
  if (entry === null || entry.detail === null || entry.row.state !== "pending") { d.say("comments go on a waiting review; this one is settled"); return; }
  const file = entry.detail.files[fi];
  if (file === undefined) return;
  const key = entry.row.key;
  const state = d.state;
  const anchor = state.anchor;
  const from = extend && anchor !== null && anchor.fi === fi && anchor.side === side ? anchor.line : line;
  const start = Math.min(from, line);
  const end = Math.max(from, line);
  if (!extend) d.set({ anchor: { fi, side, line } });
  const draft = d.draft(key);
  const same = draft.comments.find((comment) => comment.path === file.path && comment.side === side && comment.start === start && comment.end === end);
  if (same !== undefined) { d.set({ editing: same.id }); return; }
  const active = draft.comments.find((comment) => comment.id === state.editing);
  if (extend && active !== undefined && active.side === side && active.path === file.path) {
    d.setDraft(key, (now) => ({ comments: now.comments.map((comment) => comment.id === active.id ? { ...comment, start, end } : comment) }));
    return;
  }
  const id = draftId();
  d.setDraft(key, (now) => ({ comments: [...now.comments.filter((comment) => comment.id !== state.editing || comment.note.trim() !== ""), { id, path: file.path, side, start, end, note: "" }] }));
  d.set({ editing: id, focus: "editor" });
}

/** `i`, `a`, `o` or Enter on a line: a comment on it; on a fold, open it; on a draft, edit it. */
export function commentAtCursor(d: Dashboard): void {
  const at = rowHere(d);
  if (at === undefined) return;
  if (at.t === "cm" && at.draft && "id" in at.comment) { d.set({ editing: at.comment.id }); return; }
  if (at.t === "gap" || at.t === "fold" || at.t === "file") { fold(d, "toggle"); return; }
  if (at.t !== "line" || at.num === null) { d.say("comments anchor to a line of a file; move to one"); return; }
  pickLine(d, at.side, at.num, false, at.fi);
}

/** Leave the comment being written; an empty one is dropped. */
export function closeComment(d: Dashboard): void {
  const entry = d.current();
  const editing = d.state.editing;
  if (entry !== null && editing !== null) {
    d.setDraft(entry.row.key, (draft) => ({ comments: draft.comments.filter((comment) => comment.id !== editing || comment.note.trim() !== "") }));
  }
  d.set({ editing: null });
  d.focusWin("editor");
}

/** `x` on a review: delete the draft comment on this line; `u` brings it back. */
export function deleteDraft(d: Dashboard): void {
  const entry = d.current();
  const at = rowHere(d);
  if (entry === null || entry.detail === null || at === undefined) return;
  const draft = d.draft(entry.row.key);
  const file = "fi" in at && at.fi !== undefined ? entry.detail.files[at.fi] : undefined;
  const target = at.t === "cm" && at.draft && "id" in at.comment ? draft.comments.find((comment) => "id" in at.comment && comment.id === at.comment.id)
    : at.t === "line" && at.num !== null && file !== undefined ? draft.comments.find((comment) => comment.path === file.path && comment.side === at.side && at.num !== null && at.num >= comment.start && at.num <= comment.end) : undefined;
  if (target === undefined) { d.say("no draft comment on this line"); return; }
  d.setDraft(entry.row.key, (now) => ({ comments: now.comments.filter((comment) => comment.id !== target.id) }));
  d.set({ deleted: { key: entry.row.key, comment: target }, editing: null });
  setCursor(d, d.state.pane, Math.min(d.state.editor[d.state.pane].cur, rowsOf(d).length - 1));
  d.say("draft comment deleted · u restores it");
}

export function undoDelete(d: Dashboard): void {
  const deleted = d.state.deleted;
  if (deleted === null) { d.say("already at oldest change"); return; }
  d.setDraft(deleted.key, (draft) => ({ comments: [...draft.comments, deleted.comment] }));
  d.set({ deleted: null });
  d.say("draft comment restored");
}

/** The lines a visual range covers: from where it started to the cursor, on one side of one file. */
export function visualSpan(d: Dashboard): { fi: number; side: Side; start: number; end: number } | null {
  const visual = d.state.visual;
  if (visual === null) return null;
  const rows = rowsOf(d, visual.pane);
  const anchor = rows[visual.anchor];
  const at = rows[d.state.editor[visual.pane].cur];
  if (anchor?.t !== "line" || anchor.num === null) return null;
  // A range stays on the side it started on: the cursor's line there, or the
  // nearest line between that the side has, where the cursor's has none.
  const side = anchor.side;
  const number = (row: Row | undefined) => row?.t === "line" && row.fi === anchor.fi ? (side === "before" ? row.old : row.new) : null;
  const cur = d.state.editor[visual.pane].cur;
  const between = rows.slice(Math.min(cur, visual.anchor), Math.max(cur, visual.anchor) + 1).map(number).filter((each): each is number => each !== null);
  const end = number(at) ?? (cur > visual.anchor ? between[between.length - 1] : between[0]) ?? anchor.num;
  return { fi: anchor.fi, side, start: Math.min(anchor.num, end), end: Math.max(anchor.num, end) };
}

export function startVisual(d: Dashboard): void {
  const at = rowHere(d);
  if (at?.t !== "line" || at.num === null) { d.say("visual mode starts on a line of a file"); return; }
  if (d.current()?.row.state !== "pending") { d.say("comments go on a waiting review; this one is settled"); return; }
  d.set((state) => ({ visual: { pane: state.pane, anchor: state.editor[state.pane].cur } }));
  d.say(d.state.narrow ? "tap another line to stretch the range, then Comment" : "V-LINE: move with ↑↓ or j/k, then gc, c or Enter comments on the lines; Esc cancels");
}

export function commitVisual(d: Dashboard): void {
  const span = visualSpan(d);
  d.set({ visual: null });
  if (span === null) return;
  d.set({ anchor: { fi: span.fi, side: span.side, line: span.start } });
  pickLine(d, span.side, span.end, true, span.fi);
}

export function cancelVisual(d: Dashboard): void {
  d.set({ visual: null });
}

// ───────────────────────────── windows ─────────────────────────────

export function split(d: Dashboard, kind: "" | "v" | "s"): void {
  if (d.centerKind() !== "review") { d.say("splits hold a review's documents; open one first"); return; }
  d.set((state) => {
    const left = kind === "v" ? "before" : kind === "" ? "diff" : state.editor[0].view;
    const right = kind === "v" ? "after" : kind === "s" ? (state.editor[0].view === "diff" ? "after" : "diff") : state.editor[1].view;
    return { split: kind, pane: kind === "" ? 0 : state.pane, editor: [{ view: left, cur: 0, want: 0 }, { view: right, cur: 0, want: 0 }] };
  });
  d.focusWin("editor");
  d.say(kind === "v" ? "before | after: Space wh and Space wl move between them, :only closes one" : kind === "s" ? "a second window below; Space v changes what it shows" : "one window");
}

export function setPaneView(d: Dashboard, view: string): void {
  if (!["diff", "before", "after", "raw"].includes(view)) { d.say("E: :view takes diff, before, after or raw", "err"); return; }
  d.set((state) => {
    const editor: typeof state.editor = [...state.editor];
    editor[state.pane] = { view: view as PaneView, cur: 0, want: 0 };
    return { editor };
  });
  d.say(`this window shows ${view === "raw" ? "the unified diff as text" : view}`);
}

/**
 * The places focus moves through, in order (decision 120): the tree, the
 * buffer, the box where there is one, the context; a hidden window is skipped.
 */
export function windows(d: Dashboard): Win[] {
  const state = d.state;
  if (state.view === "setup") return ["setup", "setupframe"];
  return [
    ...(state.queueShown ? ["queue" as const] : []), "editor",
    ...(d.elements.box !== null ? ["composer" as const] : []),
    ...(state.contextShown ? ["context" as const] : []),
  ];
}

/** `Tab`/`Shift+Tab`: focus to the next or previous place; a split's two panes are two stops. */
export function cycleWin(d: Dashboard, delta: 1 | -1): void {
  const state = d.state;
  const wins = windows(d);
  const at = Math.max(0, wins.indexOf(state.focus));
  if (state.focus === "editor" && state.split !== "" && ((delta > 0 && state.pane === 0) || (delta < 0 && state.pane === 1))) {
    d.set({ pane: state.pane === 0 ? 1 : 0 });
    d.focusWin("editor");
    return;
  }
  const next = wins[(at + delta + wins.length) % wins.length] ?? "editor";
  if (next === "editor" && state.split !== "") d.set({ pane: delta > 0 ? 0 : 1 });
  d.focusWin(next);
}

export function moveWin(d: Dashboard, direction: "h" | "j" | "k" | "l"): void {
  switch (direction) {
    case "j": d.focusWin("composer"); return;
    case "k": d.focusWin("editor"); return;
    default: cycleWin(d, direction === "l" ? 1 : -1);
  }
}

/** Show or hide the tree or the context; below 1280 px one side window shows at a time. */
export function toggleSide(d: Dashboard, which: "queue" | "context"): void {
  const narrow = !d.state.wide;
  d.set((state) => which === "queue"
    ? { queueShown: !state.queueShown, ...(narrow && !state.queueShown ? { contextShown: false } : {}) }
    // Below 1280 px the context takes the tree's place, and gives it back when it goes.
    : { contextShown: !state.contextShown, ...(narrow ? { queueShown: state.contextShown } : {}) });
  const state = d.state;
  if (which === "queue" && state.queueShown) d.focusWin("queue");
  else if (which === "context" && state.contextShown) d.focusWin("context");
  else d.focusWin("editor");
}

// ───────────────────────────── search ─────────────────────────────

/** A pattern as Vim's smartcase reads it: lower case matches either case, a capital matches exactly; `\<` and `\>` are word edges. */
export function searchRegex(pattern: string): RegExp {
  const flags = /[A-Z]/.test(pattern) ? "g" : "gi";
  const source = pattern.replace(/\\[<>]/g, "\\b");
  try {
    return new RegExp(source, flags);
  } catch {
    return new RegExp(source.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), flags);
  }
}

/** What a tree row says, as a search reads it. */
export function treeText(d: Dashboard, item: TreeItem): string {
  const live = d.state.live;
  if (live === null) return "";
  switch (item.t) {
    case "repo": return live.repositories.get(item.key)?.name ?? "";
    case "you": return "you";
    case "member": {
      const session = live.sessions.get(item.key);
      return session === undefined ? "" : `${session.name || session.id} ${activityBrief(session, d.state.now)}`;
    }
    case "review": {
      const row = d.rows().find((each) => each.key === item.key);
      return row === undefined ? "" : d.short(row);
    }
    case "folded": return `${item.count} stopped agents`;
  }
}

type Searched = { kind: "tree" | "pane"; count: number; cur: number; text: (index: number) => string; go: (index: number, column?: number) => void };

/** The window a search acts on: the tree (or the discussions) when it has focus, else the editor's pane. */
function searched(d: Dashboard): Searched {
  const state = d.state;
  const live = state.live;
  if (state.focus === "queue" && state.view === "threads" && live !== null) {
    const all = d.discussions();
    return {
      kind: "tree", count: all.length, cur: Math.max(0, all.findIndex((each) => each.key === state.sel.key)),
      text: (index) => { const each = all[index]; return each === undefined ? "" : `${each.title} ${discussionLine(live, each)}`; },
      go: (index) => { const each = all[index]; if (each !== undefined) d.openThread(each.key, "queue"); },
    };
  }
  if (state.focus === "queue" && state.view !== "history") {
    const items = d.tree();
    return {
      kind: "tree", count: items.length, cur: state.treeCur,
      text: (index) => { const item = items[index]; return item === undefined ? "" : treeText(d, item); },
      go: (index) => { d.set({ treeCur: index }); const item = items[index]; if (item !== undefined) openItem(d, item, true); },
    };
  }
  const rows = rowsOf(d);
  return {
    kind: "pane", count: rows.length, cur: state.editor[state.pane].cur,
    text: (index) => { const row = rows[index]; return row === undefined ? "" : rowText(row); },
    go: (index, column = 0) => { if (state.focus !== "editor") d.focusWin("editor"); placeCursor(d, state.pane, index, column, "center"); },
  };
}

/** Where on a row's line a search's first match stands, as the cursor lands on it. */
function matchColumn(d: Dashboard, index: number, pattern: string): number {
  const regex = searchRegex(pattern);
  regex.lastIndex = 0;
  return regex.exec(lineText(rowElement(d, d.state.pane, index)))?.index ?? 0;
}

function matches(list: Searched, pattern: string): number[] {
  const regex = searchRegex(pattern);
  const found: number[] = [];
  for (let index = 0; index < list.count; index += 1) {
    regex.lastIndex = 0;
    if (regex.test(list.text(index))) found.push(index);
  }
  return found;
}

function nextMatch(found: number[], from: number, backward: boolean): { index: number; wrapped: boolean } | null {
  if (found.length === 0) return null;
  if (backward) {
    const before = found.filter((index) => index < from);
    const last = before[before.length - 1];
    return last !== undefined ? { index: last, wrapped: false } : { index: found[found.length - 1] ?? 0, wrapped: true };
  }
  const after = found.find((index) => index > from);
  return after !== undefined ? { index: after, wrapped: false } : { index: found[0] ?? 0, wrapped: true };
}

export function startSearch(d: Dashboard): void {
  const state = d.state;
  d.set({ search: { ...state.search, typing: "", from: { win: state.focus, pane: state.pane, cur: state.editor[state.pane].cur, tree: state.treeCur } } });
}

/** Incremental search, as Vim's incsearch: each keystroke moves to the first match and lights every one. */
export function previewSearch(d: Dashboard, pattern: string): void {
  const from = d.state.search.from;
  if (from === null) return;
  d.set((state) => ({ search: { ...state.search, typing: pattern } }));
  if (pattern === "") return;
  const list = searched(d);
  const target = nextMatch(matches(list, pattern), from.win === "queue" ? from.tree : from.cur, false);
  if (target === null) return;
  if (list.kind === "pane") setCursor(d, d.state.pane, target.index, "center");
  else d.set({ treeCur: target.index });
}

export function confirmSearch(d: Dashboard, typed: string): void {
  const from = d.state.search.from;
  const pattern = typed !== "" ? typed : d.state.search.pattern;
  d.set((state) => ({ search: { ...state.search, typing: null, from: null } }));
  if (pattern === "") { d.say("E35: No previous regular expression", "err"); return; }
  if (from !== null) restore(d, from);
  d.set((state) => ({ search: { ...state.search, pattern, lit: true } }));
  searchStep(d, false, from === null ? undefined : from.win === "queue" ? from.tree : from.cur);
}

export function cancelSearch(d: Dashboard): void {
  const from = d.state.search.from;
  if (from !== null) restore(d, from);
  d.set((state) => ({ search: { ...state.search, typing: null, from: null } }));
}

function restore(d: Dashboard, from: { win: Win; pane: number; cur: number; tree: number }): void {
  if (from.win === "queue") { d.set({ treeCur: from.tree }); return; }
  const pane = from.pane === 1 ? 1 : 0;
  d.set({ pane });
  setCursor(d, pane, from.cur);
}

/** `;` goes to the next match of the last search and `,` to the previous one; both wrap, and say so in Vim's words. */
export function searchStep(d: Dashboard, backward: boolean, from?: number): void {
  const pattern = d.state.search.pattern;
  if (pattern === "") { d.say("E35: No previous regular expression: / searches first", "err"); return; }
  const list = searched(d);
  const found = matches(list, pattern);
  d.set((state) => ({ search: { ...state.search, lit: true } }));
  const target = nextMatch(found, from ?? list.cur, backward);
  if (target === null) { d.say(`E486: Pattern not found: ${pattern}`, "err"); return; }
  list.go(target.index, list.kind === "pane" ? matchColumn(d, target.index, pattern) : 0);
  const position = found.indexOf(target.index) + 1;
  d.say(target.wrapped ? `search hit ${backward ? "TOP, continuing at BOTTOM" : "BOTTOM, continuing at TOP"} · /${pattern} [${position}/${found.length}]` : `/${pattern} [${position}/${found.length}]`, target.wrapped ? "warn" : "");
}

/** `:N` goes to line N of the file in a review, and to row N of any other list. */
export function gotoLine(d: Dashboard, number: number): void {
  const list = searched(d);
  const entry = d.current();
  if (list.kind === "pane" && entry !== null && entry.detail !== null) {
    // Line n of the file the cursor is in (the first file where it is in none), shown where it stands: its gap opened, or the whole file.
    const at = rowHere(d);
    const fi = at !== undefined && "fi" in at && at.fi !== undefined ? at.fi : 0;
    const file = entry.detail.files[fi];
    const side: Side = at?.t === "line" ? at.side : file?.after === null ? "before" : "after";
    const lines = file === undefined ? 0 : lineCount(file[side]);
    if (number < 1 || number > lines) { d.say(`E: no line ${number} in ${file === undefined ? "this review" : basename(file.path)}`, "err"); return; }
    reveal(d, fi, side, number);
    placeCursor(d, d.state.pane, d.state.editor[d.state.pane].cur, 0);
    return;
  }
  list.go(Math.min(Math.max(0, number - 1), list.count - 1));
}

// ───────────────────────────── floats and small acts ─────────────────────────────

/** `K`: what is attached to the cursor's line, file, step, message, agent or tree row, next to it. */
export function hover(d: Dashboard): void {
  if (d.state.float?.kind === "hover") { d.set({ float: null }); return; }
  const state = d.state;
  const anchor = state.focus === "queue" && state.view !== "history"
    ? d.elements.queue?.querySelector(state.view === "threads" ? ".tr.sel" : `[data-ti="${state.treeCur}"]`)
    : d.elements.panes[state.pane]?.querySelector(".r.cur");
  const rect = anchor?.getBoundingClientRect();
  const top = rect === undefined ? 80 : rect.bottom + 2;
  const left = rect === undefined ? 40 : rect.left + 24;
  if (state.narrow) d.set({ touch: { ...state.touch, sheet: "hover" } });
  d.set({ float: { kind: "hover", top, left } });
}

/** `Esc` in Normal mode: close the top float, cancel visual mode or a sequence, or go back to the editor. */
export function escape(d: Dashboard): void {
  const state = d.state;
  if (state.float !== null) { d.set({ float: null, touch: { ...state.touch, sheet: "" } }); return; }
  if (state.visual !== null) { cancelVisual(d); return; }
  if (state.pending !== "") { d.sequencer.reset(); d.set({ pending: "", whichKey: false }); return; }
  if (state.touch.drawer !== "" || state.touch.sheet !== "") { d.set({ touch: { drawer: "", sheet: "" } }); return; }
  if (state.focus === "context" || state.focus === "queue") d.focusWin("editor");
}

/** `q`: close the top float, then the split. */
export function quit(d: Dashboard): void {
  const state = d.state;
  if (state.float !== null) { d.set({ float: null }); return; }
  if (state.split !== "") { split(d, ""); return; }
  d.say("E: this page has no window to quit; close the tab to leave it");
}

/** Enter: comment here, open a fold, open what the cursor is on, or write back to a message's sender. */
export function enter(d: Dashboard): void {
  const state = d.state;
  if (state.focus === "queue") {
    const item = d.tree()[state.treeCur];
    if (item?.t === "folded") { toggleStopped(d, item.key); return; }
    d.focusWin("editor");
    return;
  }
  if (state.focus === "context") {
    const element = d.elements.context?.querySelector<HTMLElement>(`.it[data-ci="${state.ctxCur}"]`);
    element?.click();
    return;
  }
  if (state.view === "setup") { d.focusWin("setupframe"); return; }
  if (d.centerKind() === "review") { commentAtCursor(d); return; }
  const row = rowHere(d);
  switch (row?.t) {
    case "post": replyToPost(d, row.post); return;
    case "mail": openMessage(d, row.m); return;
    case "earlier": void d.loadEarlier(row.repository); return;
    default: d.focusWin("composer");
  }
}

/** `r` or Enter on a post: the box under the discussion answers that post rather than the last. */
export function replyToPost(d: Dashboard, post: Post): void {
  d.set({ threadReply: post.id });
  d.focusWin("composer");
}

/** The box beside the agent a message is between, answering that message in its thread. */
export function replyToMessage(d: Dashboard, message: LiveMessage): void {
  const live = d.state.live;
  const session = live === null ? undefined : counterpart(live, message);
  if (session === undefined) { d.say("whoever wrote it is not on the roster this page holds", "err"); return; }
  if (!session.running) { d.say(`${session.name || session.id} has stopped; nothing would read a reply`, "err"); return; }
  d.set((state) => ({ replyTo: { ...state.replyTo, [session.key]: message.post || message.id } }));
  d.openOther("member", session.key, "box");
}

/** `r`: in a discussion, answer the post under the cursor; beside an agent or in the inbox, the message under it, in its thread. */
export function replyHere(d: Dashboard): void {
  const row = rowHere(d);
  if (row?.t === "post") { replyToPost(d, row.post); return; }
  if (row?.t === "mail") { replyToMessage(d, row.m); return; }
  d.say("r answers the message under the cursor; put it on one first");
}

/** `T`: the transcript of the post's author in a discussion, of the agent in view elsewhere. */
export function transcriptHere(d: Dashboard): void {
  const row = rowHere(d);
  const live = d.state.live;
  const author = row?.t === "post" && live !== null ? memberById(live, row.post.repository, row.post.sender) : memberHere(d);
  if (author === undefined) { d.say("choose an agent first (on the left, or Space fa)"); return; }
  void d.openTranscript(author);
}

/** A message opens whoever it is between with you, the box ready to answer it in its thread. */
export function openMessage(d: Dashboard, message: LiveMessage): void {
  const live = d.state.live;
  if (live === null) return;
  const session = counterpart(live, message);
  if (session === undefined) { d.openOther("repo", message.repository); return; }
  if (session.running) replyToMessage(d, message);
  else d.openOther("member", session.key, "normal");
}

/** `x`: delete a draft comment on a review; beside an agent or in the inbox, mark the message to you under the cursor read. */
export function xHere(d: Dashboard): void {
  if (d.centerKind() === "review") { deleteDraft(d); return; }
  const row = rowHere(d);
  if (row?.t === "mail" && row.unread) { void d.markRead([row.m]); return; }
  d.say("nothing to mark here: x marks a message read or deletes a draft comment");
}

/** `gs`: from a review, the agent that asked; from an agent, its parent session. */
export function goAsker(d: Dashboard): void {
  const state = d.state;
  const live = state.live;
  if (live === null) return;
  const open = d.current();
  if (open !== null) {
    const asker = d.asker(open.row);
    if (asker !== undefined) d.openOther("member", asker.key);
    else d.say("its session is not in the roster this page holds");
    return;
  }
  const session = state.sel.kind === "member" ? live.sessions.get(state.sel.key) : undefined;
  const parent = session === undefined || session.parent === "" ? undefined : [...live.sessions.values()].find((each) => each.repository === session.repository && each.id === session.parent);
  if (parent !== undefined) d.openOther("member", parent.key);
  else d.say("it has no parent session here");
}

/** `gr`: from an agent, a review it parked. */
export function goReview(d: Dashboard): void {
  const state = d.state;
  const live = state.live;
  const session = state.sel.kind === "member" ? live?.sessions.get(state.sel.key) : undefined;
  if (live === null || session === undefined) { d.say("gr goes from an agent to a review it parked"); return; }
  const parked = d.rows().filter((row) => d.asker(row)?.key === session.key);
  const target = parked.find((row) => row.state === "pending") ?? parked[0];
  if (target !== undefined) d.openReview(target.key, { mode: "normal" });
  else d.say("it parked no review this page holds");
}

/** The repository the page is in: the agent's, the row's, or the open review's queue's. */
export function repositoryHere(d: Dashboard, roots: ReviewRoot[] = d.roots()): string {
  const state = d.state;
  const live = state.live;
  if (live === null) return "";
  if (state.sel.kind === "member") return live.sessions.get(state.sel.key)?.repository ?? "";
  if (state.sel.kind === "you" || state.sel.kind === "repo") return state.sel.key;
  if (state.sel.kind === "thread") return d.discussion()?.repository ?? "";
  const open = d.current();
  if (open !== null) {
    const root = roots.find((each) => each.id === open.row.root_id);
    return [...live.repositories.values()].find((each) => root !== undefined && (each.repository === root.repository || each.repository === root.path))?.key ?? "";
  }
  return [...live.repositories.values()][0]?.key ?? "";
}

/** The member a command names, or the one in view: the selected agent, the inbox message's sender, or the open review's asker. */
export function memberHere(d: Dashboard): import("../generated/views").LiveSession | undefined {
  const state = d.state;
  const live = state.live;
  if (live === null) return undefined;
  if (state.sel.kind === "member") return live.sessions.get(state.sel.key);
  const discussion = d.discussion();
  if (state.view === "threads" && discussion !== undefined) {
    const row = rowHere(d);
    const id = row?.t === "post" && row.post.sender !== "user" ? row.post.sender : discussion.participants.find((each) => each !== "user");
    return id === undefined ? undefined : memberById(live, discussion.repository, id);
  }
  if (state.view === "inbox") {
    const target = d.boxTarget();
    return target.kind === "member" ? target.session : undefined;
  }
  const open = d.current();
  return open === null ? undefined : d.asker(open.row);
}

export function copyLink(d: Dashboard): void {
  const open = d.current();
  if (open === null) return;
  const url = new URL(window.location.href);
  url.search = "";
  url.hash = new URLSearchParams({ review: open.row.id, root: open.row.root_id }).toString();
  const link = url.href;
  if (navigator.clipboard === undefined) { d.say(`copy is unavailable; the link is ${link}`); return; }
  navigator.clipboard.writeText(link).then(() => d.say(`link copied: ${link}`), () => d.say(`copy is unavailable; the link is ${link}`));
}

/** The unread messages to the operator, for the statusline and the tree. */
export const unreadTotal = (d: Dashboard) => inboxOf(d.state.live ?? { messages: new Map() } as never).filter((each) => each.waiting).length;
