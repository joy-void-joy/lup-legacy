// The layout below 861 px: one column, not a squeezed desktop. A top bar says
// what is in view and opens the tree and the context as drawers; an action bar
// above the tabs holds the answers, every button at least 44 px; the tabs sit
// under the thumb. An answer takes two taps in different places: the first
// opens a sheet saying what is sent, its confirming button at the top and
// Cancel at the bottom, where the first tap was. Every control runs what a key
// runs on the desktop, and nothing is hover-only.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { Answer, Dashboard } from "./dashboard";
import { commitVisual, cancelVisual, gotoJudged, hover, moveAgent, moveException, moveFile, moveInbox, moveMarker, moveReview, rowHere, rowsOf, setCursor, startVisual, toggleWhole, transcriptHere, visualSpan } from "./editor";
import { openCommand, runCommand } from "./commands";
import { openFinder } from "./finder";
import { askedBy, exceptionStops, headOf, headShort, judgedOf, markerStops, plural, stateLabel, stateSign, type Row } from "./review";
import { HANDLERS } from "./actions";
import type { Feature } from "./served";
import { VIEW_NAMES, VIEWS, type NavKind, type PageState } from "./state";
import { GLYPH, inboxOf, kindWords, standing, unreadCount } from "./supervision";
import { memberName } from "./threads";

export function TopBar({ d, state }: { d: Dashboard; state: PageState }) {
  const kind = d.centerKind(state);
  const live = state.live;
  let title = "";
  let sub = "";
  switch (kind) {
    case "review": {
      const entry = d.current(state);
      if (entry !== null) { title = `${stateSign(entry.row)} ${headShort(headOf(entry))}`; sub = `asked by ${askedBy(entry.row)} · ${entry.row.id.slice(0, 8)} · ${stateLabel(entry.row)}`; }
      break;
    }
    case "member": {
      const session = live?.sessions.get(state.sel.key);
      if (session !== undefined) { title = `${GLYPH[standing(session, state.now)]} ${session.name || session.id}`; sub = `${kindWords(session)} · ${standing(session, state.now)}`; }
      break;
    }
    case "you": title = "◆ you"; sub = live?.repositories.get(state.sel.key)?.name ?? ""; break;
    case "repo": title = live?.repositories.get(state.sel.key)?.name ?? ""; sub = "its members and messages"; break;
    case "inbox": title = "Inbox"; sub = live === null ? "" : `${unreadCount(live)} unread of ${inboxOf(live).length}`; break;
    case "thread": {
      const discussion = d.discussion(state);
      if (discussion !== undefined && live !== null) { title = `» ${discussion.title}`; sub = `${plural(discussion.posts.length, "post")} · ${discussion.participants.map((id) => memberName(live, discussion.repository, id)).join(", ")}`; }
      break;
    }
    case "setup": title = "Setup"; break;
    default: title = state.live === null ? "Loading…" : state.view === "threads" ? "Discussions" : "Nothing waits on you";
  }
  const header = useRef<HTMLElement>(null);
  // Notices sit under the top bar however tall its title makes it, so they never cover its buttons.
  useLayoutEffect(() => {
    const element = header.current;
    if (element === null) return;
    const place = () => document.documentElement.style.setProperty("--touchtop", `${element.offsetHeight}px`);
    place();
    const observer = new ResizeObserver(place);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const drawer = (which: "tree" | "context") => d.set((now) => ({ touch: { drawer: now.touch.drawer === which ? "" : which, sheet: "" } }));
  return <header id="touchtop" ref={header} aria-label="Current item">
    <button type="button" className="tb" aria-label="Agents and reviews" onClick={() => drawer("tree")}>☰</button>
    <div id="t-title"><span className="main">{title}</span>{sub !== "" && <span className="sub">{sub}</span>}</div>
    <button type="button" className="tb" aria-label="Find" onClick={() => openFinder(d, kind === "review" ? "reviews" : "agents")}>⌕</button>
    <button type="button" className="tb" aria-label="Context" onClick={() => drawer("context")}>ⓘ</button>
    <button type="button" className="tb" aria-label="More" onClick={() => d.set({ touch: { drawer: "", sheet: "more" } })}>⋯</button>
  </header>;
}

/** On a touch screen, ‹ and › and a sideways swipe move through what is in view: reviews, the inbox's messages, or the agents. */
export function touchStep(d: Dashboard, step: 1 | -1): void {
  const state = d.state;
  if (state.view === "threads") d.moveThread(step);
  else if (state.view === "inbox") moveInbox(d, step);
  else if (d.centerKind(state) === "review" || state.view === "history") moveReview(d, step, "normal");
  else moveAgent(d, step);
}

function ask(d: Dashboard, action: Answer): void {
  const entry = d.current();
  if (entry === null) return;
  const draft = d.draft(entry.row.key);
  if (action === "remark" && draft.note.trim() === "" && draft.comments.every((comment) => comment.note.trim() === "")) { d.say("nothing to send: write a note or a line comment first"); return; }
  d.set({ touch: { drawer: "", sheet: action } });
}

export function ActionBar({ d, state }: { d: Dashboard; state: PageState }) {
  const nav = (step: 1 | -1, label: string) => <button type="button" className="nav" aria-label={label} onClick={() => touchStep(d, step)}>{step < 0 ? "‹" : "›"}</button>;
  if (state.visual !== null) {
    const span = visualSpan(d);
    const lines = span === null ? "these lines" : span.start === span.end ? `line ${span.start}` : `lines ${span.start}–${span.end}`;
    return <div id="actionbar" aria-label="Actions"><button type="button" onClick={() => cancelVisual(d)}>Cancel</button><button type="button" className="approve" onClick={() => commitVisual(d)}>Comment on {lines}</button></div>;
  }
  switch (d.centerKind(state)) {
    case "review": {
      const entry = d.current(state);
      if (entry === null) return null;
      const row = entry.row;
      return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous review")}
        {row.state === "pending" && row.answerable ? <>
          <button type="button" className="decline" onClick={() => ask(d, "decline")}>Decline</button>
          <button type="button" onClick={() => ask(d, "remark")}>Send</button>
          <button type="button" className="approve" disabled={row.stale.length > 0} onClick={() => ask(d, "approve")}>Approve</button>
        </> : <span className="say">{row.state === "pending" ? row.unanswerable || "nobody here may answer it" : stateLabel(row)}</span>}
        {nav(1, "Next review")}</div>;
    }
    case "member": return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous agent")}<button type="button" onClick={() => d.focusWin("composer")}>Write</button>
      <button type="button" onClick={() => transcriptHere(d)}>Transcript</button>
      <button type="button" onClick={() => d.set({ touch: { drawer: "", sheet: "agent" } })}>Act</button>{nav(1, "Next agent")}</div>;
    case "inbox": return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous message")}<button type="button" onClick={() => d.focusWin("composer")}>Reply</button>
      <button type="button" onClick={() => { const row = rowHere(d); if (row?.t === "mail") void d.markRead([row.m]); else d.say("put the cursor on a message first"); }}>Mark read</button>{nav(1, "Next message")}</div>;
    case "thread": return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous discussion")}<button type="button" className="approve" onClick={() => d.focusWin("composer")}>Post to all</button>
      <button type="button" onClick={() => d.set({ touch: { drawer: "context", sheet: "" } })}>Who is in it</button>{nav(1, "Next discussion")}</div>;
    case "repo": return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous agent")}<button type="button" onClick={() => d.focusWin("composer")}>Broadcast</button>{nav(1, "Next agent")}</div>;
    case "you": return <div id="actionbar" aria-label="Actions">{nav(-1, "Previous agent")}<button type="button" onClick={() => openFinder(d, "commands")}>Your verbs</button>{nav(1, "Next agent")}</div>;
    default: return null;
  }
}

export function TabBar({ d, state }: { d: Dashboard; state: PageState }) {
  const live = state.live;
  const badges: Record<string, number> = {
    supervise: d.pending(state).length, history: 0, inbox: live === null ? 0 : unreadCount(live),
    threads: d.discussions(state).reduce((total, each) => total + each.unread, 0), setup: 0,
  };
  return <nav id="tabbar" aria-label="Views">
    {VIEWS.map((view) => <button key={view} type="button" role="tab" aria-selected={state.view === view} onClick={() => d.setView(view)}>{VIEW_NAMES[view]}{(badges[view] ?? 0) > 0 && <span className="badge">{badges[view]}</span>}</button>)}
  </nav>;
}

const VERBS: Record<Answer, string> = { approve: "Approve", decline: "Decline", remark: "Send without deciding" };

/** What the phone's agent sheet offers, each the catalog action a key runs, and what it needs from the server. */
const AGENT_ACTS: { action: string; label: string; needs?: Feature }[] = [
  { action: "agent.wake", label: "Wake it", needs: "bare-wake" },
  { action: "agent.nudge", label: "Interrupt its turn (the box's words, or the standard ones)", needs: "interrupt" },
  { action: "agent.reply", label: "Reply to its last message, in its thread", needs: "reply-thread" },
  { action: "peer.redirect", label: "Redirect its next call…", needs: "redirect" },
  { action: "agent.rename", label: "Rename it…", needs: "rename" },
  { action: "agent.stop", label: "Stop its runtime (tap twice)", needs: "stop" },
  { action: "agent.parent", label: "Ask its parent session about it" },
];

export function Sheet({ d, state }: { d: Dashboard; state: PageState }) {
  const sheet = state.touch.sheet;
  const close = () => d.set({ touch: { drawer: "", sheet: "" } });
  if (sheet === "" || sheet === "hover") return null;
  if (sheet === "nav") return <NavKinds d={d} state={state} />;
  if (sheet === "agent") {
    const session = state.live?.sessions.get(state.sel.key);
    const act = (action: string) => () => { close(); HANDLERS[action]?.(d, 1, false); };
    const lacking = (needs: Feature) => d.lacks(needs) !== "";
    return <div id="sheet" role="dialog" aria-modal="true" aria-label="Act on the agent">
      <h3>{session === undefined ? "Act on the agent" : `Act on ${session.name || session.id}`}</h3>
      <div className="list">
        {AGENT_ACTS.map(({ action, label, needs }) => <button key={action} type="button" disabled={needs !== undefined && lacking(needs)} title={needs === undefined ? "" : d.lacks(needs)} onClick={act(action)}>{label}</button>)}
      </div>
      <button type="button" className="big cancel" onClick={close}>Close</button>
    </div>;
  }
  if (sheet === "more") {
    const live = state.live;
    const run = (command: string) => () => { close(); runCommand(d, command); };
    return <div id="sheet" role="dialog" aria-modal="true" aria-label="More">
      <h3>More</h3>
      <p className="muted">{d.queueCurrent(state) ? "● live" : `◌ ${state.connection}`} · {d.pending(state).length} wait on you · {live === null ? 0 : unreadCount(live)} unread to you{live !== null && live.code.source !== "" ? ` · code ${live.code.source.slice(0, 8)}` : ""}</p>
      <div className="list">
        <button type="button" onClick={() => { close(); openFinder(d, "commands"); }}>Find a command (every verb)</button>
        <button type="button" onClick={() => { close(); openFinder(d, "agents"); }}>Find an agent</button>
        <button type="button" onClick={() => { close(); openFinder(d, "lines"); }}>Find a line in this buffer</button>
        <button type="button" onClick={run("help")}>Every key (help)</button>
        <button type="button" onClick={run("map")}>Your keys (:map)</button>
        <button type="button" onClick={run("context")}>Full context</button>
        <button type="button" onClick={run("messages")}>What this page said (:messages)</button>
        <button type="button" onClick={run(state.showStopped ? "set nostopped" : "set stopped")}>{state.showStopped ? "Fold the stopped agents" : "Show the stopped agents"}</button>
        <button type="button" onClick={() => { close(); openCommand(d, ""); }}>Command line (:)</button>
        <button type="button" onClick={run("reconnect")}>Reconnect</button>
      </div>
      <button type="button" className="big cancel" onClick={close}>Close</button>
    </div>;
  }
  const entry = d.current(state);
  if (entry === null) return null;
  const draft = d.draft(entry.row.key, state);
  const comments = draft.comments.filter((comment) => comment.note.trim() !== "").length;
  const verb = VERBS[sheet];
  const note = draft.note.trim() !== "" ? `Your note goes with it: “${draft.note.trim()}”` : sheet === "remark" ? "" : "No note: the agent hears only the answer.";
  return <div id="sheet" role="dialog" aria-modal="true" aria-label={`${verb}?`}>
    <h3>{verb}?</h3>
    <p><b>{headShort(headOf(entry))}</b> <span className="muted">· asked by {askedBy(entry.row)} · {entry.row.id.slice(0, 8)}</span></p>
    <button type="button" className={`big ${sheet === "remark" ? "" : sheet}`} onClick={() => { close(); d.answer(sheet); }}>{verb}</button>
    {note !== "" && <p>{note}</p>}
    {comments > 0 && <p>{plural(comments, "line comment")} {comments === 1 ? "goes" : "go"} with it.</p>}
    <button type="button" className="big cancel" onClick={close}>Cancel</button>
  </div>;
}

/** The hover, on a touch screen, as a sheet from the bottom with a ✕; a tap elsewhere closes it. */
export const hoverSheet = (state: PageState) => state.narrow && state.float?.kind === "hover";

/**
 * Taps, long-presses and sideways swipes over the buffer. A tap moves the
 * cursor and shows what is attached to the line; a long-press (0.55 s) starts a
 * range there, which a tap on another line stretches; a sideways swipe of 70
 * px, under 50 px down and within 0.8 s, moves to the next or previous item,
 * so a scroll never moves on. Each touch starts a new gesture, so a click a
 * swipe or a long-press suppressed never outlives it.
 */
export function useGestures(d: Dashboard, narrow: boolean): void {
  const gesture = useRef<{ x: number; y: number; at: number; timer: ReturnType<typeof setTimeout> | undefined; suppress: boolean }>({ x: 0, y: 0, at: 0, timer: undefined, suppress: false });
  useEffect(() => {
    if (!narrow) return;
    const now = gesture.current;
    const down = (event: PointerEvent) => {
      now.suppress = false;
      clearTimeout(now.timer);
      if (!(event.target instanceof Element)) return;
      const pane = event.target.closest<HTMLElement>(".pane");
      if (pane === null) return;
      now.x = event.clientX;
      now.y = event.clientY;
      now.at = performance.now();
      const row = event.target.closest<HTMLElement>(".r");
      if (row === null || d.centerKind() !== "review" || d.state.visual !== null) return;
      now.timer = setTimeout(() => {
        now.suppress = true;
        const index = pane.dataset.pane === "1" ? 1 : 0;
        d.set({ pane: index, focus: "editor" });
        setCursor(d, index, Number(row.dataset.i));
        const at = rowsOf(d, index)[Number(row.dataset.i)];
        if (at?.t !== "line" || at.num === null) { d.say("long-press a line of a file to comment on it"); return; }
        startVisual(d);
      }, 550);
    };
    const move = (event: PointerEvent) => {
      if (Math.hypot(event.clientX - now.x, event.clientY - now.y) > 12) clearTimeout(now.timer);
    };
    const up = (event: PointerEvent) => {
      clearTimeout(now.timer);
      if (now.at === 0) return;
      const dx = event.clientX - now.x;
      const dy = event.clientY - now.y;
      const quick = performance.now() - now.at < 800;
      now.at = 0;
      if (Math.abs(dx) > 70 && Math.abs(dy) < 50 && quick) {
        now.suppress = true;
        touchStep(d, dx < 0 ? 1 : -1);
      }
    };
    const click = (event: MouseEvent) => {
      if (now.suppress) { now.suppress = false; event.preventDefault(); event.stopPropagation(); return; }
      if (!(event.target instanceof Element)) return;
      const row = event.target.closest<HTMLElement>(".pane .r");
      if (row === null || event.target.closest("textarea, .ln") !== null) return;
      // A tap stretches a range being picked, or shows what is attached to the line: nothing is hover-only.
      if (d.state.visual !== null) return;
      const at = rowsOf(d, d.state.pane)[Number(row.dataset.i)];
      if (at !== undefined && at.t !== "file" && (at.jg === true || (at.t === "line" && (at.marker !== null || at.exception !== null)) || at.t === "mail")) {
        setTimeout(() => hover(d), 0);
      }
    };
    const cancel = () => { clearTimeout(now.timer); now.at = 0; };
    document.addEventListener("pointerdown", down);
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", cancel);
    document.addEventListener("click", click, true);
    return () => {
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", cancel);
      document.removeEventListener("click", click, true);
    };
  }, [d, narrow]);
}

// ───────────── the strip that steps through a review, on a phone (decision 123) ─────────────

type NavCount = { label: string; at: number; total: number };

/** Where each change starts: the first changed line after unchanged ones, or a step of a command that asks. */
function changeStarts(rows: Row[]): number[] {
  const changed = (row: Row | undefined) => row?.t === "line" && (row.kind === "add" || row.kind === "remove");
  return rows.filter((row) => (changed(row) && !changed(rows[row.i - 1])) || (row.t === "seg" && !row.allowed)).map((row) => row.i);
}

/** Where the cursor stands among each kind of stop in the open review, and how many there are. */
export function navCounts(d: Dashboard, state: PageState): Record<NavKind, NavCount> | null {
  const entry = d.current(state);
  if (entry === null || entry.detail === null) return null;
  const rows = d.buffer(state.pane, state).rows;
  const cur = state.editor[state.pane].cur;
  const here = rows[cur];
  const full = d.ui(entry.row.key, state).full;
  const exceptions = exceptionStops(entry.detail, full).length;
  const markers = markerStops(entry.detail, full).length;
  const judged = judgedOf(entry.detail, entry.row).length;
  const starts = changeStarts(rows);
  const within = (at: number, total: number) => at >= 0 && at < total ? at + 1 : 0;
  return {
    exception: { label: "exception", at: within(state.exceptionAt, exceptions), total: exceptions },
    change: { label: "change", at: starts.filter((index) => index <= cur).length, total: starts.length },
    file: { label: "file", at: here !== undefined && "fi" in here && here.fi !== undefined ? here.fi + 1 : 0, total: entry.detail.files.length },
    marker: { label: "marker", at: within(state.markerAt, markers), total: markers },
    asked: { label: "asked about", at: within(state.judgedAt, judged), total: judged },
  };
}

/** The kind the strip steps by: the one chosen, else exceptions where there are any, else changes. */
export function navKind(state: PageState, counts: Record<NavKind, NavCount>): NavKind {
  if (state.navKind !== "") return state.navKind;
  return (["exception", "change", "file", "marker"] as const).find((kind) => counts[kind].total > 0) ?? "change";
}

function navStep(d: Dashboard, step: 1 | -1): void {
  const state = d.state;
  const counts = navCounts(d, state);
  if (counts === null) return;
  switch (navKind(state, counts)) {
    case "exception": moveException(d, step); return;
    case "file": moveFile(d, step); return;
    case "marker": moveMarker(d, step); return;
    case "asked": {
      const total = counts.asked.total;
      if (total > 0) gotoJudged(d, ((state.judgedAt < 0 ? (step > 0 ? -1 : 0) : state.judgedAt) + step + total) % total);
      return;
    }
    case "change": {
      const rows = rowsOf(d);
      const cur = state.editor[state.pane].cur;
      const starts = changeStarts(rows);
      const target = step > 0 ? starts.find((index) => index > cur) : [...starts].reverse().find((index) => index < cur);
      if (target === undefined) d.say(step > 0 ? "no change below" : "no change above");
      else setCursor(d, state.pane, target, "center");
    }
  }
}

/** `full file` shows the whole file at the cursor, and then reads `back to diff`. */
function wholeHere(d: Dashboard): void {
  const rows = rowsOf(d);
  const here = rows[d.state.editor[d.state.pane].cur];
  if (here === undefined || !("fi" in here) || here.fi === undefined) {
    const first = rows.findIndex((row) => row.t === "file");
    if (first >= 0) setCursor(d, d.state.pane, first);
  }
  toggleWhole(d);
}

/** One row of 52 px under the editor's bar: `‹ change 2/36 ▾ › full file`. */
export function NavStrip({ d, state }: { d: Dashboard; state: PageState }) {
  const counts = navCounts(d, state);
  const entry = d.current(state);
  if (counts === null || entry === null || entry.detail === null || entry.detail.files.length === 0) return null;
  const kind = navKind(state, counts);
  const count = counts[kind];
  const here = d.buffer(state.pane, state).rows[state.editor[state.pane].cur];
  const fi = here !== undefined && "fi" in here && here.fi !== undefined ? here.fi : 0;
  const whole = d.ui(entry.row.key, state).whole.has(fi);
  return <div id="navstrip" role="toolbar" aria-label="Step through the review">
    <button type="button" className="nav" aria-label={`Previous ${count.label}`} onClick={() => navStep(d, -1)}>‹</button>
    <button type="button" className="kind" aria-label={`Step by: ${count.label}`} onClick={() => d.set({ touch: { drawer: "", sheet: "nav" } })}>{count.label} {count.at === 0 ? "–" : count.at}/{count.total} ▾</button>
    <button type="button" className="nav" aria-label={`Next ${count.label}`} onClick={() => navStep(d, 1)}>›</button>
    <button type="button" className="whole" onClick={() => wholeHere(d)}>{whole ? "back to diff" : "full file"}</button>
  </div>;
}

/** Every kind to step by, each with where the cursor stands: a sheet, so the strip itself stays one row. */
function NavKinds({ d, state }: { d: Dashboard; state: PageState }) {
  const counts = navCounts(d, state);
  const close = () => d.set({ touch: { drawer: "", sheet: "" } });
  if (counts === null) return null;
  return <div id="sheet" role="dialog" aria-modal="true" aria-label="Step by">
    <h3>Step by</h3>
    <div className="list">
      {(Object.keys(counts) as NavKind[]).map((kind) => <button type="button" key={kind} onClick={() => { d.set({ navKind: kind, touch: { drawer: "", sheet: "" } }); navStep(d, 1); }}>
        {counts[kind].label} · {counts[kind].at === 0 ? "–" : counts[kind].at} of {counts[kind].total}</button>)}
    </div>
    <button type="button" className="big cancel" onClick={close}>Close</button>
  </div>;
}

// ───────────── long prose folds to four lines on a phone (decision 124) ─────────────

/**
 * Prose that folds to four lines with `more` on a phone — an agent's note on a
 * file, what an agent said, a message, a post, the context's prose — and
 * reads `less` once opened. On a wider screen it is the prose, whole.
 */
export function Clamp({ d, narrow, open, id, children, as = "span" }: { d: Dashboard; narrow: boolean; open: boolean; id: string; children: ReactNode; as?: "span" | "p" }) {
  const element = useRef<HTMLElement>(null);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const held = element.current;
    if (held === null || !narrow) return;
    setLong(open || held.scrollHeight > held.clientHeight + 2);
  }, [narrow, open, children]);
  const Tag = as;
  if (!narrow) return <Tag>{children}</Tag>;
  const toggle = () => d.set((now) => {
    const unclamped = new Set(now.unclamped);
    if (unclamped.has(id)) unclamped.delete(id);
    else unclamped.add(id);
    return { unclamped };
  });
  return <>
    <Tag ref={element as never} className={`clamp${open ? " open" : ""}`}>{children}</Tag>
    {long && <button type="button" className="more" onClick={(event) => { event.stopPropagation(); toggle(); }}>{open ? "less" : "more"}</button>}
  </>;
}
