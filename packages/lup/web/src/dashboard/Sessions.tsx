import { useState, type FormEvent } from "react";
import type { LiveMessage, LiveRepository, LiveSession, ReplyOutcome, SessionActivity } from "../generated/views";
import { sendReply } from "./api";
import { called, conversation, repositoryMessages, sessionTree, type LiveState, type SessionNode } from "./live";

/** What the operator is looking at: one session, one repository's messages, or nothing yet. */
type Chosen = { kind: "session"; key: string } | { kind: "repository"; key: string } | null;

function standing(session: LiveSession): string {
  if (!session.running) return "stopped";
  return session.kind === "subagent" ? "subagent" : "working";
}

function moment(value: string | null): string {
  return value === null ? "" : new Date(value).toLocaleTimeString();
}

/** How a message reaches this session, in the words a reader decides by. */
function reached(session: LiveSession): string {
  if (session.wake !== "") return `its mailbox, then a wake through ${session.wake}`;
  if (session.delivery === "hook") return "its mailbox, handed over before its next tool call";
  return "its mailbox, read when it next looks";
}

function Activity({ activity, brief }: { activity: SessionActivity; brief: boolean }) {
  if (brief) {
    if (activity.calling !== "") return <small className="calling">calling <code>{activity.calling}</code></small>;
    return activity.said === "" ? null : <small className="said-brief">{activity.said}</small>;
  }
  if (activity.transcript === "") return <p className="empty">Its roster row names no transcript, so what it is doing now is not known here.</p>;
  return <>
    {activity.calling !== "" && <details className="calling-detail">
      <summary>Calling <code>{activity.calling}</code></summary>
      <pre>{JSON.stringify(activity.arguments, null, 2)}</pre>
    </details>}
    {activity.said !== "" ? <blockquote className="said">{activity.said}</blockquote> : <p className="empty">Nothing said in its own words yet.</p>}
    <small>{activity.at === null ? "" : `Last at ${moment(activity.at)} · `}<code>{activity.transcript}</code></small>
  </>;
}

function MessageLine({ live, message }: { live: LiveState; message: LiveMessage }) {
  const from = message.sender === "" ? message.door : called(live, message.repository, message.sender);
  const to = called(live, message.repository, message.recipient);
  return <li className={`message${message.sender === "user" ? " from-user" : ""}`}>
    <div className="row-top">
      <span><strong>{from} → {to}</strong>{message.redirect && <span className="state rejected">redirect</span>}</span>
      <time>{moment(message.sent_at)}</time>
    </div>
    <p>{message.text}</p>
    <small>{message.waiting ? "waiting in its mailbox" : "taken"} · through {message.door}</small>
  </li>;
}

function SessionItem({ node, chosen, onChoose }: { node: SessionNode; chosen: string; onChoose(key: string): void }) {
  const { session } = node;
  return <li className={`session-item ${standing(session)}`} data-session={session.key}>
    <button type="button" aria-current={chosen === session.key ? "true" : undefined} onClick={() => onChoose(session.key)}>
      <span className="row-top"><strong>{session.name || session.id}</strong><span className={`state ${standing(session)}`}>{standing(session)}</span></span>
      {session.doing !== "" && <span className="doing">{session.doing}</span>}
      <Activity activity={session.activity} brief />
      {session.waiting > 0 && <small className="waiting">{session.waiting} waiting</small>}
    </button>
    {node.subagents.length > 0 && <ul className="subagents" aria-label={`Subagents of ${session.name || session.id}`}>
      {node.subagents.map((subagent) => <SessionItem key={subagent.key} node={{ session: subagent, subagents: [] }} chosen={chosen} onChoose={onChoose} />)}
    </ul>}
  </li>;
}

function Reply({ session, token }: { session: LiveSession; token: string }) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<ReplyOutcome | null>(null);
  const [error, setError] = useState("");
  async function send(event: FormEvent) {
    event.preventDefault();
    if (sending || draft.trim() === "") return;
    setSending(true);
    setError("");
    setOutcome(null);
    try {
      setOutcome(await sendReply(session.repository, session.id, draft, token));
      setDraft("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSending(false);
    }
  }
  return <form className="reply" onSubmit={(event) => void send(event)}>
    <label htmlFor={`reply-${session.key}`}>Write to {session.name || session.id}</label>
    <textarea id={`reply-${session.key}`} rows={3} value={draft} disabled={sending} placeholder="It reads this as a message from user"
      onChange={(event) => setDraft(event.target.value)} />
    <div className="actions">
      <button className="approve" type="submit" disabled={sending || draft.trim() === ""}>{sending ? "Sending…" : "Send"}</button>
      <span className="decision-hint">Reaches {reached(session)}.</span>
    </div>
    {outcome !== null && <p className="reply-outcome" role="status">{outcome.detail}</p>}
    {error !== "" && <p className="error" role="alert">{error}</p>}
  </form>;
}

function SessionDetail({ live, session, token, onChoose }: { live: LiveState; session: LiveSession; token: string; onChoose(key: string): void }) {
  const here = [...live.sessions.values()].filter((each) => each.repository === session.repository);
  const parent = here.find((each) => each.id === session.parent);
  const subagents = here.filter((each) => each.parent === session.id);
  const messages = conversation(live, session.repository, session.id);
  return <article className="session-detail" aria-label={`Session ${session.name || session.id}`}>
    <header>
      <div className="request-title"><span className={`state ${standing(session)}`}>{standing(session)}</span><h2>{session.name || session.id}</h2></div>
      <dl className="request-location">
        <dt>Id</dt><dd><code>{session.id}</code></dd>
        {parent !== undefined && <><dt>Subagent of</dt><dd><button type="button" onClick={() => onChoose(parent.key)}>{parent.name || parent.id}</button></dd></>}
        <dt>Worktree</dt><dd><code>{session.worktree || "—"}</code></dd>
        {session.task !== "" && <><dt>Task</dt><dd>{session.task}</dd></>}
        <dt>Arrived</dt><dd>{moment(session.arrived)}</dd>
        <dt>Last heard</dt><dd>{moment(session.heard)}</dd>
      </dl>
    </header>
    <section><h3>Says it is doing</h3><p className="doing">{session.doing || "It has not said."}</p>
      {!session.running && (session.summary !== "" || session.error !== "") && <p className="notice">{session.summary || session.error}</p>}
    </section>
    <section className="activity"><h3>Now</h3><Activity activity={session.activity} brief={false} /></section>
    <section><h3>Holding ({session.holding.length})</h3>
      {session.holding.length === 0 ? <p className="empty">Nothing its calls changed or locked.</p>
        : <ul className="holding">{session.holding.map((held) => <li key={held}><code>{held}</code>{session.contested.includes(held) && <span className="state rejected">also held by another session</span>}</li>)}</ul>}
    </section>
    {subagents.length > 0 && <section><h3>Subagents ({subagents.length})</h3><ul className="holding">
      {subagents.map((subagent) => <li key={subagent.key}><button type="button" onClick={() => onChoose(subagent.key)}>{subagent.name || subagent.id}</button> <small>{subagent.doing}</small></li>)}
    </ul></section>}
    <section className="conversation"><h3>Messages ({messages.length})</h3>
      {messages.length === 0 ? <p className="empty">Nothing said to it or by it yet.</p> : <ul>{messages.map((message) => <MessageLine key={message.key} live={live} message={message} />)}</ul>}
    </section>
    {session.running ? <Reply session={session} token={token} />
      : <p className="notice">This session has stopped; nothing would read a message to it.</p>}
  </article>;
}

function RepositoryMessages({ live, repository }: { live: LiveState; repository: LiveRepository }) {
  const messages = repositoryMessages(live, repository.key);
  return <section className="repository-messages session-detail" aria-label={`Messages in ${repository.name}`}>
    <header><h2>{repository.name}</h2><p className="watched-checkout"><code>{repository.repository}</code></p></header>
    <section className="conversation"><h3>Messages between its sessions ({messages.length})</h3>
      {messages.length === 0 ? <p className="empty">No session here has written to another yet.</p> : <ul>{messages.map((message) => <MessageLine key={message.key} live={live} message={message} />)}</ul>}
    </section>
  </section>;
}

/** Every session of every repository the dashboard serves, what each is doing, and what was said. */
export function Sessions({ live, current, token }: { live: LiveState | null; current: boolean; token: string }) {
  const [chosen, setChosen] = useState<Chosen>(null);
  if (live === null) return <div className="sessions"><p className="empty" role="status">Loading sessions…</p></div>;
  const tree = sessionTree(live);
  const session = chosen?.kind === "session" ? live.sessions.get(chosen.key) : undefined;
  const repository = chosen?.kind === "repository" ? live.repositories.get(chosen.key) : undefined;
  const choose = (key: string) => setChosen({ kind: "session", key });
  return <div className="sessions workspace">
    <aside className="queue session-tree" aria-label="Sessions">
      {!current && <p className="empty" role="status">Reconnecting · what is shown may be behind.</p>}
      {tree.length === 0 && <p className="empty">No repository has held the dashboard yet.</p>}
      {tree.map((group) => <section className="repository-group" key={group.repository.key}>
        <h2 className="repository-name tree-repository" title={group.repository.repository}>
          <button type="button" aria-pressed={chosen?.kind === "repository" && chosen.key === group.repository.key} onClick={() => setChosen({ kind: "repository", key: group.repository.key })}>{group.repository.name}</button>
          {" "}<span className="count">({group.sessions.filter((node) => node.session.running).length} working)</span>
        </h2>
        {group.sessions.length === 0 ? <p className="empty">No session.</p>
          : <ul className="session-list">{group.sessions.map((node) => <SessionItem key={node.session.key} node={node} chosen={session?.key ?? ""} onChoose={choose} />)}</ul>}
      </section>)}
    </aside>
    <main className="stage">
      {session !== undefined ? <SessionDetail key={session.key} live={live} session={session} token={token} onChoose={choose} />
        : repository !== undefined ? <RepositoryMessages live={live} repository={repository} />
        : <section className="welcome"><h2>Every session, as it works</h2><p>Choose a session to read what it is doing, what it holds and what was said to it, and to write to it; choose a repository to read what its sessions said to each other.</p></section>}
    </main>
  </div>;
}
