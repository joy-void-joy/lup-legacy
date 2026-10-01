// The context window on the right: everything about what is open, beside the
// lines it describes. For a review: what the call is, the asker's own words as
// prose, what the policy asks about, its files, exceptions, markers and thread.
// Beside an agent: its standing and kind, what needs the operator, its holds,
// mailbox, the reviews it parked, its subagents, and what can be done to it.
import { useLayoutEffect, useRef, type ReactNode } from "react";
import type { LiveSession } from "../generated/views";
import type { Dashboard } from "./dashboard";
import { gotoJudged, moveException, moveMarker, reveal, jumpTo, rowsOf } from "./editor";
import { askedBy, basename, checkoutLabel, EFFECT_SIGN, exceptionRules, exceptionStops, exceptionsOf, headOf, headText, judgedOf, lineSummary, MARKER_LETTER, markerLabel, markerStops, needsReview, plural, relative, SOURCES, staleSentences, stateClass, stateLabel, stateSign } from "./review";
import { unserved, type Feature } from "./served";
import type { PageState } from "./state";
import { activityBrief, ago, attention, childrenOf, clock, GLYPH, heldOthers, holdersOf, inboxOf, inRepository, kindWords, parentOf, reached, reviewsOf, stamp, standing, unreadCount } from "./supervision";
import { claimPath } from "./review";
import { memberName, reaches, type Discussion } from "./threads";
import { Clamp } from "./Touch";

type Item = (act: () => void, body: ReactNode) => ReactNode;

/** The context's items, numbered in the order `j`/`k` walk them while the context has focus. */
function items(d: Dashboard, state: PageState): Item {
  let at = 0;
  return (act, body) => {
    const ci = at;
    at += 1;
    return <span key={ci} className={`it${state.focus === "context" && state.ctxCur === ci ? " cur" : ""}`} data-ci={ci} role="button" tabIndex={-1}
      onClick={() => { d.set({ ctxCur: ci }); act(); if (state.narrow) d.set({ touch: { drawer: "", sheet: "" } }); }}>{body}</span>;
  };
}

/** A proposal's files counted by why each asks, the ones that ask first: `7 protected · 1 new devtools module · 3 written whole`. */
export function reasonCounts(files: { review_label: { kind: string } }[]): string {
  const counts = new Map<string, number>();
  for (const file of files) counts.set(file.review_label.kind || "no verdict captured", (counts.get(file.review_label.kind || "no verdict captured") ?? 0) + 1);
  return [...counts].sort(([left], [right]) => Number(left === "automatic") - Number(right === "automatic")).map(([kind, count]) => `${count} ${kind}`).join(" · ");
}

function ReviewContext({ d, state }: { d: Dashboard; state: PageState }) {
  const entry = d.current(state);
  if (entry === null) return null;
  const { row, detail } = entry;
  const item = items(d, state);
  const root = d.roots(state).find((each) => each.id === row.root_id);
  const target = row.target || root?.path || "";
  const parent = d.parentName(row, state);
  const asker = d.asker(row, state);
  const judged = detail === null ? [] : judgedOf(detail, row);
  const full = d.ui(row.key, state).full;
  const notices = [
    ...(row.stale.length > 0 ? [<div key="stale" className="notice err">Retired as stale: {staleSentences(row).join("; ")}.</div>] : []),
    ...(row.unanswerable !== "" ? [<div key="unanswerable" className="notice blocked">{row.unanswerable}</div>] : []),
    ...(state.failures[row.key] !== undefined ? [<div key="failure" className="notice err">{state.failures[row.key]}</div>] : []),
    ...(detail !== null && detail.preview_notice !== "" ? [<div key="worked" className="notice">How these documents were worked out: {detail.preview_notice}</div>] : []),
  ];
  const sending = state.sending.get(row.key);
  const thread = detail === null ? [] : sending === undefined ? detail.thread : [...detail.thread, sending];
  return <>
    <section className="cx cxhead">
      <div className="kind"><span className={`st-${stateClass(row)}`}>{stateSign(row)} {stateLabel(row)}</span> · {headText(headOf(entry))}</div>
      <div className="title" title={`${target}\n${row.title}`}><span className="muted">{checkoutLabel(target, root)}:</span> {row.title}</div>
      <div>asked by {asker !== undefined ? item(() => d.openOther("member", asker.key), <><b>{askedBy(row)}</b> <span className={`g-${standing(asker, state.now)}`}>{GLYPH[standing(asker, state.now)]} {standing(asker, state.now)}</span></>) : <><b>{askedBy(row)}</b> <span className="muted">(not on the roster)</span></>}
        {parent !== "" && <span className="cyan"> ↳ subagent of {parent}</span>} · {stamp(row.created)} <span className="muted">({ago(row.created, state.now)} ago)</span></div>
      <div className="muted">queue {root !== undefined ? checkoutLabel(root.path, root) : "checkout not watched"}{detail !== null && <> · runs in {checkoutLabel(detail.question.operation.cwd, root)}</>}</div>
      {detail?.question.account.map((said, index) => <div key={index} className="said-by"><p className="src">{SOURCES[said.source] ?? said.source}</p>
        <Clamp d={d} narrow={state.narrow} open={state.unclamped.has(`said:${row.key}:${index}`)} id={`said:${row.key}:${index}`} as="p"><span className="prose">{said.text}</span></Clamp></div>)}
    </section>
    {judged.length > 0 && <section className="cx"><h3>the policy asks about <span className="k">gd</span></h3><p className="warn">{row.rule || "unattributed"}</p>
      {judged.map((each, index) => {
        const where = each.kind === "segment" ? `step ${each.si + 1}: ${each.segment.command}` : each.kind === "file" ? `${relative(each.file.path, target)}${each.lines.length > 0 ? ` · ${lineSummary(each.lines)}` : ""}` : "the command line as a whole";
        return item(() => gotoJudged(d, index), <><span className="warn">?</span> {where}<br /><span className="muted">{each.reason || row.reason}</span></>);
      })}
      {(judged.length > 1 || judged[0]?.reason !== row.reason) && <p className="muted as-put">as the policy put it: {row.reason}</p>}
    </section>}
    {notices.length > 0 && <section className="cx">{notices}</section>}
    {detail !== null && detail.files.length > 0 && <section className="cx"><h3>files · {detail.files.filter(needsReview).length} of {detail.files.length} need review <span className="k">[ ] · F {full ? "full" : "review"}</span></h3>
      <p className="reasons">{reasonCounts(detail.files)}</p>
      {detail.files.map((file, fi) => {
        const comments = d.draft(row.key, state).comments.filter((comment) => comment.path === file.path).length;
        const exceptions = exceptionsOf(file, full).length;
        const holders = state.live === null ? [] : holdersOf(state.live, file.path);
        return item(() => jumpTo(d, rowsOf(d).findIndex((each) => each.t === "file" && each.fi === fi), "top"), <><span className={`eff-${file.review_effect}`}>{EFFECT_SIGN[file.review_effect] ?? "·"}</span> {relative(file.path, target)} <span className="addn">+{file.additions}</span> <span className="deln">−{file.deletions}</span> <span className={`why eff-${file.review_effect}`} title={file.review_reason}>{file.review_label.words}</span>
          {exceptions > 0 && <span className="orange"> {plural(exceptions, "exception")}</span>}{comments > 0 && <span className="warn"> {plural(comments, "comment")}</span>}{file.about !== "" && <span className="info"> note</span>}
          {holders.length > 0 && <span className="cyan"> held by {holders.map((each) => each.name).join(", ")}</span>}</>);
      })}
    </section>}
    {detail !== null && exceptionStops(detail, full).length > 0 && (() => {
      const stops = exceptionStops(detail, full);
      const added = stops.filter((each) => each.suppression.introduced).length;
      return <section className="cx"><h3>rule exceptions · {added} added · {stops.length - added} existing <span className="k">n p</span></h3>
        {stops.map((stop, index) => item(() => { d.set({ exceptionAt: index - 1 }); moveException(d, 1); }, <><span className="orange">X</span> {basename(stop.file.path)}:{stop.suppression.line} <span className="muted">{exceptionRules(stop.suppression, full)}{stop.suppression.introduced ? " · added" : ""}</span><br /><span className="muted">{stop.suppression.reason || "No reason supplied"}</span></>))}
      </section>;
    })()}
    {detail !== null && markerStops(detail, full).length > 0 && <section className="cx"><h3>lup markers · {markerStops(detail, full).length} <span className="k">m M</span></h3>
      {markerStops(detail, full).map((stop, index) => item(() => { d.set({ markerAt: index - 1 }); moveMarker(d, 1); }, <><span className={`an mk-${stop.marker.kind}`}>{MARKER_LETTER[stop.marker.kind]}</span> {basename(stop.file.path)}:{stop.marker.line} <span className="muted">{markerLabel(stop.marker)}</span></>))}
    </section>}
    <section className="cx"><h3>thread · {thread.length}</h3>
      {thread.length === 0 && <p className="muted">Nothing said on it yet. Your note and line comments are sent with your decision, or alone with Alt+Enter.</p>}
      {thread.map((said, index) => {
        const heading = said.kind === "reply" ? `${said.author} replied` : said.kind === "remark" ? `${said.author} commented` : `${said.approved === true ? "Approved" : "Declined"} by ${said.author}`;
        return <div key={index} className={`thr ${said.kind}${said.kind === "answer" ? (said.approved === true ? " yes" : " no") : ""}${said === sending ? " sending" : ""}`}>
          <b>{heading}</b> <span className="muted">{clock(said.at)}{said === sending ? " · sending…" : ""}</span>
          {said.text !== "" && <p>{said.text}</p>}
          {said.comments.map((comment, at) => item(() => {
            const fi = detail?.files.findIndex((file) => file.path === comment.path) ?? -1;
            if (fi >= 0) reveal(d, fi, comment.side, comment.end);
          }, <span key={at}><span className="info">●</span> {relative(comment.path, target)}:{comment.start}{comment.end !== comment.start ? `-${comment.end}` : ""}{comment.side === "before" ? " (before)" : ""} {comment.note}</span>))}
        </div>;
      })}
      {detail?.notification != null && <p className="muted">How the requester heard: {detail.notification.detail}</p>}
    </section>
    <section className="cx"><h3>full context <span className="k">I</span></h3>{item(() => d.set({ float: { kind: "context" } }), <>▸ tool input, the whole record, fingerprint and paths, pretty-printed</>)}</section>
  </>;
}

/** What can be done to an agent, with its key, and whether today's server serves it. */
const ACTIONS: { id: string; keys: string; label: string; needs?: Feature }[] = [
  { id: "write", keys: "c", label: "write to it" },
  { id: "parent", keys: "Space a p", label: "ask its parent session about it" },
  { id: "wake", keys: "Space a w", label: "wake it: today a message wakes it" },
  { id: "reply", keys: "r on a message", label: "reply in the thread of one of its messages", needs: "reply-thread" },
  { id: "nudge", keys: "Space a n", label: "interrupt its turn with a message", needs: "interrupt" },
  { id: "transcript", keys: "T", label: "read its whole transcript, live", needs: "transcript" },
  { id: "redirect", keys: ":redirect", label: "refuse its next tool call with your words", needs: "redirect" },
  { id: "rename", keys: "Space a R", label: "rename it", needs: "rename" },
  { id: "stop", keys: "Space a x", label: "stop it", needs: "stop" },
];

function MemberContext({ d, state, session }: { d: Dashboard; state: PageState; session: LiveSession }) {
  const live = state.live;
  if (live === null) return null;
  const item = items(d, state);
  const now = standing(session, state.now);
  const parent = parentOf(live, session);
  const flags = attention(live, d.roots(state), d.pending(state), session, state.now);
  const mail = [...live.messages.values()].filter((each) => each.repository === session.repository && each.recipient === session.id);
  const parked = reviewsOf(live, d.roots(state), d.rows(state), session, false);
  const children = childrenOf(live, session);
  const home = live.repositories.get(session.repository);
  const act = (id: string) => () => {
    if (id === "write") { if (state.sel.key !== session.key) d.openOther("member", session.key, "box"); else d.focusWin("composer"); return; }
    if (id === "parent" || id === "wake") { d.openOther("member", session.key); d.focusWin("composer"); return; }
  };
  return <>
    <section className="cx cxhead">
      <div className="kind"><span className={`g-${now}`}>{GLYPH[now]} {now}</span> · {session.name || session.id}</div>
      <div className="muted">{kindWords(session)} · live roster</div>
      {parent !== undefined && <div>subagent of {item(() => d.openOther("member", parent.key), <b>{parent.name || parent.id}</b>)}</div>}
      <Clamp d={d} narrow={state.narrow} open={state.unclamped.has(`doing:${session.key}`)} id={`doing:${session.key}`} as="p"><span className="prose">{session.doing || "It has not said what it is on."}</span></Clamp>
    </section>
    {flags.length > 0 && <section className="cx"><h3>needs you</h3>{flags.map((flag) => <p key={flag.key} className="warn">{flag.text}</p>)}</section>}
    <section className="cx"><dl className="facts">
      <dt>id</dt><dd>{session.id}</dd>
      <dt>worktree</dt><dd>{session.worktree !== "" ? inRepository(session.worktree, home) : "—"}</dd>
      {session.task !== "" && <><dt>task</dt><dd>{session.task}</dd></>}
      <dt>arrived</dt><dd>{stamp(session.arrived)}</dd>
      <dt>heard</dt><dd>{clock(session.heard)} <span className="muted">({ago(session.heard, state.now)} ago)</span></dd>
      <dt>reaches it</dt><dd>{session.running ? reached(session) : "nothing: it stopped"}</dd>
      {session.activity.transcript !== "" && <><dt>transcript</dt><dd>{inRepository(session.activity.transcript, home)}</dd></>}
    </dl></section>
    <section className="cx"><h3>holds · {session.holding.length} <span className="k">what its calls changed or locked</span></h3>
      {session.holding.length === 0 && <p className="muted">Nothing its calls changed or locked.</p>}
      {session.holding.map((claim) => {
        const contested = session.contested.includes(claim);
        return <p key={claim}>{contested && <span className="err">! </span>}<span className="muted">{claim.startsWith("under ") ? "locked" : "touched"}</span> {inRepository(claimPath(claim), home)}
          {contested && <span className="err"> also held by {heldOthers(live, session, claim).join(", ") || "another member"}</span>}</p>;
      })}
    </section>
    <section className="cx"><h3>its mailbox</h3><p>{mail.filter((each) => each.waiting).length} waiting · {mail.filter((each) => !each.waiting).length} taken <span className="muted">· of the messages loaded</span></p></section>
    <section className="cx"><h3>reviews it parked · {parked.length} <span className="k">gr</span></h3>
      {parked.length === 0 && <p className="muted">None this page holds.</p>}
      {parked.slice(0, 10).map((row) => item(() => d.openReview(row.key, { mode: "normal" }), <><span className={`st-${stateClass(row)}`}>{stateSign(row)}</span> {d.short(row, state)} <span className="muted">{stateLabel(row)} · {ago(row.created, state.now)}</span></>))}
    </section>
    {children.length > 0 && <section className="cx"><h3>its subagents · {children.length}</h3>
      {children.map((child) => item(() => d.openOther("member", child.key), <><span className={`g-${standing(child, state.now)}`}>{GLYPH[standing(child, state.now)]}</span> {child.name || child.id} <span className="muted">{activityBrief(child, state.now)}</span></>))}
    </section>}
    <section className="cx"><h3>actions <span className="k">today · new server work</span></h3>
      {ACTIONS.map((action) => action.needs === undefined
        ? item(act(action.id), <><span className="orange">{action.keys}</span> {action.label} <i className="srv today">today</i></>)
        : item(() => d.say(unserved(action.needs ?? "stop"), "err"), <><span className="orange">{action.keys}</span> {action.label} <i className="srv new">new server work</i></>))}
    </section>
  </>;
}

/** Beside a discussion: who is in it, each a step from their transcript, how it is grouped, and what posting into it needs. */
function ThreadContext({ d, state, discussion }: { d: Dashboard; state: PageState; discussion: Discussion }) {
  const live = state.live;
  if (live === null) return null;
  const item = items(d, state);
  const first = discussion.posts[0];
  return <>
    <section className="cx cxhead"><div className="kind">» {discussion.kind}</div><p><b>{discussion.title}</b></p>
      <p className="muted">{live.repositories.get(discussion.repository)?.name} · started {stamp(first?.sent_at ?? null)} · last {stamp(discussion.last)}</p></section>
    <section className="cx"><h3>who is in it · {discussion.participants.length}</h3>
      {discussion.participants.map((id) => {
        if (id === "user") return <p key={id} className="info">◆ you</p>;
        const session = [...live.sessions.values()].find((each) => each.repository === discussion.repository && each.id === id);
        if (session === undefined) return <p key={id} className="muted">{id} · not on the roster</p>;
        const now = standing(session, state.now);
        return <p key={id}>{item(() => d.openOther("member", session.key), <><span className={`g-${now}`}>{GLYPH[now]}</span> {session.name || session.id}</>)} <span className="muted">{kindWords(session)} · {now}</span>{" "}
          {item(() => d.say(`${session.name || session.id}'s transcript ${unserved("transcript")}`, "err"), <>transcript</>)}</p>;
      })}
    </section>
    <section className="cx"><h3>how it is grouped</h3><p>{discussion.kind === "thread"
      ? `Every post here replies to another (in_reply_to), back to the first, ${discussion.root.slice(0, 8)}.`
      : "These members wrote to each other and none of it replies to anything, so their messages read as one running conversation."}</p></section>
    <section className="cx"><h3>posting into it <i className="srv new">new server work</i></h3>
      <p>A post reaches {reaches(live, discussion).join(", ") || "nobody yet"}: one message in each mailbox, replying to the last post or the one r chose, shown here once for all of them. It {unserved("thread-post")}.</p>
      {first !== undefined && <p className="muted">r on a post answers it · c writes · T opens its author's transcript ({memberName(live, discussion.repository, first.sender)} wrote first)</p>}</section>
  </>;
}

function RepoContext({ d, state }: { d: Dashboard; state: PageState }) {
  const live = state.live;
  const repository = live?.repositories.get(state.sel.key);
  if (live === null || repository === undefined) return null;
  const item = items(d, state);
  const here = [...live.sessions.values()].filter((each) => each.repository === repository.key).sort((left, right) => Number(right.running) - Number(left.running));
  const before = live.earlier.get(repository.key) ?? 0;
  return <>
    <section className="cx cxhead"><div className="kind">{repository.name}</div><div className="muted">{repository.repository}</div></section>
    <section className="cx"><dl className="facts"><dt>key</dt><dd>{repository.key}</dd><dt>checkout</dt><dd>{repository.checkout}</dd>
      <dt>mail on the stream</dt><dd>{before === 0 ? "every message" : `from byte ${before.toLocaleString("en")}; E loads earlier`}</dd></dl></section>
    <section className="cx"><h3>members · {here.length}</h3>
      {here.map((each) => item(() => d.openOther("member", each.key), <><span className={`g-${standing(each, state.now)}`}>{GLYPH[standing(each, state.now)]}</span> {each.name || each.id} <span className="muted">{activityBrief(each, state.now)}</span></>))}</section>
  </>;
}

function YouContext({ state }: { state: PageState }) {
  const live = state.live;
  if (live === null) return null;
  return <>
    <section className="cx cxhead"><div className="kind"><span className="info">◆</span> you</div>
      <p className="prose">You are a member of every roster, reached at user: agents write to you, and you write to any of them. Supervising is the layer this page puts first; your working verbs are one level down.</p></section>
    <section className="cx"><h3>your inbox</h3><p>{unreadCount(live, state.sel.key)} unread here · {unreadCount(live)} in all <span className="muted">· gi</span></p></section>
    <section className="cx"><h3>what needs new server work</h3>
      <p className="muted">{unserved("describe")}</p><p className="muted">{unserved("claims")}</p><p className="muted">{unserved("notices")}</p></section>
  </>;
}

export function Context({ d, state }: { d: Dashboard; state: PageState }) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    d.elements.context = element.current;
    return () => { d.elements.context = null; };
  }, [d]);
  useLayoutEffect(() => {
    element.current?.querySelector(".it.cur")?.scrollIntoView({ block: "nearest" });
  }, [state.ctxCur]);
  const live = state.live;
  let body: ReactNode = <div className="intro"><p>What you select on the left shows its context here: an agent's standing, holds, mailbox and actions; a review's head, what the policy asks about, and its files.</p></div>;
  switch (d.centerKind(state)) {
    case "review": body = <ReviewContext d={d} state={state} />; break;
    case "member": { const session = live?.sessions.get(state.sel.key); if (session !== undefined) body = <MemberContext d={d} state={state} session={session} />; break; }
    case "repo": body = <RepoContext d={d} state={state} />; break;
    case "you": body = <YouContext state={state} />; break;
    case "thread": { const discussion = d.discussion(state); if (discussion !== undefined) body = <ThreadContext d={d} state={state} discussion={discussion} />; break; }
    case "inbox": {
      const target = d.boxTarget(state);
      body = target.kind === "member" ? <MemberContext d={d} state={state} session={target.session} /> : <div className="intro"><p>{live !== null && inboxOf(live).length === 0 ? "Nothing has been sent to you." : "The sender of the message under the cursor shows here."}</p></div>;
      break;
    }
    default:
  }
  return <section id="w-context" className={`win${state.focus === "context" ? " focus" : ""}`} data-win="context" aria-label="Context"
    onClick={() => { if (d.state.focus !== "context" && !state.narrow && d.state.focus === state.focus) d.focusWin("context"); }}>
    <div className="wb" id="cbar"><span className="t">context</span><span className="grow" /><span className="muted">K hover · I full context</span>
      {state.narrow && <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ touch: { drawer: "", sheet: "" } })}>✕</button>}</div>
    <div className="buf" id="context" ref={element} tabIndex={-1}>{body}</div>
  </section>;
}
