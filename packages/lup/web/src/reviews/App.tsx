import { memo, useEffect, useRef, useState, type RefObject } from "react";
import type { ReviewDecision, ReviewDetail, ReviewInbox, ReviewSummary } from "../generated/views";
import { answerReview, followInbox, readReview, readReviewLink, reviewLink, ReviewError, takeToken } from "./api";
import { Files, type FileNavigation } from "./Files";

const FileEvidence = memo(Files);
const JsonRecord = memo(function JsonRecord({ value }: { value: unknown }) {
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
});

function RequestRecord({ question }: { question: ReviewDetail["question"] }) {
  const [open, setOpen] = useState(false);
  return <details className="record" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Complete request record</summary>{open && <JsonRecord value={question} />}
  </details>;
}

function ToolInput({ detail }: { detail: ReviewDetail }) {
  const [open, setOpen] = useState(false);
  return <details className="request-evidence" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{detail.command !== null ? "Command and tool input" : "Complete tool input"}</summary>
    {open && <>
      {detail.command !== null && <section className="command"><h3>Command</h3><pre>{detail.command}</pre></section>}
      <div className="tool-input"><JsonRecord value={detail.question.operation.payload} /></div>
    </>}
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
      setStatus("Copy is unavailable. Copy the request link directly.");
    }
  }
  return <div className="request-link">
    <a href={url}>Link to this request</a><button type="button" onClick={() => void copy()}>Copy link</button>
    {status !== "" && <span role="status">{status}</span>}
  </div>;
}

function RequestDetails({ detail, note, sending, fileNavigation, onNote, onAnswer }: {
  detail: ReviewDetail;
  note: string;
  sending: boolean;
  fileNavigation: RefObject<FileNavigation | null>;
  onNote(note: string): void;
  onAnswer(approved: boolean): Promise<void>;
}) {
  const { summary, question } = detail;
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.scrollIntoView({ block: "start" });
    heading.current?.focus({ preventScroll: true });
  }, []);

  return <article className="request">
    <div className="request-inspection">
    {detail.notification !== undefined && detail.notification !== null && <details className="request-context notification-status"><summary>Agent notification · {detail.notification.woken ? "accepted by runtime" : detail.notification.queued ? "queued" : "unconfirmed"}</summary><p>{detail.notification.detail}</p></details>}
    <header className="request-heading">
      <div className="request-title"><span className={`state ${summary.state}`}>{summary.state}</span>
      <h2 ref={heading} tabIndex={-1}>{summary.title}</h2><RequestLink summary={summary} /></div>
      <details className="request-context"><summary>Why approval is needed · {summary.rule || "Request details"}</summary>
      <p className="reason">{summary.reason}</p>
      <dl className="metadata">
        <dt>Requester</dt><dd>{summary.requester}</dd>
        <dt>Operation</dt><dd>{summary.operation}</dd>
        <dt>Created</dt><dd>{new Date(summary.created).toLocaleString()}</dd>
        <dt>Rule</dt><dd>{summary.rule || "Unattributed"}</dd>
        <dt>Request</dt><dd><code>{summary.id}</code></dd>
      </dl>
      <RequestRecord question={question} />
      </details>
    </header>
    {detail.stale_reason !== "" && <p className="notice" role="status">{detail.stale_reason}</p>}
    <ToolInput detail={detail} />
    {detail.preview_unavailable !== "" && <p className="notice" role="status">{detail.preview_unavailable}</p>}
    {(detail.preview_notice ?? "") !== "" && <details className="preview-note"><summary>Preview computed in inbox environment</summary><p>{detail.preview_notice}</p></details>}
    {detail.files.length > 0 ? <FileEvidence files={detail.files} navigation={fileNavigation} command={detail.command} /> : <section className="command-only"><h3>Tool input</h3><JsonRecord value={question.operation.payload} /></section>}
    {question.answer !== null && <section className="answer-record">
      <h3>{question.answer.approved ? "Approved" : "Rejected"} by {question.answer.principal}</h3>
      {question.answer.note !== "" && <p>{question.answer.note}</p>}
    </section>}
    </div>
    {summary.state === "pending" && <section className="decision">
      <details className="comment-editor"><summary>Comment{note !== "" ? " · draft" : " (optional)"}</summary>
      <label htmlFor="review-comment">Comment for the requesting agent</label>
      <textarea id="review-comment" rows={2} value={note} disabled={sending}
        onChange={(event) => onNote(event.target.value)} placeholder="Optional instructions or reason" />
      </details>
      {!summary.answerable && <p className="notice">This request cannot be answered from this inbox.</p>}
      <div className="actions">
        <button className="approve" type="button" aria-keyshortcuts="Shift+A"
          disabled={sending || !summary.answerable || detail.stale_reason !== ""}
          onClick={(event) => { if (event.detail < 2) void onAnswer(true); }}>Approve</button>
        <button className="reject" type="button" aria-keyshortcuts="Shift+R" disabled={sending || !summary.answerable}
          onClick={(event) => { if (event.detail < 2) void onAnswer(false); }}>Reject</button>
        <span className="decision-hint">{sending ? "Recording decision…" : "Shift+A approve · Shift+R reject"}</span>
      </div>
    </section>}
  </article>;
}

function nextPending(rows: ReviewSummary[], key: string): string {
  const position = rows.findIndex((row) => row.key === key);
  return rows.find((row, index) => index > position && row.key !== key && row.state === "pending")?.key
    ?? rows.find((row) => row.key !== key && row.state === "pending")?.key ?? "";
}

export function App() {
  const [access, setAccess] = useState(() => takeToken());
  const { token, notice } = access;
  const liveAccess = useRef(access);
  liveAccess.current = access;
  const [linked, setLinked] = useState(readReviewLink);
  const [accessDenied, setAccessDenied] = useState(false);
  const [inbox, setInbox] = useState<ReviewInbox | null>(null);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState<"pending" | "history">("pending");
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [error, setError] = useState("");
  const [decision, setDecision] = useState<ReviewDecision | null>(null);
  const [retry, setRetry] = useState(0);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [sending, setSending] = useState(false);
  const [help, setHelp] = useState(false);
  const [advance, setAdvance] = useState(true);
  const [mobilePanel, setMobilePanel] = useState<"queue" | "review">("review");
  const fileNavigation = useRef<FileNavigation | null>(null);
  const heldKeys = useRef(new Set<string>());
  const routedAddress = useRef(window.location.href);
  const answering = useRef(false);
  const current = useRef(selected);
  const liveInbox = useRef(inbox);
  const settledReviews = useRef(new Map<string, ReviewDetail>());
  current.current = selected;
  liveInbox.current = inbox;
  const rows = inbox?.reviews ?? [];
  const pending = rows.filter((row) => row.state === "pending");
  const visible = rows.filter((row) => filter === "pending" ? row.state === "pending" : row.state !== "pending");
  const position = visible.findIndex((row) => row.key === selected);
  const linkedRows = linked === null ? [] : rows.filter((row) => row.id === linked.id && (linked.root === null || row.root_id === linked.root));

  function refreshAccess() {
    const fresh = takeToken(liveAccess.current.token);
    liveAccess.current = fresh;
    setAccess(fresh);
  }

  function navigate(row: ReviewSummary | null, replace = false) {
    const url = new URL(window.location.href);
    url.hash = "";
    const address = row === null ? url.href : reviewLink(row.id, row.root_id);
    routedAddress.current = address;
    if (address !== window.location.href) {
      if (replace) window.history.replaceState(null, "", address);
      else window.history.pushState(null, "", address);
    }
    setLinked(row === null ? null : { id: row.id, root: row.root_id });
    setSelected(row?.key ?? "");
    current.current = row?.key ?? "";
  }

  useEffect(() => {
    function refreshed(event: StorageEvent) {
      if (event.key === "lup-review-token" || event.key === null) refreshAccess();
    }
    window.addEventListener("storage", refreshed);
    return () => window.removeEventListener("storage", refreshed);
  }, []);

  useEffect(() => {
    function changed() {
      const fragment = new URLSearchParams(window.location.hash.slice(1));
      if (fragment.has("token")) {
        refreshAccess();
        if (!fragment.has("review")) window.history.replaceState(null, "", routedAddress.current);
      }
      if (routedAddress.current === window.location.href) return;
      routedAddress.current = window.location.href;
      setLinked(readReviewLink());
      setSelected("");
      current.current = "";
      setError("");
      setDecision(null);
    }
    window.addEventListener("hashchange", changed);
    window.addEventListener("popstate", changed);
    return () => { window.removeEventListener("hashchange", changed); window.removeEventListener("popstate", changed); };
  }, []);

  useEffect(() => {
    if (inbox === null) return;
    if (linked !== null) {
      const row = linkedRows.length === 1 ? linkedRows[0] : undefined;
      if (row === undefined) { setSelected(""); current.current = ""; }
      else if (selected !== row.key) {
        setSelected(row.key);
        current.current = row.key;
        setFilter(row.state === "pending" ? "pending" : "history");
      }
      return;
    }
    if (selected === "" && filter === "pending") {
      const row = inbox.reviews.find((item) => item.state === "pending");
      if (row !== undefined) navigate(row, true);
    }
  }, [inbox, filter, linked, selected]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function connect() {
      setConnection("Connecting…");
      setAccessDenied(false);
      try {
        for await (const snapshot of followInbox(token, controller.signal)) {
          if (controller.signal.aborted) return;
          for (const key of settledReviews.current.keys()) {
            if (!snapshot.reviews.some((row) => row.key === key && row.state === "pending")) settledReviews.current.delete(key);
          }
          const fresh = { ...snapshot, reviews: snapshot.reviews.map((row) => settledReviews.current.get(row.key)?.summary ?? row) };
          liveInbox.current = fresh;
          setInbox(fresh);
          setConnection("Live");
        }
        if (!controller.signal.aborted) throw new Error("The connection closed.");
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ReviewError && [401, 403].includes(failure.status)) {
          setConnection("Access denied. Open the inbox using the operator's launch link.");
          setAccessDenied(true);
          return;
        }
        setConnection(`Reconnecting — ${String(failure)}`);
        timer = setTimeout(() => void connect(), 3000);
      }
    }
    void connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [token, retry]);

  useEffect(() => {
    if (selected === "") return;
    const controller = new AbortController();
    void readReview(selected, token, controller.signal).then((fresh) => {
      if (!controller.signal.aborted && !answering.current) setDetail(settledReviews.current.get(selected) ?? fresh);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(String(failure));
    });
    return () => controller.abort();
  }, [selected, token, inbox]);

  function select(wanted: string) {
    if (answering.current) return;
    navigate(rows.find((row) => row.key === wanted) ?? null);
    setMobilePanel("review");
    setError("");
    setDecision(null);
  }

  function show(wanted: "pending" | "history") {
    if (answering.current) return;
    setFilter(wanted);
    const matching = rows.filter((row) => wanted === "pending" ? row.state === "pending" : row.state !== "pending");
    if (!matching.some((row) => row.key === selected)) select(matching[0]?.key ?? "");
  }

  function move(offset: number) {
    const target = visible[position + offset];
    if (target !== undefined) select(target.key);
  }

  async function answer(approved: boolean): Promise<void> {
    if (answering.current || detail === null || detail.summary.key !== selected
      || detail.summary.state !== "pending" || !detail.summary.answerable || (approved && detail.stale_reason !== "")) return;
    const key = selected;
    const fingerprint = detail.question.fingerprint;
    answering.current = true;
    setSending(true);
    setError("");
    try {
      const settled = await answerReview(key, { approved, note: notes[key] ?? "", fingerprint }, token);
      settledReviews.current.set(key, settled.review);
      setNotes((drafts) => ({ ...drafts, [key]: "" }));
      if (current.current === key) {
        setDetail(settled.review);
        setDecision(settled);
      }
      let refreshed = liveInbox.current;
      if (refreshed !== null) refreshed = { ...refreshed, reviews: refreshed.reviews.map((row) => row.key === key ? settled.review.summary : row) };
      liveInbox.current = refreshed;
      setInbox(refreshed);
      if (current.current === key && advance) {
        setFilter("pending");
        const next = nextPending(refreshed?.reviews ?? [], key);
        navigate(refreshed?.reviews.find((row) => row.key === next) ?? null);
      }
    } catch (failure) {
      setError(String(failure));
    } finally {
      answering.current = false;
      setSending(false);
    }
  }

  useEffect(() => {
    function down(event: KeyboardEvent) {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']") !== null) return;
      const code = event.code || event.key;
      if (heldKeys.current.has(code)) return;
      const key = event.key.toLowerCase();
      if (!(key === "?" || key === "j" || key === "k" || key === "c" || key === "[" || key === "]" || key === "n" || key === "p" || (event.shiftKey && (key === "a" || key === "r")))) return;
      heldKeys.current.add(code);
      event.preventDefault();
      if (key === "?") setHelp((value) => !value);
      else if (event.shiftKey && key === "a") void answer(true);
      else if (event.shiftKey && key === "r") void answer(false);
      else if (key === "j") move(1);
      else if (key === "k") move(-1);
      else if (!answering.current && key === "[") fileNavigation.current?.moveFile(-1);
      else if (!answering.current && key === "]") fileNavigation.current?.moveFile(1);
      else if (!answering.current && key === "n") fileNavigation.current?.moveException(1);
      else if (!answering.current && key === "p") fileNavigation.current?.moveException(-1);
      else if (key === "c") {
        const field = document.getElementById("review-comment");
        const disclosure = field?.closest("details");
        if (disclosure instanceof HTMLDetailsElement) disclosure.open = true;
        field?.focus();
      }
    }
    function up(event: KeyboardEvent) { heldKeys.current.delete(event.code || event.key); }
    function blur() { heldKeys.current.clear(); }
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  });

  if (accessDenied) return <main className="access"><h1>Review inbox</h1>
    <p>This browser is not authorized, or its review session has expired. Open the launch link printed by the operator's review inbox command, then return to this request link.</p>
    {linked !== null && <p>Requested review: <code>{linked.id}</code></p>}
    {notice !== "" && <p className="notice" role="alert">{notice}</p>}
    <button type="button" onClick={() => { refreshAccess(); setRetry((value) => value + 1); }}>Check access again</button>
  </main>;

  return <div className="inbox">
    <header className="masthead">
      <div><p className="eyebrow">Lup · operator review</p><h1>Review inbox</h1></div>
      <div className="connection"><span role="status" className={connection === "Live" ? "live" : "muted"}>{connection}</span>
        <button type="button" aria-expanded={help} aria-controls="shortcut-help" onClick={() => setHelp((value) => !value)}>Keyboard shortcuts</button>
        <button type="button" onClick={() => { refreshAccess(); setRetry((value) => value + 1); }}>Reconnect</button></div>
    </header>
    {notice !== "" && <p className="notice" role="alert">{notice}</p>}
    {help && <section className="shortcut-help" id="shortcut-help" aria-label="Keyboard shortcuts">
      <span><kbd>Shift</kbd> + <kbd>A</kbd> Approve</span><span><kbd>Shift</kbd> + <kbd>R</kbd> Reject</span>
      <span><kbd>J</kbd> Next request</span><span><kbd>K</kbd> Previous request</span><span><kbd>C</kbd> Comment</span><span><kbd>?</kbd> Toggle help</span>
      <span><kbd>[</kbd> / <kbd>]</kbd> Previous / next file</span><span><kbd>P</kbd> / <kbd>N</kbd> Previous / next rule exception</span>
      <p>Shortcuts pause while typing. Release the keys before deciding another request.</p>
    </section>}
    <nav className="mobile-switch" aria-label="Workspace panel"><button type="button" aria-pressed={mobilePanel === "queue"} onClick={() => setMobilePanel("queue")}>Queue ({pending.length})</button><button type="button" aria-pressed={mobilePanel === "review"} onClick={() => setMobilePanel("review")}>Review</button></nav>
    <div className="workspace" data-mobile-panel={mobilePanel}>
      <aside className="queue" aria-label="Review requests">
        <div className="filters" aria-label="Request filter">
          <button type="button" disabled={sending} aria-pressed={filter === "pending"} onClick={() => show("pending")}>Pending ({pending.length})</button>
          <button type="button" disabled={sending} aria-pressed={filter === "history"} onClick={() => show("history")}>History ({rows.length - pending.length})</button>
        </div>
        <label className="queue-setting"><input type="checkbox" checked={advance} disabled={sending} onChange={(event) => setAdvance(event.target.checked)} /> Advance after decision</label>
        <div className="queue-navigation">
          <button type="button" disabled={sending || position <= 0} onClick={() => move(-1)} aria-label="Previous request">← Previous</button>
          <span>{position >= 0 ? `${position + 1} of ${visible.length}` : `${visible.length} requests`}</span>
          <button type="button" disabled={sending || position + 1 >= visible.length} onClick={() => move(1)} aria-label="Next request">Next →</button>
        </div>
        {inbox !== null && visible.length === 0 && <p className="empty">{filter === "pending" ? "No requests waiting. This page will update when one arrives." : "No answered requests yet."}</p>}
        {visible.map((row) => <div className="queue-entry" key={row.key}><button type="button" disabled={sending} className={`queue-row ${selected === row.key ? "selected" : ""}`}
          aria-current={selected === row.key ? "true" : undefined} onClick={() => select(row.key)}>
          <span className="row-top"><span className={`state ${row.state}`}>{row.state}</span><time>{new Date(row.created).toLocaleTimeString()}</time></span>
          <strong>{row.title}</strong><small>{row.requester}</small>
          {row.total_files > 0 && <small className="review-file-count">{row.paths.length > 0 ? `${row.paths.length} ${row.paths.length === 1 ? "file" : "files"} to review` : "Operation review"} · {row.total_files} submitted</small>}
          <small className="root-path">{inbox?.roots.find((root) => root.id === row.root_id)?.path}</small>
        </button>{row.paths.length > 1 && <details className="queue-files"><summary>{row.paths.length} files to review</summary>{row.paths.map((path) => <code key={path}>{path}</code>)}</details>}</div>)}
        <details className="roots"><summary>Watched checkouts ({inbox?.roots.length ?? 0})</summary>
          {inbox?.roots.map((root) => <p key={root.id}><code>{root.path}</code></p>)}</details>
      </aside>
      <main className="stage">
        {inbox?.errors.map((issue) => <p className="notice" role="alert" key={issue.root}>{issue.root}: {issue.message}</p>)}
        {error !== "" && <p className="error" role="alert">{error}</p>}
        {decision !== null && <div className="decision-receipt" role="status">
          <strong>{decision.review.question.answer?.approved ? "Approval recorded." : "Rejection recorded."}</strong>
          <span> {decision.review.summary.title}</span><p>{decision.notification.detail}</p>
        </div>}
        {linked !== null && selected === "" ? <section className="missing-review" role="status">
          <h2>{inbox === null ? "Loading requested review…" : linkedRows.length > 1 ? "This request ID exists in multiple checkouts" : "Request not found"}</h2>
          <p>Requested review: <code>{linked.id}</code></p>
          <p>{linkedRows.length > 1 ? "Choose the intended checkout from the queue." : "This page will keep watching for that exact request in the selected repositories."}</p>
          <button type="button" disabled={sending} onClick={() => { setFilter("pending"); select(""); }}>Show pending queue</button>
        </section> : selected === "" ? <section className="welcome"><h2>{pending.length === 0 ? "Queue complete" : "Ready for the next request"}</h2><p>Keep this tab open. New requests appear automatically, with their complete changes and tool inputs.</p></section>
          : detail?.summary.key === selected ? <RequestDetails key={selected} detail={detail} note={notes[selected] ?? ""} sending={sending} fileNavigation={fileNavigation}
            onNote={(note) => setNotes((drafts) => ({ ...drafts, [selected]: note }))} onAnswer={answer} />
          : <p className="empty" role="status">Loading request…</p>}
      </main>
    </div>
  </div>;
}
