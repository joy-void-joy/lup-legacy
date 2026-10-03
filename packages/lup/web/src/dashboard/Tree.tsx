// The window on the left: the agents tree — every repository, the operator's
// own row, each session with its subagents, and under each agent the reviews
// it parked that wait on the operator — or, in History, the requests that
// left the queue, and in Threads, the discussions.
import { useLayoutEffect, useRef } from "react";
import { grouped, type Dashboard } from "./dashboard";
import { openItem, toggleStopped } from "./editor";
import { checkoutLabel, plural, staleSentences, stateClass, stateLabel, stateSign } from "./review";
import type { PageState } from "./state";
import { activityBrief, ago, GLYPH, heldCount, heldWord, holdTitle, membersOf, repositoryOf, reviewsOf, standing, unreadCount, wroteYou, type TreeItem } from "./supervision";
import { discussionLine } from "./threads";

/** A tree row's second line: what the agent is doing, its first line whole, and how many more a hover reads. */
function brief(text: string): { first: string; more: number } {
  const lines = text.split("\n").map((line) => line.trim()).filter((line) => line !== "");
  return { first: lines[0] ?? "", more: Math.max(0, lines.length - 1) };
}

function TreeRow({ d, state, item, index }: { d: Dashboard; state: PageState; item: TreeItem; index: number }) {
  const live = state.live;
  if (live === null) return null;
  const selected = state.sel.kind === item.t && state.sel.key === item.key;
  const cur = state.focus === "queue" && index === state.treeCur;
  const classes = `tr ${item.t}${selected ? " sel" : ""}${cur ? " cur" : ""}`;
  const open = () => {
    d.set({ treeCur: index });
    if (item.t === "folded") { toggleStopped(d, item.key); return; }
    openItem(d, item, !state.narrow);
    if (state.narrow) d.set({ touch: { drawer: "", sheet: "" } });
  };
  const style = { ["--depth" as string]: item.depth };
  switch (item.t) {
    case "repo": {
      const repository = live.repositories.get(item.key);
      if (repository === undefined) return null;
      const working = membersOf(live, item.key).filter((each) => each.running).length;
      const asks = d.pending(state).filter((row) => repositoryOf(live, d.roots(state), row.root_id) === item.key).length;
      const unread = unreadCount(live, item.key);
      const held = heldCount(live, item.key);
      return <button type="button" className={classes} style={style} data-ti={index} title={repository.repository} onClick={open}>
        <span className="t1"><span>{state.collapsed.has(item.key) ? "▸" : "▾"}</span><span className="nm">{repository.name} <span className="muted">· {working} working{held > 0 && <> · <span className="warn">⏸{held} held</span></>}{asks > 0 && <> · <span className="warn">{asks} wait on you</span></>}{unread > 0 && <> · <span className="warn">✉ {unread} to you</span></>}</span></span></span>
      </button>;
    }
    case "you": return <button type="button" className={classes} style={style} data-ti={index} onClick={open}>
      <span className="t1"><span className="info">◆</span><span className="nm">you <span className="muted">· user</span></span>
        {unreadCount(live, item.key) > 0 && <span className="flags warn">✉{unreadCount(live, item.key)}</span>}</span>
    </button>;
    case "member": {
      const session = live.sessions.get(item.key);
      if (session === undefined) return null;
      const now = standing(session, state.now);
      const asks = reviewsOf(live, d.roots(state), d.pending(state), session).length;
      const wrote = wroteYou(live, session);
      const what = brief(activityBrief(session, state.now));
      const held = session.running ? heldWord(session) : "";
      const foldable = asks > 0 || membersOf(live, session.repository).some((each) => each.parent === session.id);
      return <button type="button" className={`${classes}${session.running ? "" : " stopped"}`} style={style} data-ti={index} onClick={open}>
        <span className="t1">
          {foldable ? <span className="fold" onClick={(event) => { event.stopPropagation(); d.set((now_) => { const collapsed = new Set(now_.collapsed); if (collapsed.has(session.key)) collapsed.delete(session.key); else collapsed.add(session.key); return { collapsed }; }); }}>{state.collapsed.has(session.key) ? "▸" : "▾"}</span> : <span> </span>}
          <span className="nm">{session.parent !== "" && <span className="cyan">↳ </span>}<span className={`g-${now}`} title={now}>{GLYPH[now]}</span> <b>{session.name || session.id}</b>{session.parent === "" && session.wake !== "" && <span className="tag"> {session.wake}</span>}</span>
          <span className="flags">
            {held !== "" && <span className="warn held" title={holdTitle(live, session, state.now)}>⏸ {held} </span>}
            {asks > 0 && <span className="warn" title="reviews wait on you">?{asks} </span>}
            {wrote > 0 && <span className="warn" title="unread messages it sent you">✎{wrote} </span>}
            {session.running && session.waiting > 0 && <span className="info" title="messages waiting in its mailbox">✉{session.waiting} </span>}
            {session.holding.length > 0 && <span className="muted" title="paths its calls hold">⌂{session.holding.length} </span>}
            {session.contested.length > 0 && <span className="err" title="held by another member too">!{session.contested.length} </span>}
            <span className="muted">{ago(now === "quiet" ? session.activity.at : session.heard, state.now)}</span>
          </span>
        </span>
        <span className="t2">{what.first}{what.more > 0 && <span className="muted"> · {plural(what.more, "more line")}, K</span>}</span>
      </button>;
    }
    case "folded": return <button type="button" className={classes} style={style} data-ti={index} onClick={open}>
      <span className="t1"><span>{item.open ? "▾" : "▸"}</span><span className="nm muted">{plural(item.count, "stopped agent")} {item.open ? "shown · za folds them" : "folded · za shows them"}</span></span>
    </button>;
    case "review": {
      const row = d.rows(state).find((each) => each.key === item.key);
      if (row === undefined) return null;
      return <button type="button" className={classes} style={style} data-ti={index} onClick={open}>
        <span className="t1"><span className={`st-${stateClass(row)}`} title={stateLabel(row)}>{stateSign(row)}</span><span className="nm">{d.short(row, state)}{item.orphan && <span className="muted"> · asked by {row.session || row.requester}, not on the roster</span>}</span><span className="muted">{ago(row.created, state.now)}</span></span>
      </button>;
    }
  }
}

function AgentsTree({ d, state }: { d: Dashboard; state: PageState }) {
  const items = d.tree(state);
  const live = state.live;
  const all = [...(live?.sessions.values() ?? [])];
  const working = all.filter((each) => standing(each, state.now) === "working").length;
  const quiet = all.filter((each) => standing(each, state.now) === "quiet").length;
  const held = live === null ? 0 : heldCount(live);
  const waiting = d.pending(state).length;
  const reasons = [...(state.connection === "Live" ? [] : [state.connection]), ...(live?.reviews.errors ?? []).map((issue) => `${issue.root.slice(issue.root.lastIndexOf("/") + 1) || issue.root} unavailable: ${issue.message}`)];
  return <>
    <div className="wb" id="qbar"><span className="t">agents</span><span>{working} working{quiet > 0 && <> · <span className="warn">{quiet} quiet</span></>}{held > 0 && <> · <span className="warn" title="agents held at their next tool call">⏸{held} held</span></>} · {d.counted(waiting, state)} wait on you</span><span className="grow" /><span className="muted" title="Space t a / t t / t r">{state.tree}</span>
      {state.narrow && <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ touch: { drawer: "", sheet: "" } })}>✕</button>}</div>
    <Scroller d={d} state={state}>
      {reasons.length > 0 && <div className="qstate" role="status">{reasons.join(" · ")}</div>}
      {live === null ? <div className="intro" role="status"><p>Loading…</p></div>
        : items.length === 0 ? <div className="intro"><p>{state.tree === "all" ? "No repository has held the dashboard yet." : "Nothing here needs you. Space t a shows every agent."}</p></div>
          : items.map((item, index) => <TreeRow key={`${item.t}:${item.key}`} d={d} state={state} item={item} index={index} />)}
    </Scroller>
  </>;
}

function HistoryList({ d, state }: { d: Dashboard; state: PageState }) {
  const settled = d.settled(state);
  const total = d.historyTotal(state);
  const roots = d.roots(state);
  return <>
    <div className="wb" id="qbar"><span className="t">History</span><span>{d.counted(total, state)}</span><span className="grow" /><span className="muted">{state.settings.advance ? "advance" : "stay"}</span>
      {state.narrow && <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ touch: { drawer: "", sheet: "" } })}>✕</button>}</div>
    <Scroller d={d} state={state}>
      {settled.length === 0 && <div className="intro"><p>No answered requests yet.</p></div>}
      {grouped(settled, roots).map((group) => <section key={group.repository} aria-label={`Repository ${group.name}`}>
        <div className="qg" title={group.repository}>▾ {group.name} <span className="muted">{group.sessions.reduce((total_, asking) => total_ + asking.rows.length, 0)}</span></div>
        {group.sessions.map((asking) => <div key={asking.session}>
          <div className="qs">asked by <b>{asking.session}</b> <span className="muted">{asking.rows.length}</span></div>
          {asking.rows.map((row) => {
            const root = roots.find((each) => each.id === row.root_id);
            const facts = [stateLabel(row), checkoutLabel(row.target || root?.path || "", root), row.said > 0 ? `${plural(row.said, "comment")} in its thread` : "", row.archived ? "archived" : ""].filter((each) => each !== "").join(" · ");
            const when = row.settled ?? row.created;
            return <button type="button" key={row.key} className={`q${state.sel.key === row.key ? " sel" : ""}`} onClick={() => d.openReview(row.key, { mode: state.narrow ? "normal" : "queue" })}>
              <span className="q1"><span className={`st-${stateClass(row)}`} title={stateLabel(row)}>{stateSign(row)}</span><span className="h">{d.short(row, state)}</span><span className="muted">{ago(when, state.now)}</span></span>
              <span className="q2">{facts}</span>
              {row.stale.length > 0 && <span className="q3 err">stale: {staleSentences(row).join("; ")}</span>}
            </button>;
          })}
        </div>)}
      </section>)}
      {settled.length < total && <button type="button" className="btn qmore" disabled={state.olderLoading} onClick={() => void d.loadOlder()}>
        {state.olderLoading ? "Loading older requests…" : `Load older requests (${(total - settled.length).toLocaleString("en")} more) · :older`}</button>}
    </Scroller>
  </>;
}

/** Threads' list: every discussion in every repository, the most recently written first. */
function DiscussionList({ d, state }: { d: Dashboard; state: PageState }) {
  const live = state.live;
  const all = d.discussions(state);
  const unread = all.reduce((total, each) => total + each.unread, 0);
  return <>
    <div className="wb" id="qbar"><span className="t">discussions</span><span>{all.length} · {unread} unread to you</span><span className="grow" />
      {state.narrow && <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ touch: { drawer: "", sheet: "" } })}>✕</button>}</div>
    <Scroller d={d} state={state}>
      {live === null ? <div className="intro" role="status"><p>Loading…</p></div>
        : all.length === 0 ? <div className="intro"><p>No discussion yet: nobody has written to anybody.</p></div>
          : all.map((each) => {
            const selected = state.sel.kind === "thread" && state.sel.key === each.key;
            return <button type="button" key={each.key} className={`tr thr${selected ? " sel" : ""}${selected && state.focus === "queue" ? " cur" : ""}`}
              onClick={() => { d.openThread(each.key, state.narrow ? "normal" : "queue"); }}>
              <span className="t1">{each.unread > 0 ? <span className="warn" title="unread to you">●</span> : <span className="muted">○</span>}
                <span className="nm">{each.title}</span><span className="muted">{ago(each.last, state.now)}</span></span>
              <span className="t2">{discussionLine(live, each)}</span>
            </button>;
          })}
    </Scroller>
  </>;
}

function Scroller({ d, state, children }: { d: Dashboard; state: PageState; children: React.ReactNode }) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    d.elements.queue = element.current;
    return () => { d.elements.queue = null; };
  }, [d]);
  useLayoutEffect(() => {
    element.current?.querySelector(".tr.cur, .tr.sel, .q.sel")?.scrollIntoView({ block: "nearest" });
  }, [state.treeCur, state.sel]);
  return <div className="buf" id="queue" ref={element} tabIndex={-1}>{children}</div>;
}

export function Left({ d, state }: { d: Dashboard; state: PageState }) {
  const label = state.view === "history" ? "History" : state.view === "threads" ? "Discussions" : "Agents and the reviews they parked";
  return <section id="w-queue" className={`win${state.focus === "queue" ? " focus" : ""}`} data-win="queue" aria-label={label}
    onClick={() => { if (d.state.focus !== "queue" && !state.narrow) d.focusWin("queue"); }}>
    {state.view === "history" ? <HistoryList d={d} state={state} /> : state.view === "threads" ? <DiscussionList d={d} state={state} /> : <AgentsTree d={d} state={state} />}
  </section>;
}
