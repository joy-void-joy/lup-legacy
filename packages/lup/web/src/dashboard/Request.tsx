// One review as the operator reads it: the editor in the centre, the context
// around it in single lines, and the composer pinned beneath, open and
// focused, so the operator writes and decides without leaving the keyboard.
import { memo, useEffect, useRef, useState, type RefObject } from "react";
import type { Account, CommandSegment, LineComment, ReviewDetail, ReviewRoot, ReviewSummary, ThreadEntry, UnpreviewedStep } from "../generated/views";
import { reviewLink } from "./api";
import { Files, reviewLabel, type DraftComment, type FileNavigation } from "./Files";

const FileEvidence = memo(Files);
const JsonRecord = memo(function JsonRecord({ value }: { value: unknown }) {
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
});

/** What the operator is writing on one review: the note, and the line comments drafted on its diffs. */
export type Draft = { note: string; comments: DraftComment[] };
export const EMPTY_DRAFT: Draft = { note: "", comments: [] };

/** What the operator can do to a waiting review from the composer. */
export type Action = "approve" | "decline" | "remark";

/** Whether a waiting review is one this page cannot answer, which it then never calls pending. */
function blocked(row: ReviewSummary): boolean {
  return row.state === "pending" && !row.answerable && row.unanswerable !== "";
}

/**
 * A review's state as the page names it: the relay records a declined review
 * as `rejected`, and a waiting review this page cannot answer is not called
 * pending -- the reason stands where Approve would be.
 */
export function stateLabel(row: ReviewSummary): string {
  if (blocked(row)) return "can't answer here";
  return row.state === "rejected" ? "declined" : row.state;
}

/** The class a review's state badge takes, a waiting review this page cannot answer its own. */
export function stateClass(row: ReviewSummary): string {
  return blocked(row) ? "blocked" : row.state;
}

/**
 * A path as the operator reads it beside its repository: relative to the
 * directory holding the repository's checkouts where it sits beneath it, so
 * `…/lup.git/tree/feature` reads `tree/feature`. The full path stays on the
 * element, for hover and copy.
 */
export function checkoutLabel(path: string, root: ReviewRoot | undefined): string {
  const repository = root?.repository ?? "";
  const home = repository.endsWith("/.git") ? repository.slice(0, -"/.git".length) : repository;
  if (home !== "" && path === home) return home.slice(home.lastIndexOf("/") + 1);
  if (home !== "" && path.startsWith(`${home}/`)) return path.slice(home.length + 1);
  return path.slice(path.lastIndexOf("/") + 1) || path;
}

/** What went stale, in a sentence per file. */
export function staleSentences(row: ReviewSummary): string[] {
  return row.stale.map((moved) => {
    const name = moved.path.slice(moved.path.lastIndexOf("/") + 1);
    switch (moved.cause) {
      case "created": return `${name} was created since this was recorded`;
      case "deleted": return `${name} was deleted since this was recorded`;
      case "directory": return `a directory now stands at ${name}`;
      default: return `${name} changed since this was recorded`;
    }
  });
}

/** Where the agent's own words about a call were found, said as the claim it is. */
const SOURCES: Record<Account["source"], string> = {
  description: "agent's note",
  justification: "agent's reason to leave the sandbox",
  preceding: "agent said before this call",
  doing: "session is on",
  proposal: "the proposal says",
};

/** What the agent says a call is for: never cut, and folded where it runs long. */
function Accounted({ said }: { said: Account }) {
  const long = said.text.length > 240 || said.text.split("\n").length > 3;
  const [open, setOpen] = useState(false);
  return <div className="accounted">
    <span className="account-source">{SOURCES[said.source]}</span>
    <p className={long && !open ? "folded" : undefined}>{said.text}</p>
    {long && <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? "Show less" : "Show all"}</button>}
  </div>;
}

/**
 * A command line as the policy read it: the line whole, then every command in
 * it that asks or is refused, each with its own reason, and the commands
 * allowed on their own folded beneath. A line of one command is its reason
 * already, above.
 */
function CommandPreview({ command, segments }: { command: string; segments: CommandSegment[] }) {
  const objecting = segments.filter((segment) => segment.effect !== "allow");
  const allowed = segments.filter((segment) => segment.effect === "allow");
  const row = (segment: CommandSegment, index: number) => <li key={index} className={`segment ${segment.effect}`}>
    <span className="segment-head"><span className={`file-review-state ${segment.effect}`}>{reviewLabel(segment.effect)}</span>
      {segment.rule !== "" && <code className="segment-rule">{segment.rule}</code>}</span>
    <pre>{segment.command === "" ? "the line as a whole" : segment.command}</pre>
    {segment.effect !== "allow" && <p>{segment.reason}</p>}
  </li>;
  return <section className="command" aria-label="Command">
    <pre>{command}</pre>
    {segments.length > 1 && <>
      <ol className="segments" aria-label="Commands that ask">{objecting.map(row)}</ol>
      {allowed.length > 0 && <details className="segments-allowed"><summary>{allowed.length === 1 ? "1 command" : `${allowed.length} commands`} allowed on {allowed.length === 1 ? "its" : "their"} own</summary>
        <ol className="segments">{allowed.map(row)}</ol></details>}
    </>}
  </section>;
}

/** The steps of a command no document shows: what only running them reveals, or a file that is not text. */
function UnpreviewedSteps({ steps }: { steps: UnpreviewedStep[] }) {
  return <section className="unpreviewed" aria-label="Steps no document shows">
    <h3>{steps.length === 1 ? "1 step" : `${steps.length} steps`} no document shows</h3>
    <ul>{steps.map((step, index) => <li key={index}>
      <span className={`unpreviewed-cause ${step.cause}`}>{step.cause === "run" ? "Result known only after running" : "Leaves a file that is not text"}</span>
      <pre>{step.command}</pre>
      {step.paths.length > 0 && <ul className="unpreviewed-paths" aria-label="Files it leaves so">{step.paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>}
    </li>)}</ul>
  </section>;
}

/** The whole record, mounted only once opened: a payload can run to megabytes. */
function RequestRecord({ question }: { question: ReviewDetail["question"] }) {
  const [open, setOpen] = useState(false);
  return <details className="record" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Complete request record</summary>{open && <JsonRecord value={question} />}
  </details>;
}

function RequestLink({ summary }: { summary: ReviewSummary }) {
  const [status, setStatus] = useState("");
  const url = reviewLink(summary.id, summary.root_id);
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setStatus("Link copied.");
    } catch {
      setStatus("Copy is unavailable; the link is the address of this review.");
    }
  }
  return <span className="request-link">
    <a href={url} title="Link to this request">Link</a><button type="button" onClick={() => void copy()}>Copy link</button>
    {status !== "" && <span role="status">{status}</span>}
  </span>;
}

function Commented({ comments, target }: { comments: LineComment[]; target: string }) {
  if (comments.length === 0) return null;
  return <ul className="thread-comments">{comments.map((comment, index) => <li key={index}>
    <code title={comment.path}>{comment.path.startsWith(`${target}/`) ? comment.path.slice(target.length + 1) : comment.path}:{comment.start}{comment.end !== comment.start ? `-${comment.end}` : ""}{comment.side === "before" ? " (before)" : ""}</code>
    <span>{comment.note}</span>
  </li>)}</ul>;
}

/** Everything said on the review, oldest first: remarks, the requester's replies, the answer. */
function Thread({ entries, sending, target }: { entries: ThreadEntry[]; sending: ThreadEntry | null; target: string }) {
  const shown = sending === null ? entries : [...entries, sending];
  if (shown.length === 0) return null;
  return <section className="thread" aria-label="Thread">
    <ol>{shown.map((entry, index) => <li key={index} className={`said ${entry.kind}${entry === sending ? " sending" : ""}`}>
      <span className="said-heading"><strong>{entry.kind === "reply" ? `${entry.author} replied`
        : entry.kind === "remark" ? `${entry.author} commented` : `${entry.approved ? "Approved" : "Declined"} by ${entry.author}`}</strong>
        <time dateTime={entry.at}>{new Date(entry.at).toLocaleTimeString()}</time>{entry === sending && <span className="muted"> sending…</span>}</span>
      {entry.text !== "" && <p>{entry.text}</p>}
      <Commented comments={entry.comments} target={target} />
    </li>)}</ol>
  </section>;
}

export function RequestView({ detail, row, roots, draft, sending, error, fileNavigation, composer, onDraft, onAct }: {
  detail: ReviewDetail;
  row: ReviewSummary;
  roots: ReviewRoot[];
  draft: Draft;
  sending: ThreadEntry | null;
  error: string;
  fileNavigation: RefObject<FileNavigation | null>;
  composer: RefObject<HTMLTextAreaElement | null>;
  onDraft(draft: Draft): void;
  onAct(action: Action): void;
}) {
  const { question, summary } = detail;
  const [details, setDetails] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const pending = row.state === "pending";
  const root = roots.find((each) => each.id === row.root_id);
  const target = row.target || root?.path || "";
  const stale = staleSentences(row);
  const answered = question.answer?.comments ?? [];
  const remarked = detail.thread.flatMap((entry) => entry.kind === "remark" ? entry.comments : []);
  useEffect(() => {
    if (pending) composer.current?.focus({ preventScroll: true });
    else heading.current?.focus({ preventScroll: true });
  }, []);

  return <>
    <article className="request">
      <header className="request-bar">
        <span className={`state ${stateClass(row)}`}>{stateLabel(row)}</span>
        <h2 ref={heading} tabIndex={-1} title={`${target}\n${summary.title}`}><span className="target">{checkoutLabel(target, root)}:</span> {summary.title}</h2>
        <button type="button" className="request-more" aria-expanded={details} onClick={() => setDetails((open) => !open)}>Details</button>
        <RequestLink summary={row} />
      </header>
      <p className="request-meta" aria-label="Request location">
        <span>queue <code title={root?.path ?? ""} tabIndex={0}>{root === undefined ? "checkout not watched" : checkoutLabel(root.path, root)}</code></span>
        <span>runs in <code title={question.operation.cwd} tabIndex={0}>{checkoutLabel(question.operation.cwd, root)}</code></span>
        <span>asked by <strong>{row.session || row.requester}</strong></span>
        <time dateTime={row.created}>{new Date(row.created).toLocaleString()}</time>
      </p>
      {details && <section className="request-record" aria-label="Request details">
        <dl className="metadata">
          <dt>Queue checkout</dt><dd><code>{root?.path ?? "Checkout not present in the current watch list"}</code></dd>
          <dt>Operation directory</dt><dd><code>{question.operation.cwd}</code></dd>
          <dt>Requester</dt><dd>{summary.requester}</dd>
          <dt>Operation</dt><dd>{summary.operation}</dd>
          <dt>Rule</dt><dd>{summary.rule || "Unattributed"}</dd>
          <dt>Request</dt><dd><code>{row.id}</code></dd>
          <dt>Fingerprint</dt><dd><code>{question.fingerprint}</code></dd>
        </dl>
        <h3>Tool input</h3><JsonRecord value={question.operation.payload} />
        <RequestRecord question={question} />
      </section>}
      <section className="why" aria-label="Why approval is needed">
        <h3>Why approval is needed · <code>{summary.rule || "unattributed"}</code></h3>
        <p className="reason">{summary.reason}</p>
      </section>
      {question.account.length > 0 && <section className="account" aria-label="What the agent says it is for">
        {question.account.map((said, index) => <Accounted key={index} said={said} />)}
      </section>}
      {row.state === "stale" && <p className="stale-reason" role="status">
        <strong>Retired as stale:</strong> {stale.join("; ")}. No approval could release it any more; its session was told to re-read the file and ask again.
      </p>}
      {detail.command !== null && <CommandPreview command={detail.command} segments={question.segments ?? []} />}
      {(question.unpreviewed ?? []).length > 0 && <UnpreviewedSteps steps={question.unpreviewed ?? []} />}
      {detail.preview_unavailable !== "" && <p className="notice" role="status">{detail.preview_unavailable}</p>}
      {(detail.preview_notice ?? "") !== "" && <details className="preview-note"><summary>How these documents were worked out</summary><p>{detail.preview_notice}</p></details>}
      <Thread entries={detail.thread} sending={sending} target={target} />
      {detail.notification !== null && <p className="delivery" role="status">How the requester heard: {detail.notification.detail}</p>}
      {detail.files.length > 0 ? <FileEvidence files={detail.files} navigation={fileNavigation} command={detail.command}
        comments={draft.comments} recorded={[...remarked, ...answered]} editable={pending} target={target}
        onComments={(comments) => onDraft({ ...draft, comments })} />
        : detail.command === null && <section className="command-only"><h3>Tool input</h3><JsonRecord value={question.operation.payload} /></section>}
    </article>
    {pending && <section className="composer" aria-label="Answer this review">
      {error !== "" && <p className="error" role="alert">{error}</p>}
      <label htmlFor="review-comment" className="sr-only">Note for the requesting agent</label>
      <textarea id="review-comment" ref={composer} rows={2} value={draft.note}
        onChange={(event) => onDraft({ ...draft, note: event.target.value })}
        placeholder="Note for the requesting agent: sent with your decision, or alone with Alt+Enter. Click a line number to comment on a line." />
      <div className="actions">
        {row.answerable || row.unanswerable === "" ? <>
          <button className="approve" type="button" aria-keyshortcuts="Control+Enter Meta+Enter" disabled={!row.answerable}
            title="Ctrl+Enter" onClick={(event) => { if (event.detail < 2) onAct("approve"); }}>Approve</button>
          <button className="decline" type="button" aria-keyshortcuts="Alt+Delete" disabled={!row.answerable} title="Alt+Delete"
            onClick={(event) => { if (event.detail < 2) onAct("decline"); }}>Decline</button>
        </> : <p className="unanswerable" role="status">{row.unanswerable}</p>}
        <button className="send" type="button" aria-keyshortcuts="Alt+Enter" disabled={draft.note.trim() === "" && draft.comments.length === 0}
          title="Alt+Enter: send the note and line comments without deciding" onClick={() => onAct("remark")}>Send comments</button>
        {draft.comments.length > 0 && <span className="draft-count">{draft.comments.length} line {draft.comments.length === 1 ? "comment" : "comments"} drafted</span>}
        <span className="decision-hint">Ctrl+Enter approve · Alt+Del decline · Alt+Enter send without deciding · Esc leave the box</span>
      </div>
    </section>}
  </>;
}
