// The editor in the middle: its window bar says what the call is and what the
// policy asks about; its panes hold the buffer; and the split under it is the
// box — the note on a review, which a review opens in, or the message box
// beside an agent.
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Dashboard } from "./dashboard";
import { searchRegex, visualSpan } from "./editor";
import { BufferView } from "./Buffer";
import { askedBy, headOf, headText, judgedOf, judgedSummary, lineCount, plural, stateClass, stateLabel, stateSign } from "./review";
import type { PageState } from "./state";
import { ago, attention, GLYPH, kindWords, parentOf, reached, standing, stamp } from "./supervision";
import { discussionLine, memberName, reaches } from "./threads";
import { NavStrip } from "./Touch";

function ReviewBar({ d, state }: { d: Dashboard; state: PageState }) {
  const [folded, setFolded] = useState(true);
  const entry = d.current(state);
  if (entry === null) return null;
  const { row, detail } = entry;
  const asker = d.asker(row, state);
  const parent = d.parentName(row, state);
  const summary = detail === null ? "" : judgedSummary(judgedOf(detail, row), row, state.narrow ? "the context (ⓘ) lists them" : `${d.keymap.spoken("judged")} walks them`);
  const standingNow = asker === undefined ? null : standing(asker, state.now);
  // On a phone the top bar already says the head, the asker and the id: the bar keeps what the policy asks about, folded to two lines.
  if (state.narrow) {
    return summary === "" ? null : <div className={`wb fold${folded ? "" : " open"}`} id="ebar" role="button" tabIndex={0} aria-expanded={!folded}
      onClick={() => setFolded(!folded)}><span className="l2">{summary}</span></div>;
  }
  return <div className="wb" id="ebar">
    <span className="l1"><span className={`st-${stateClass(row)}`}>{stateSign(row)}</span> {headText(headOf(entry))}</span>
    <span className="muted">asked by</span><b>{askedBy(row)}</b>
    {parent !== "" && <span className="cyan">↳ subagent of {parent}</span>}
    {standingNow === null ? <span className="muted">not on the roster</span> : <span className={`g-${standingNow}`}>{GLYPH[standingNow]} {standingNow}</span>}
    <span className="muted">{ago(row.created, state.now)} ago</span>
    <span className="grow" /><span className="muted">{row.id.slice(0, 8)}</span>
    {summary !== "" && <span className="l2">{summary}</span>}
  </div>;
}

function Bar({ d, state }: { d: Dashboard; state: PageState }) {
  const live = state.live;
  switch (d.centerKind(state)) {
    case "review": return <ReviewBar d={d} state={state} />;
    case "member": {
      const session = live?.sessions.get(state.sel.key);
      if (session === undefined || live === null) return null;
      const now = standing(session, state.now);
      const flags = attention(live, d.roots(state), d.pending(state), session, state.now).filter((flag) => flag.key !== "asks");
      return <div className="wb" id="ebar">
        <span className="l1"><span className={`g-${now}`}>{GLYPH[now]}</span> {session.name || session.id}</span>
        <span className="muted">{kindWords(session)} · {now} · heard {ago(session.heard, state.now)} ago</span>
        <span className="grow" /><span className="muted">c write · Space a acts on it</span>
        <span className="l2 plain">{session.doing || "It has not said what it is on."}{flags.length > 0 && <span className="warn"> · {flags.map((flag) => flag.text).join(" · ")}</span>}</span>
      </div>;
    }
    case "you": return <div className="wb" id="ebar"><span className="l1"><span className="info">◆</span> you in {live?.repositories.get(state.sel.key)?.name}</span><span className="muted">a peer, reached at user</span><span className="grow" /><span className="muted">Space p for your verbs</span></div>;
    case "repo": return <div className="wb" id="ebar"><span className="l1">▾ {live?.repositories.get(state.sel.key)?.name}</span><span className="muted">every member and what they said to each other</span><span className="grow" /><span className="muted">c broadcasts</span></div>;
    case "inbox": return <div className="wb" id="ebar"><span className="l1">✉ inbox</span><span className="muted">everything addressed to you, in every repository</span><span className="grow" /><span className="muted">Enter opens its sender</span></div>;
    case "thread": {
      const discussion = d.discussion(state);
      if (discussion === undefined || live === null) return null;
      return <div className="wb" id="ebar"><span className="l1">» {discussion.title}</span>
        <span className="muted">{discussionLine(live, discussion)}</span><span className="grow" /><span className="muted">r replies to a post · c posts · T its author's transcript</span></div>;
    }
    default: return <div className="wb" id="ebar"><span className="l1">{state.view === "history" ? "history" : "supervise"}</span></div>;
  }
}

function Intro({ d, state }: { d: Dashboard; state: PageState }) {
  if (state.linked !== null) {
    const found = d.rows(state).filter((row) => row.id === state.linked?.id);
    return <div className="intro" role="status"><h2>{!d.queueCurrent(state) ? "Loading requested review…" : found.length > 1 ? "This request ID exists in multiple checkouts" : "Request not found"}</h2>
      <p>Requested review: <code>{state.linked.id}</code></p>
      <p>{found.length > 1 ? "Choose the intended checkout from the tree." : "This page keeps watching for that exact request in the selected repositories; it never opens a different one."}</p>
      <p><button type="button" className="btn" onClick={() => { d.set({ linked: null }); d.setView("supervise"); }}>Supervise</button></p></div>;
  }
  if (state.view === "history") return <div className="intro"><h2>History</h2><p>Choose a request on the left, or Space f h to find one.</p></div>;
  const waiting = d.pending(state).length;
  return <div className="intro"><h2>{state.live === null ? "Loading…" : !d.queueCurrent(state) ? state.connection : waiting === 0 ? "Nothing waits on you" : "Ready for the next request"}</h2>
    <p>Every agent is on the left. Choose one to read what it is doing and write to it; j opens the next review waiting on you.</p>
    <p className="muted">? lists every key · : opens the command line · Space shows what the leader does</p></div>;
}

/** The note box on a waiting review: who reads it, the decisions beside it, and what is drafted. */
function NoteBox({ d, state }: { d: Dashboard; state: PageState }) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    d.elements.box = box.current;
    return () => { d.elements.box = null; };
  });
  const entry = d.current(state);
  if (entry === null) return null;
  const { row, detail } = entry;
  if (row.state !== "pending") {
    const answer = detail?.question.answer ?? null;
    const said = answer !== null ? `${answer.approved ? "Approved" : "Declined"} by ${answer.principal} ${stamp(answer.at)}${answer.note !== "" ? ` — “${answer.note}”` : ""}`
      : row.state === "expired" ? `expired ${stamp(row.settled)}: its requester left the roster before anybody answered` : stateLabel(row);
    return <div id="composer"><div className="answered" role="status"><span className={`st-${stateClass(row)}`}>{stateSign(row)} {stateLabel(row)}</span> <span className="muted">·</span> {said}
      {detail?.notification != null && <span className="muted"> · how the requester heard: {detail.notification.detail}</span>}</div></div>;
  }
  const key = row.key;
  const draft = d.draft(key, state);
  const failure = state.failures[key] ?? "";
  const parent = d.parentName(row, state);
  const comments = draft.comments.filter((comment) => comment.note.trim() !== "").length;
  const canDecide = row.answerable || row.unanswerable === "";
  return <div id="composer">
    {failure !== "" && <div className="notice err" role="alert">{failure}</div>}
    <div className="wb"><span className="t">note</span><span className="muted">to</span><b>{askedBy(row)}</b>{parent !== "" && <span className="muted">(a copy goes to {parent})</span>}
      <span className="grow" />{comments > 0 ? <span className="warn">{plural(comments, "line comment")} drafted</span> : <span className="muted">no line comments</span>}</div>
    <label htmlFor="note" className="sr">Note for the requesting agent</label>
    <textarea id="note" ref={box} value={draft.note} spellCheck
      placeholder={state.narrow ? "Note for the requesting agent: it goes with Approve or Decline, or alone with Send."
        : "Note for the requesting agent: sent with your decision (Ctrl+Enter approves, Alt+Delete declines) or alone (Alt+Enter). While it is empty, j and k move to the next and previous review; the first letter you type makes it yours. Esc reads in Normal mode."}
      onPointerDown={() => { if (d.state.armed) d.set({ armed: false }); }}
      onClick={(event) => { if (document.activeElement !== event.currentTarget) event.currentTarget.focus(); }}
      onChange={(event) => { const note = event.target.value; d.set({ armed: false, typing: true }); d.setDraft(key, () => ({ note })); }} />
    <div className="acts">
      {canDecide ? <>
        <button type="button" className="btn approve" disabled={!row.answerable || row.stale.length > 0} aria-keyshortcuts="Control+Enter Meta+Enter" title="Ctrl+Enter (Cmd+Enter), from anywhere"
          onClick={(event) => { if (event.detail < 2) d.answer("approve"); }}>Approve <kbd>Ctrl+Enter</kbd></button>
        <button type="button" className="btn decline" disabled={!row.answerable} aria-keyshortcuts="Alt+Delete" title="Alt+Delete (forward Delete), from anywhere"
          onClick={(event) => { if (event.detail < 2) d.answer("decline"); }}>Decline <kbd>Alt+Del</kbd></button>
      </> : <span className="purple" role="status">⊘ {row.unanswerable}</span>}
      <button type="button" className="btn" disabled={draft.note.trim() === "" && comments === 0} aria-keyshortcuts="Alt+Enter" title="Alt+Enter, from anywhere" onClick={() => d.answer("remark")}>Send without deciding <kbd>Alt+Enter</kbd></button>
      <span className="muted">Esc normal · c back · Alt+↑↓ move</span>
    </div>
  </div>;
}

/** The message box beside an agent, a repository's broadcast box, or the box under the inbox. */
function MessageBox({ d, state }: { d: Dashboard; state: PageState }) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    d.elements.box = box.current;
    return () => { d.elements.box = null; };
  });
  const target = d.boxTarget(state);
  if (target.kind === "none") return null;
  const outcome = state.replyOutcome[target.key];
  const said = outcome === undefined ? null : <span className={outcome.error ? "err" : "muted"} role="status">{outcome.text}</span>;
  const draft = state.replyDrafts[target.key] ?? "";
  const typed = (text: string) => d.set((now) => ({ typing: true, replyDrafts: { ...now.replyDrafts, [target.key]: text } }));
  if (target.kind === "thread") {
    const live = state.live;
    const discussion = target.discussion;
    const reach = live === null ? [] : reaches(live, discussion);
    const answering = discussion.posts.find((post) => post.id === state.threadReply);
    return <div id="composer">
      <div className="wb"><span className="t">post</span><span className="muted">reaches</span><b>{reach.join(", ") || "nobody yet"}</b>
        {answering !== undefined && live !== null && <span className="muted">· answering {memberName(live, discussion.repository, answering.sender)}: {answering.text.split("\n")[0]}</span>}</div>
      <textarea id="reply" ref={box} aria-label={`Post to ${reach.join(", ")}`} value={draft}
        placeholder="c writes here · Alt+Enter posts to everyone in the discussion · Esc leaves" onChange={(event) => typed(event.target.value)}
        onClick={(event) => { if (document.activeElement !== event.currentTarget) event.currentTarget.focus(); }} />
      <div className="acts"><button type="button" className="btn approve" onClick={() => d.sendBox()}>Post <kbd>Alt+Enter</kbd></button>
        <span className="muted">one post to each of them, each woken, replying to {answering !== undefined ? "the post r chose" : "the last post"}</span>{said}</div>
    </div>;
  }
  if (target.kind === "repo") {
    const working = [...(state.live?.sessions.values() ?? [])].filter((each) => each.repository === target.repository && each.running).length;
    return <div id="composer">
      <div className="wb"><span className="t">broadcast</span><span className="muted">to every working member of {state.live?.repositories.get(target.repository)?.name} ({working}), one post between them, each woken</span></div>
      <textarea id="reply" ref={box} aria-label="Broadcast" value={draft} placeholder="c writes here · Alt+Enter sends to every working member · Esc leaves" onChange={(event) => typed(event.target.value)}
        onClick={(event) => { if (document.activeElement !== event.currentTarget) event.currentTarget.focus(); }} />
      <div className="acts"><button type="button" className="btn approve" onClick={() => d.sendBox()}>Broadcast <kbd>Alt+Enter</kbd></button>{said}</div>
    </div>;
  }
  const session = target.session;
  const live = state.live;
  if (!session.running) {
    const parent = live === null ? undefined : parentOf(live, session);
    return <div id="composer"><div className="notice">{session.name || session.id} has stopped; nothing would read a message to it.{parent !== undefined ? ` Its session ${parent.name || parent.id} is ${standing(parent, state.now)}: Space a p asks it.` : ""}</div></div>;
  }
  const answering = live === null || (state.replyTo[target.key] ?? "") === "" ? undefined
    : [...live.messages.values()].find((message) => message.repository === session.repository && (message.post || message.id) === state.replyTo[target.key]);
  const interrupting = d.lacks("interrupt");
  const redirecting = d.lacks("redirect");
  return <div id="composer">
    <div className="wb"><span className="t">message</span><span className="muted">to</span><b>{session.name || session.id}</b><span className="muted">· reaches {reached(session)}</span>
      {answering !== undefined && <span className="muted">· answering {answering.sender === "user" ? "you" : session.name || session.id}: {answering.text.split("\n")[0]}{" "}
        <span className="it" role="button" tabIndex={-1} onClick={() => d.set((now) => ({ replyTo: { ...now.replyTo, [target.key]: "" } }))}>✕</span></span>}</div>
    <textarea id="reply" ref={box} aria-label={`Write to ${session.name || session.id}`} value={draft} placeholder="c writes here · Alt+Enter sends · Esc leaves · it reads this as a message from user"
      onChange={(event) => typed(event.target.value)} onClick={(event) => { if (document.activeElement !== event.currentTarget) event.currentTarget.focus(); }} />
    <div className="acts"><button type="button" className="btn approve" onClick={() => d.sendBox()}>Send <kbd>Alt+Enter</kbd></button>
      <button type="button" className="btn" disabled={interrupting !== ""} title={interrupting || "a Claude turn that is generating ends at once, and a tool call already running finishes first; a Codex turn is stopped and the message taken next"}
        onClick={() => void d.interrupt(session, draft)}>Interrupt <kbd>Space a n</kbd></button>
      <button type="button" className="btn" disabled={redirecting !== "" || draft.trim() === ""} title={redirecting || "refuses its next tool call with these words"}
        onClick={() => void d.sendTo(session, draft, { redirect: true })}>Redirect</button>
      <span className="muted">{kindWords(session) === "subagent" ? "a subagent reads it before its next tool call; an interrupt stops its session's turn" : "it reads this before its next tool call, or at once where it idles"}</span>{said}</div>
  </div>;
}

export function Editor({ d, state }: { d: Dashboard; state: PageState }) {
  const kind = d.centerKind(state);
  const entry = kind === "review" ? d.current(state) : null;
  const detail = entry?.detail ?? null;
  const splitting = kind === "review" ? state.split : "";
  const pattern = useMemo(() => {
    const typed = state.search.typing;
    const shown = typed !== null ? typed : state.search.lit ? state.search.pattern : "";
    return shown === "" ? null : searchRegex(shown);
  }, [state.search]);
  const span = visualSpan(d);
  const judged = useMemo(() => detail === null || entry === null ? [] : judgedOf(detail, entry.row), [detail, entry]);
  const judgedRule = (fi: number) => {
    const item = judged.find((each) => each.kind === "file" && each.fi === fi);
    return item?.rule || entry?.row.rule || "";
  };
  const checkout = entry === null ? "" : entry.row.target || (d.roots(state).find((root) => root.id === entry.row.root_id)?.path ?? "");
  const peeked = [state.peeks[0]?.text ?? null, state.peeks[1]?.text ?? null];
  const longest = detail === null ? 9 : Math.max(9, ...detail.files.map((file) => Math.max(lineCount(file.before), lineCount(file.after))), ...peeked.map(lineCount));
  const numberWidth = `${Math.max(3, String(longest).length + 1)}ch`;
  const panes: (0 | 1)[] = splitting === "" ? [0] : [0, 1];
  const loading = kind === "review" && detail === null;
  const titles: Record<string, string> = { diff: "diff", before: "before", after: "after", raw: "unified (raw)" };
  return <section id="w-editor" className="win" data-win="editor" aria-label="Editor">
    <Bar d={d} state={state} />
    {state.narrow && kind === "review" && <NavStrip d={d} state={state} />}
    <div id="panes" className={splitting}>
      {kind === "empty" ? <div className="buf pane active" tabIndex={-1} ref={(element) => { d.elements.panes[0] = element; }}><Intro d={d} state={state} /></div>
        : loading ? <div className="buf pane active" tabIndex={-1} ref={(element) => { d.elements.panes[0] = element; }}><p className="intro" role="status">Loading request…</p></div>
          : panes.map((pane) => <BufferView key={pane} d={d} pane={pane} rows={d.buffer(pane, state).rows} cur={state.editor[pane].cur} want={state.editor[pane].want}
            active={state.pane === pane && state.focus === "editor"} focused={state.pane === pane && state.focus === "editor" && !state.narrow}
            editing={state.editing} pattern={state.pane === pane ? pattern : null}
            span={state.visual?.pane === pane ? span : null} files={detail?.files ?? []} target={entry?.row.target ?? ""} live={state.live}
            touch={state.narrow} judgedRule={judgedRule} title={splitting === "" ? "" : titles[state.editor[pane].view] ?? ""} numberWidth={numberWidth}
            unclamped={state.unclamped} now={state.now} review={entry?.row.key ?? ""} checkout={checkout} peek={d.peek(pane, state)} semantic={state.semantic} />)}
    </div>
    {kind === "review" ? <NoteBox d={d} state={state} /> : <MessageBox d={d} state={state} />}
  </section>;
}
