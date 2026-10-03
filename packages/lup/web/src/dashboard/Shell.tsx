// The page's chrome, one text row each: the tabline with the views and their
// counts, the statusline from the mode block to the code the dashboard runs,
// the command line under it, and the notices stacked at the top right.
import { useLayoutEffect, useRef } from "react";
import type { Dashboard } from "./dashboard";
import { commandInput, completions } from "./commands";
import { basename, checkoutLabel, plural, stateClass, stateLabel, stateSign } from "./review";
import { VIEW_NAMES, VIEWS, type PageState } from "./state";
import { clock, GLYPH, heldCount, heldWord, kindWords, standing, unreadCount } from "./supervision";
import { rowHere } from "./editor";
import { HANDLERS } from "./actions";

export function Tabline({ d, state }: { d: Dashboard; state: PageState }) {
  const live = state.live;
  const working = [...(live?.sessions.values() ?? [])].filter((each) => each.running).length;
  const unread = live === null ? 0 : unreadCount(live);
  const counts: Record<string, string> = {
    supervise: `${d.counted(d.pending(state).length, state)} wait · ${working} working`,
    history: d.counted(d.historyTotal(state), state),
    inbox: unread > 0 ? `${unread} unread` : "",
    threads: `${d.discussions(state).length}`,
    setup: "",
  };
  const roots = d.roots(state);
  return <header id="tabline" role="tablist" aria-label="Views">
    {VIEWS.map((view, index) => <button key={view} type="button" className="tab" role="tab" aria-selected={state.view === view} title={`${index + 1}gt`} onClick={() => d.setView(view)}>
      {index + 1} {VIEW_NAMES[view]}{counts[view] !== "" && <span className="n">{counts[view]}</span>}</button>)}
    <span className="tl-right"><span className="tl-brand">lup</span><span className="muted">dashboard · {window.location.host}</span>
      <button type="button" className="link plain" title=":checkouts" onClick={() => d.set({ float: { kind: "checkouts" } })}>{plural(live?.repositories.size ?? 0, "repository", "repositories")} · {plural(roots.length, "checkout queue")}</button>
      <span className="muted">? keys · / search · : commands · Space leader</span></span>
  </header>;
}

function Code({ d, state }: { d: Dashboard; state: PageState }) {
  const code = state.live?.code;
  if (code === undefined) return null;
  const say = () => d.say(code.older ? (code.failing !== "" ? `runs older code; newer code does not start: ${code.failing}` : "runs older code; restarting onto its checkout's") : `runs ${code.source} from ${code.root}${code.since !== null ? ` since ${clock(code.since)}` : ""}${code.restarted !== "" ? `; restarted after it stopped: ${code.restarted}` : ""}`);
  if (code.older && code.failing !== "") return <button type="button" className="seg wrap err" onClick={say}>dashboard runs older code; its newer code does not start</button>;
  if (code.older) return <button type="button" className="seg warn" onClick={say}>dashboard runs older code; restarting</button>;
  return <>
    {code.restarted !== "" && <button type="button" className="seg wrap warn" onClick={say}>restarted after it stopped: {code.restarted}</button>}
    {code.source !== "" && <button type="button" className="seg muted" title={code.root} onClick={say}>code {code.source.slice(0, 8)}{code.since !== null ? ` · since ${clock(code.since)}` : ""}</button>}
  </>;
}

/** Where keys act now, as the statusline names it; the same place carries the focus outline. */
export function focusLabel(state: PageState): string {
  switch (state.focus) {
    case "queue": return state.view === "history" ? "the history list" : state.view === "threads" ? "the discussions" : "the tree";
    case "editor": return "the buffer";
    case "composer": return state.armed ? "the box · j/k move on" : "the box";
    case "context": return "the context";
    case "setup": return "the repositories";
    case "setupframe": return "the setup";
  }
}

export function Statusline({ d, state }: { d: Dashboard; state: PageState }) {
  const mode = d.mode(state);
  const kind = d.centerKind(state);
  const live = state.live;
  const words = { normal: "NORMAL", insert: "INSERT", visual: "V-LINE", command: "COMMAND", find: "FIND" }[mode];
  const block = mode === "find" ? "command" : mode;
  const parts = [];
  if (kind === "review") {
    const entry = d.current(state);
    if (entry !== null) {
      const { row, detail } = entry;
      const walked = d.walked(state);
      const position = walked.findIndex((each) => each.key === row.key);
      const root = d.roots(state).find((each) => each.id === row.root_id);
      parts.push(<span key="review" className="seg"><span className={`st-${stateClass(row)}`}>{stateSign(row)} {stateLabel(row)}</span> {row.id.slice(0, 8)}{position >= 0 && <span className="muted"> {position + 1}/{walked.length}</span>}</span>);
      parts.push(<span key="target" className="seg" title={row.target}>{checkoutLabel(row.target || root?.path || "", root)}</span>);
      const at = rowHere(d);
      if (at !== undefined && detail !== null) {
        const fi = "fi" in at ? at.fi : undefined;
        const file = fi === undefined ? undefined : detail.files[fi];
        const fileText = file !== undefined && fi !== undefined ? `${basename(file.path)} ${fi + 1}/${detail.files.length}` : at.t === "seg" || at.t === "cmd" || at.t === "segwhy" ? "command" : "";
        const lineText = at.t === "line" ? `${at.side === "before" ? "−" : ""}L${at.num ?? ""}:${state.column + 1}` : at.t === "seg" ? `step ${at.si + 1}` : "";
        const view = state.editor[state.pane].view;
        const whole = fi !== undefined && d.ui(row.key, state).whole.has(fi);
        parts.push(<span key="at" className="seg">{fileText}{lineText !== "" && <span className="muted"> {lineText}</span>} <span className="muted">{view}{whole ? "·whole" : ""}</span></span>);
      }
    }
  } else if (kind === "member") {
    const session = live?.sessions.get(state.sel.key);
    if (session !== undefined) parts.push(<span key="member" className="seg"><span className={`g-${standing(session, state.now)}`}>{GLYPH[standing(session, state.now)]}</span> {session.name || session.id} <span className="muted">{kindWords(session)}</span>{session.running && heldWord(session) !== "" && <span className="warn"> ⏸ {heldWord(session)}</span>}</span>);
  } else if (kind === "you") parts.push(<span key="you" className="seg info">◆ you · {live?.repositories.get(state.sel.key)?.name}</span>);
  else if (kind === "repo") parts.push(<span key="repo" className="seg">{live?.repositories.get(state.sel.key)?.name}</span>);
  else if (kind === "thread") parts.push(<span key="thread" className="seg">» {d.discussion(state)?.title}</span>);
  if (state.focus === "editor" && kind !== "review" && kind !== "empty" && kind !== "setup" && d.buffer(state.pane, state).rows.length > 0) {
    parts.push(<span key="row" className="seg muted">row {state.editor[state.pane].cur + 1}:{state.column + 1}</span>);
  }
  const entry = d.current(state);
  const draft = entry === null ? null : d.draft(entry.row.key, state);
  const unsent = draft === null || entry?.row.state !== "pending" ? 0 : draft.comments.filter((comment) => comment.note.trim() !== "").length + (draft.note.trim() !== "" ? 1 : 0);
  const unread = live === null ? 0 : unreadCount(live);
  const held = live === null ? 0 : heldCount(live);
  const waiting = d.pending(state).length;
  const current = d.queueCurrent(state);
  return <footer id="statusline" aria-label="Status">
    <span className={`seg mode ${block}`}>{words}</span>
    <span className="seg">{VIEW_NAMES[state.view]}</span>
    <span className="seg focusseg" title="Tab and Shift+Tab move between the tree, the buffer, the box and the context">in {focusLabel(state)}</span>
    {parts}
    {state.pending !== "" && <span className="seg warn">{state.pending}</span>}
    {state.armed && mode === "insert" && <span className="seg info" title="the box navigates until you type">j/k move on · type to write</span>}
    <span className="seg fill" />
    {unsent > 0 && <span className="seg warn">✎ {unsent} unsent</span>}
    {held > 0 && <button type="button" className="seg warn" title="agents held at their next tool call; the tree shows the agents that need you (Space t t)" onClick={() => { d.setView("supervise"); HANDLERS["tree.attention"]?.(d, 1, false); }}>⏸{held} held</button>}
    {unread > 0 && <button type="button" className="seg warn" title="gi" onClick={() => d.setView("inbox")}>✉ {unread} to you</button>}
    <button type="button" className={`seg ${waiting > 0 ? "warn" : "ok"}`} onClick={() => d.setView("supervise")}>{d.counted(waiting, state)} wait on you</button>
    <button type="button" className={`seg wrap ${current ? "ok" : state.connection === "Live" ? "warn" : "err"}`} title="Reconnect" onClick={() => d.reconnect()}>{current ? "● live" : state.connection === "Live" ? "◐ some queues unavailable" : `◌ ${state.connection}`}</button>
    <Code d={d} state={state} />
    <button type="button" className="seg" onClick={() => d.set({ float: { kind: "help" } })}>? keys</button>
  </footer>;
}

export function CommandLine({ d, state }: { d: Dashboard; state: PageState }) {
  const input = useRef<HTMLInputElement>(null);
  const line = state.cmdline;
  useLayoutEffect(() => {
    d.elements.command = input.current;
    if (line !== null) input.current?.focus({ preventScroll: true });
    return () => { d.elements.command = null; };
  }, [d, line === null]);
  const wild = line === null || line.wild.length === 0 ? [] : completions(d, line.base).map((each) => ({ ...each }));
  return <div id="cmdline">
    {line !== null && <>
      <span id="cmd-prefix">{line.prefix}</span>
      <input id="cmd-input" ref={input} aria-label={line.prefix === ":" ? "Command line" : "Search"} autoComplete="off" spellCheck={false} value={line.text} onChange={(event) => commandInput(d, event.target.value)} />
    </>}
    {line === null && <span className={`msg ${state.message.tone}`} id="cmd-msg" role="status" aria-live="polite">{state.message.text}</span>}
    <span className="pend">{state.pending}</span>
    {wild.length > 0 && line !== null && <div id="wild" className="float" role="listbox" aria-label="Completions">
      {wild.map((each, index) => <div key={each.value} className={`w${index === line.wildAt ? " cur" : ""}`} role="option" aria-selected={index === line.wildAt}><span>{each.value}</span><span className="muted">{each.description}</span></div>)}
    </div>}
  </div>;
}

export function Notices({ d, state }: { d: Dashboard; state: PageState }) {
  if (state.notes.length === 0) return null;
  return <div id="notify" aria-live="polite" role="region" aria-label="What the page says">
    {state.notes.map((note) => <div key={note.id} className={`nt ${note.tone}${note.open ? " open" : ""}`} role={note.tone === "err" ? "alert" : "status"}
      onClick={(event) => { if (state.narrow && !(event.target instanceof Element && event.target.closest("button") !== null)) d.set((now) => ({ notes: now.notes.map((each) => each.id === note.id ? { ...each, open: !each.open } : each) })); }}>
      <div className="nh"><span>{note.heading}</span><span className="grow muted">{note.title}</span>
        {note.failed !== "" && <button type="button" className="btn" onClick={() => d.openReview(note.failed, { mode: "box" })}>Open it</button>}
        <button type="button" className="x" aria-label="Dismiss" onClick={() => d.dismiss(note.id)}>×</button></div>
      {note.detail !== "" && <p>{note.detail}</p>}
    </div>)}
  </div>;
}
