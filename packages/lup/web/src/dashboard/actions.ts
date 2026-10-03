// What each action of the catalog does, by its name, and how a key reaches it.
// The handler table answers every action the library's catalog declares — the
// page's test checks the two name the same actions — and the dispatcher reads
// the keymap in effect: lup's keys, with the person's and this tab's over them.
import type { LiveSession } from "../generated/views";
import type { Dashboard } from "./dashboard";
import { cancelVisual, changeJump, closeComment, commentAtCursor, commitVisual, copyLink, cursorColumn, cycleWin, edge, enter, escape, fold, foldTree, goAsker, goReview, gotoJudged, gotoLine, halfPage, hover, lineEdge, memberHere, moveAgent, moveCursor, moveException, moveFile, moveInbox, moveMarker, moveReview, moveWin, quit, replyHere, replyToMessage, repositoryHere, rowHere, searchStep, setCursor, setPaneView, split, startSearch, startVisual, toggleFull, toggleSide, toggleWhole, transcriptHere, undoDelete, wordMotion, xHere } from "./editor";
import { keyName, type Bound } from "./keys";
import { commandKey, openCommand } from "./commands";
import { openFinder } from "./finder";
import { VIEWS, type Float } from "./state";
import { inboxOf, ownPause, parentOf, standing } from "./supervision";

/** What one action does, given the count typed before its keys and whether one was. */
export type Handler = (d: Dashboard, count: number, counted: boolean) => void;

/** Act on the agent in view, or say how to choose one. */
function withAgent(d: Dashboard, act: (session: LiveSession) => void): void {
  const session = memberHere(d);
  if (session === undefined) { d.say("choose an agent first (on the left, or Space fa)"); return; }
  act(session);
}

/** The agent's box, answering the last message between it and the operator, in that message's thread. */
function replyToLast(d: Dashboard): void {
  withAgent(d, (session) => {
    const live = d.state.live;
    const last = live === null ? undefined : [...live.messages.values()]
      .filter((message) => message.repository === session.repository && !message.prompt && [message.sender, message.recipient].includes(session.id))
      .sort((left, right) => left.at - right.at).at(-1);
    if (last === undefined) { d.say(`nothing between you and ${session.name || session.id} to reply to; c writes a new message`); return; }
    replyToMessage(d, last);
  });
}

/** The file the cursor is on in a review, as its checkout names it; nothing elsewhere. */
function fileHere(d: Dashboard): string {
  const row = rowHere(d);
  const fi = row !== undefined && "fi" in row && row.fi !== undefined ? row.fi : -1;
  return d.centerKind() === "review" && fi >= 0 ? d.current()?.detail?.files[fi]?.path ?? "" : "";
}

function toggleFloat(d: Dashboard, float: Float): void {
  d.set((state) => ({ float: state.float?.kind === float.kind ? null : float }));
}

/** The editor's own pane moves a line or half a page while the box keeps focus. */
function scrollBehind(d: Dashboard, lines: number): void {
  const state = d.state;
  setCursor(d, state.pane, state.editor[state.pane].cur + lines);
}

function halfBehind(d: Dashboard, sign: 1 | -1): void {
  const element = d.elements.panes[d.state.pane];
  const line = element === null ? 18 : Number.parseFloat(getComputedStyle(element).lineHeight) || 18;
  scrollBehind(d, sign * Math.max(1, Math.floor((element?.clientHeight ?? 360) / line / 2)));
}

/** Ask an agent's parent session what it is doing, in words written for the operator. */
function askParent(d: Dashboard): void {
  const live = d.state.live;
  const session = memberHere(d);
  if (live === null || session === undefined) { d.say("choose an agent first (on the left, or Space fa)"); return; }
  const parent = parentOf(live, session);
  if (parent === undefined) { d.say(`${session.name || session.id} has no parent session on the roster`); return; }
  const state = standing(session, d.state.now);
  const calling = session.activity.calling !== "" && session.activity.at !== null
    ? ` and has been calling ${session.activity.calling} for ${Math.floor((Date.now() - Date.parse(session.activity.at)) / 60000)} minutes` : "";
  void d.sendTo(parent, `What is ${session.name || session.id} doing? It is ${state}${calling}.`).then((sent) => { if (sent) d.say(`asked ${parent.name || parent.id} about ${session.name || session.id}`); });
}

/** Write to the agent in view: its box beside it. */
function write(d: Dashboard): void {
  const session = memberHere(d);
  if (session === undefined) { d.say("choose an agent first (on the left, or Space fa)"); return; }
  if (d.state.sel.kind !== "member" || d.state.sel.key !== session.key) d.openOther("member", session.key, "box");
  else d.focusWin("composer");
}

function setTree(d: Dashboard, filter: "all" | "attention" | "reviews"): void {
  d.set({ tree: filter, treeCur: 0 });
  d.say(`the tree shows ${filter === "all" ? "every agent" : filter === "attention" ? "the agents that need you" : "only the reviews waiting, as the old queue did"}`);
}

function setting(d: Dashboard, name: "wrap" | "numbers" | "advance", say: [string, string]): void {
  d.set((state) => ({ settings: { ...state.settings, [name]: !state.settings[name] } }));
  d.say(d.state.settings[name] ? say[0] : say[1]);
}

function tab(d: Dashboard, count: number, counted: boolean, step: 1 | -1): void {
  const at = VIEWS.indexOf(d.state.view);
  const view = counted && step > 0 ? VIEWS[Math.min(count, VIEWS.length) - 1] : VIEWS[(at + step + VIEWS.length) % VIEWS.length];
  if (view !== undefined) d.setView(view);
}

/** Every action the catalog declares, by name, and what it does. */
export const HANDLERS: Record<string, Handler> = {
  "answer.approve": (d) => d.centerKind() === "review" ? d.answer("approve") : d.say("an answer goes on an open review; open one first (j, or Space Space)"),
  "answer.decline": (d) => d.centerKind() === "review" ? d.answer("decline") : d.say("an answer goes on an open review; open one first (j, or Space Space)"),
  "answer.send": (d) => d.centerKind() === "review" ? d.answer("remark") : d.sendBox(),
  "review.previous.typing": (d) => d.state.view === "inbox" ? moveInbox(d, -1) : moveReview(d, -1, "keep"),
  "review.next.typing": (d) => d.state.view === "inbox" ? moveInbox(d, 1) : moveReview(d, 1, "keep"),
  "box.next": (d) => moveReview(d, 1, "box"),
  "box.previous": (d) => moveReview(d, -1, "box"),
  "box.down": (d) => scrollBehind(d, 1),
  "box.up": (d) => scrollBehind(d, -1),
  "box.halfdown": (d) => halfBehind(d, 1),
  "box.halfup": (d) => halfBehind(d, -1),
  down: (d, count) => moveCursor(d, count),
  up: (d, count) => moveCursor(d, -count),
  "focus.next": (d) => cycleWin(d, 1),
  "focus.previous": (d) => cycleWin(d, -1),
  "agent.next": (d, count) => moveAgent(d, count),
  "agent.previous": (d, count) => moveAgent(d, -count),
  "go.asker": (d) => goAsker(d),
  "go.review": (d) => goReview(d),
  inbox: (d) => d.setView("inbox"),
  you: (d) => { const repository = repositoryHere(d); if (repository !== "") d.openOther("you", repository); },
  search: (d) => { startSearch(d); openCommand(d, "", "/"); },
  "search.next": (d, count) => { for (let at = 0; at < count; at += 1) searchStep(d, false); },
  "search.previous": (d, count) => { for (let at = 0; at < count; at += 1) searchStep(d, true); },
  "agent.write": (d) => write(d),
  "agent.reply": (d) => replyToLast(d),
  "agent.wake": (d) => withAgent(d, (session) => void d.wake(session)),
  "agent.nudge": (d) => withAgent(d, (session) => void d.interrupt(session, d.state.replyDrafts[session.key] ?? "")),
  "agent.parent": (d) => askParent(d),
  "agent.transcript": (d) => transcriptHere(d),
  "agent.rename": (d) => withAgent(d, (session) => openCommand(d, `rename ${session.name || session.id} `)),
  "agent.stop": (d) => withAgent(d, (session) => void d.stopRuntime(session)),
  "budget.turtle": (d) => void d.turtle(),
  "budget.priority": (d) => withAgent(d, (session) => openCommand(d, `priority ${session.name || session.id} `)),
  "budget.cap": (d) => withAgent(d, (session) => openCommand(d, `cap ${session.name || session.id} `)),
  "agent.pause": (d) => withAgent(d, (session) => void d.pause({ kind: "agent", repository: session.repository, member: session.id, tree: false }, false)),
  "agent.freeze": (d) => withAgent(d, (session) => void d.pause({ kind: "agent", repository: session.repository, member: session.id, tree: false }, true)),
  "agent.resume": (d) => withAgent(d, (session) => void d.resume(ownPause(session))),
  "message.reply": (d) => replyHere(d),
  "messages.earlier": (d) => { const repository = repositoryHere(d); void d.loadEarlier(repository); },
  "peer.describe": (d) => openCommand(d, "describe "),
  "peer.lock": (d) => openCommand(d, `lock ${fileHere(d)}`),
  "peer.release": (d) => openCommand(d, `release ${fileHere(d)}`),
  "peer.notice": (d) => openCommand(d, "notice "),
  "peer.broadcast": (d) => openCommand(d, "broadcast "),
  "peer.redirect": (d) => { const session = memberHere(d); openCommand(d, `redirect ${session === undefined ? "" : `${session.name || session.id} `}`); },
  "tree.all": (d) => setTree(d, "all"),
  "tree.attention": (d) => setTree(d, "attention"),
  "tree.reviews": (d) => setTree(d, "reviews"),
  "tree.stopped": (d) => { d.set((state) => ({ showStopped: !state.showStopped, stoppedOpen: new Set() })); d.say(d.state.showStopped ? "the tree shows every stopped agent" : "stopped agents fold into one row per repository"); },
  "tab.next": (d, count, counted) => tab(d, count, counted, 1),
  "tab.previous": (d, count) => tab(d, count, false, -1),
  "tree.toggle": (d) => toggleSide(d, "queue"),
  "context.toggle": (d) => toggleSide(d, "context"),
  "split.vertical": (d) => split(d, "v"),
  "split.below": (d) => split(d, "s"),
  "split.close": (d) => split(d, ""),
  "window.next": (d) => cycleWin(d, 1),
  "window.left": (d) => moveWin(d, "h"),
  "window.right": (d) => moveWin(d, "l"),
  "window.below": (d) => moveWin(d, "j"),
  "window.above": (d) => moveWin(d, "k"),
  "view.diff": (d) => setPaneView(d, "diff"),
  "view.before": (d) => setPaneView(d, "before"),
  "view.after": (d) => setPaneView(d, "after"),
  "view.raw": (d) => setPaneView(d, "raw"),
  close: (d) => quit(d),
  delete: (d) => xHere(d),
  "inbox.readall": (d) => { const live = d.state.live; if (live !== null) void d.markRead(inboxOf(live)); },
  box: (d) => d.focusWin("composer"),
  escape: (d) => escape(d),
  "comment.line": (d) => commentAtCursor(d),
  open: (d) => enter(d),
  visual: (d) => startVisual(d),
  undo: (d) => undoDelete(d),
  left: (d, count) => cursorColumn(d, -count),
  right: (d, count) => cursorColumn(d, count),
  "word.next": (d, count) => wordMotion(d, "next", count),
  "word.back": (d, count) => wordMotion(d, "back", count),
  "word.end": (d, count) => wordMotion(d, "end", count),
  "line.start": (d) => lineEdge(d, "start"),
  "line.first": (d) => lineEdge(d, "first"),
  "line.end": (d) => lineEdge(d, "end"),
  "page.halfdown": (d) => halfPage(d, 1),
  "page.halfup": (d) => halfPage(d, -1),
  top: (d, count, counted) => counted ? gotoLine(d, count) : edge(d, "top"),
  bottom: (d, count, counted) => counted ? gotoLine(d, count) : edge(d, "bottom"),
  "change.next": (d, count) => { for (let at = 0; at < count; at += 1) changeJump(d, 1); },
  "change.previous": (d, count) => { for (let at = 0; at < count; at += 1) changeJump(d, -1); },
  "file.next": (d) => moveFile(d, 1),
  "file.previous": (d) => moveFile(d, -1),
  "exception.next": (d) => moveException(d, 1),
  "exception.previous": (d) => moveException(d, -1),
  "marker.next": (d) => moveMarker(d, 1),
  "marker.previous": (d) => moveMarker(d, -1),
  "file.whole": (d) => toggleWhole(d),
  "review.full": (d) => toggleFull(d),
  judged: (d) => gotoJudged(d),
  "fold.toggle": (d) => d.state.focus === "queue" ? foldTree(d) : fold(d, "toggle"),
  "fold.open": (d) => fold(d, "open"),
  "fold.close": (d) => fold(d, "close"),
  "fold.all": (d) => fold(d, "all-open"),
  "fold.none": (d) => fold(d, "all-close"),
  hover: (d) => hover(d),
  "context.full": (d) => toggleFloat(d, { kind: "context" }),
  "find.review": (d) => openFinder(d, "reviews"),
  "find.agent": (d) => openFinder(d, "agents"),
  "find.inbox": (d) => openFinder(d, "inbox"),
  "find.thread": (d) => openFinder(d, "threads"),
  "find.history": (d) => openFinder(d, "history"),
  "find.file": (d) => openFinder(d, "files"),
  "find.line": (d) => openFinder(d, "lines"),
  "find.message": (d) => openFinder(d, "messages"),
  "find.command": (d) => openFinder(d, "commands"),
  "find.key": (d) => openFinder(d, "keys"),
  "find.marker": (d) => openFinder(d, "markers"),
  "ui.wrap": (d) => setting(d, "wrap", ["long lines wrap", "long lines scroll sideways"]),
  "ui.numbers": (d) => setting(d, "numbers", ["line numbers shown", "line numbers hidden"]),
  "ui.advance": (d) => setting(d, "advance", ["advance after a decision", "stay on the answered request"]),
  "ui.dismiss": (d) => d.set({ notes: [] }),
  link: (d) => copyLink(d),
  reconnect: (d) => d.reconnect(),
  messages: (d) => toggleFloat(d, { kind: "messages" }),
  help: (d) => toggleFloat(d, { kind: "help" }),
  cmdline: (d) => openCommand(d, ""),
};

/** Run one action, as a key or a click asks for it. */
export function run(d: Dashboard, bound: Bound, count = 1, counted = false): void {
  const handler = HANDLERS[bound.action.name];
  if (handler === undefined) { d.say(`E: ${bound.action.name} does nothing on this page`, "err"); return; }
  handler(d, count, counted);
}

/** Wait this long for the rest of a key that also starts a longer one, and this long before which-key shows what comes next. */
export type Waits = { sequence: number; whichKey: number };
export const WAITS: Waits = { sequence: 700, whichKey: 250 };

function resetKeys(d: Dashboard): void {
  d.sequencer.reset();
  clearTimeout(d.keyTimer);
  clearTimeout(d.whichTimer);
  d.set({ pending: "", whichKey: false });
}

/** A key in Normal mode: a count, then a sequence the keymap answers in the places in view. */
function normalKey(d: Dashboard, key: string, waits: Waits): boolean {
  const bindings = d.keymap.acting("normal", d.where());
  const step = d.sequencer.press(key, bindings);
  clearTimeout(d.keyTimer);
  switch (step.kind) {
    case "count": d.set({ pending: d.sequencer.shown() }); return true;
    case "unknown":
      resetKeys(d);
      return step.typed.length > 1 || key === "<leader>";
    case "run":
      resetKeys(d);
      run(d, step.bound, step.count, step.counted);
      return true;
    case "pending": {
      if (d.sequencer.typed.length === 0) { resetKeys(d); return true; }
      const exact = step.exact;
      if (exact !== null) d.keyTimer = setTimeout(() => { const done = d.sequencer.complete(exact); resetKeys(d); if (done.kind === "run") run(d, done.bound, done.count, done.counted); }, waits.sequence);
      d.set({ pending: d.sequencer.shown() });
      clearTimeout(d.whichTimer);
      if (d.state.whichKey) d.set({ whichKey: true });
      else d.whichTimer = setTimeout(() => d.set({ whichKey: true }), waits.whichKey);
      return true;
    }
  }
}

/** A chord acts in any mode, the box and Normal alike; an answer is never repeated by a held key. */
function chord(d: Dashboard, key: string, event: KeyboardEvent): boolean {
  const bound = d.keymap.exact("any", key, d.where());
  if (bound === undefined) return false;
  event.preventDefault();
  if (bound.action.name.startsWith("answer.")) {
    const code = event.code || key;
    if (event.repeat || d.heldKeys.has(code)) return true;
    d.heldKeys.add(code);
  }
  resetKeys(d);
  run(d, bound);
  return true;
}

/**
 * The note box the page landed in navigates until the operator types: while
 * it is armed and empty, its box keys move on or scroll the diff; every other
 * key types, and the first character typed disarms it.
 */
function boxKey(d: Dashboard, key: string, event: KeyboardEvent): boolean {
  const box = d.elements.box;
  if (!d.state.armed || event.target !== box || box === null || box.value !== "" || d.centerKind() !== "review") return false;
  const bound = d.keymap.exact("box", key, d.where());
  if (bound === undefined) return false;
  event.preventDefault();
  run(d, bound);
  return true;
}

function visualKey(d: Dashboard, key: string, typedG: { value: boolean }): boolean {
  if (typedG.value) {
    typedG.value = false;
    if (key === "c") commitVisual(d);
    return true;
  }
  const state = d.state;
  switch (key) {
    case "j": case "<Down>": setCursor(d, state.pane, state.editor[state.pane].cur + 1); return true;
    case "k": case "<Up>": setCursor(d, state.pane, state.editor[state.pane].cur - 1); return true;
    case "g": typedG.value = true; return true;
    case "c": case "i": case "a": case "<Enter>": commitVisual(d); return true;
    case "<Esc>": case "<C-[>": case "V": case "v": cancelVisual(d); return true;
    default: return false;
  }
}

const SCROLLED: Record<string, (body: HTMLElement, line: number) => number> = {
  "<Down>": (_body, line) => line, j: (_body, line) => line, "<Up>": (_body, line) => -line, k: (_body, line) => -line,
  "<PageDown>": (body) => body.clientHeight / 2, "<C-d>": (body) => body.clientHeight / 2,
  "<PageUp>": (body) => -body.clientHeight / 2, "<C-u>": (body) => -body.clientHeight / 2,
  G: (body) => body.scrollHeight,
};

/** The key that opened a float closes it again, as `q`, `Esc` and `Ctrl+[` do. */
const TOGGLES: Record<string, Float["kind"]> = { "?": "help", I: "context" };

/** Keys while a float is open: they close it or scroll it, and reach nothing beneath. */
function floatKey(d: Dashboard, key: string, event: KeyboardEvent, typedG: { value: boolean }): void {
  const float = d.state.float;
  if (float === null) return;
  if (key === "<Esc>" || key === "<C-[>" || key === "q" || TOGGLES[key] === float.kind) {
    event.preventDefault();
    d.set({ float: null });
    resetKeys(d);
    return;
  }
  const body = d.elements.float;
  if (body === null) return;
  const line = Number.parseFloat(getComputedStyle(body).lineHeight) || 18;
  const scrolled = SCROLLED[key];
  if (scrolled !== undefined) {
    event.preventDefault();
    typedG.value = false;
    body.scrollTop += scrolled(body, line);
    return;
  }
  if (key === "g") {
    event.preventDefault();
    if (typedG.value) body.scrollTop = 0;
    typedG.value = !typedG.value;
  }
}

/** Each key the page takes, in the order a person expects them to win. */
export function keyHandler(d: Dashboard, waits: Waits = WAITS): { down: (event: KeyboardEvent) => void; up: (event: KeyboardEvent) => void; blur: () => void } {
  const typedG = { value: false };
  const down = (event: KeyboardEvent) => {
    if (event.isComposing || d.state.denied || event.defaultPrevented) return;
    const key = keyName(event);
    if (key === null) return;
    const state = d.state;
    // The finder and the command line take their own keys.
    if (state.float?.kind === "finder") { finderKey(d, key, event); return; }
    if (state.cmdline !== null) { commandKey(d, key, event); return; }
    if (chord(d, key, event)) return;
    const target = event.target;
    const typing = target instanceof Element && target.closest("textarea, input, select, [contenteditable]:not([contenteditable='false'])") !== null;
    if (typing) {
      if (boxKey(d, key, event)) return;
      if (key === "<Esc>" || key === "<C-[>") {
        event.preventDefault();
        if (target instanceof HTMLTextAreaElement && target.dataset.comment !== undefined) closeComment(d);
        else d.focusWin("editor");
      }
      return;
    }
    if (state.float !== null && state.float.kind !== "hover") { floatKey(d, key, event, typedG); return; }
    if (state.float?.kind === "hover") {
      d.set({ float: null, touch: { ...state.touch, sheet: "" } });
      if (key === "<Esc>" || key === "<C-[>" || key === "K" || key === "q") { event.preventDefault(); return; }
    }
    if (state.visual !== null && visualKey(d, key, typedG)) { event.preventDefault(); return; }
    if (key === "<C-[>") { event.preventDefault(); escape(d); return; }
    if (normalKey(d, key, waits)) event.preventDefault();
  };
  const up = (event: KeyboardEvent) => {
    d.heldKeys.delete(event.code || keyName(event) || event.key);
  };
  const blur = () => d.heldKeys.clear();
  return { down, up, blur };
}

function finderKey(d: Dashboard, key: string, event: KeyboardEvent): void {
  const float = d.state.float;
  if (float?.kind !== "finder") return;
  const move = (to: number) => { event.preventDefault(); d.set({ float: { ...float, cur: Math.max(0, to) } }); };
  switch (key) {
    case "<Esc>": case "<C-[>": event.preventDefault(); d.set({ float: null }); d.focusWin(d.state.focus); return;
    case "<Enter>": event.preventDefault(); d.elements.float?.querySelector<HTMLElement>(".fi.cur")?.click(); return;
    case "<Down>": case "<C-j>": case "<C-n>": move(float.cur + 1); return;
    case "<Up>": case "<C-k>": case "<C-p>": move(float.cur - 1); return;
    case "<PageDown>": move(float.cur + 10); return;
    case "<PageUp>": move(float.cur - 10); return;
    default:
  }
}

/** Whether the row under the cursor is a line a comment can go on, for the views that say so. */
export const onLine = (d: Dashboard) => rowHere(d)?.t === "line";
