import { useEffect, useMemo, useRef, useState } from "react";
import type { ReviewDecision, ReviewDetail, ReviewRoot, ReviewSnapshot, ReviewSummary, SetupPane, ThreadEntry } from "../generated/views";
import { answerReview, followDashboard, readHistory, readMessages, readReview, readReviewLink, readSetupPanes, remarkReview, reviewLink, ReviewError, takeToken, TOKEN_KEY } from "./api";
import type { FileNavigation } from "./Files";
import { applied, codeNotice, paged, type LiveState } from "./live";
import { changedElsewhere, EMPTY_DRAFT, RequestView, staleSentences, stateClass, stateLabel, type Action, type Draft } from "./Request";
import { Sessions } from "./Sessions";

type SessionGroup = { session: string; rows: ReviewSummary[] };
type RepositoryGroup = { repository: string; name: string; sessions: SessionGroup[] };

/** Rows grouped by the repository they were parked in, then by the session that asked, in the order they arrive. */
function grouped(rows: ReviewSummary[], roots: ReviewRoot[]): RepositoryGroup[] {
  const where = new Map(roots.map((root) => [root.id, root]));
  const groups: RepositoryGroup[] = [];
  for (const row of rows) {
    const root = where.get(row.root_id);
    const repository = root?.repository || root?.path || row.root_id;
    let group = groups.find((each) => each.repository === repository);
    if (group === undefined) {
      group = { repository, name: root?.repository_name || root?.path || "Checkout unavailable", sessions: [] };
      groups.push(group);
    }
    const session = row.session || row.requester || "an unknown session";
    let asking = group.sessions.find((each) => each.session === session);
    if (asking === undefined) {
      asking = { session, rows: [] };
      group.sessions.push(asking);
    }
    asking.rows.push(row);
  }
  return groups;
}

function SetupView({ panes, chosen, onChoose }: { panes: SetupPane[] | null; chosen: string; onChoose(key: string): void }) {
  const pane = panes?.find((each) => each.key === chosen) ?? panes?.[0];
  return <div className="setup-view">
    <aside className="setup-list" aria-label="Repositories">
      {panes === null ? <p className="empty" role="status">Loading repositories…</p>
        : panes.length === 0 ? <p className="empty">No repository's setup is served here.</p>
        : panes.map((each) => <button key={each.key} type="button" aria-pressed={pane?.key === each.key} title={each.repository} onClick={() => onChoose(each.key)}>{each.name}</button>)}
    </aside>
    {pane !== undefined && <iframe className="setup-frame" title={`Setup · ${pane.name}`} src={pane.path} />}
  </div>;
}

/** How many older requests one "Load older" reads: a page of History beyond what the stream carries. */
const HISTORY_PAGE = 50;

/** When a request left the queue, which History is ordered by: most recently settled first. */
function settledFirst(rows: ReviewSummary[]): ReviewSummary[] {
  const when = (row: ReviewSummary) => Date.parse(row.settled ?? row.created);
  return [...rows].sort((left, right) => when(right) - when(left));
}

/** The requests the stream carries, then the older History pages read, each once: the stream's copy wins. */
function merged(streamed: ReviewSummary[], older: ReviewSummary[]): ReviewSummary[] {
  const carried = new Set(streamed.map((row) => row.key));
  return [...streamed, ...older.filter((row) => !carried.has(row.key))];
}

function nextPending(rows: ReviewSummary[], key: string): string {
  const position = rows.findIndex((row) => row.key === key);
  return rows.find((row, index) => index > position && row.key !== key && row.state === "pending")?.key
    ?? rows.find((row) => row.key !== key && row.state === "pending")?.key ?? "";
}

/** One transient word about an action the operator took: what it was, and what came of it. */
type Toast = { id: number; key: string; status: "sending" | "done" | "failed"; heading: string; title: string; detail: string };

/** What each action is called while it is on its way, once it landed, and when it failed. */
const SPOKEN: Record<Action, { sending: string; done: string; failed: string }> = {
  approve: { sending: "Approving…", done: "Approved", failed: "Approval not recorded" },
  decline: { sending: "Declining…", done: "Declined", failed: "Decline not recorded" },
  remark: { sending: "Sending comments…", done: "Comments sent", failed: "Comments not sent" },
};

/** The keys the review view answers to, as the `?` help lists them. */
const SHORTCUTS: [string[], string][] = [
  [["Ctrl", "Enter"], "Approve"], [["Alt", "Delete"], "Decline"], [["Alt", "Enter"], "Send the note and line comments without deciding"],
  [["Alt", "↑"], "Previous request"], [["Alt", "↓"], "Next request"],
  [["Esc"], "Leave the comment box"], [["C"], "Back into the comment box"], [["J"], "Next request"], [["K"], "Previous request"],
  [["["], "Previous file"], [["]"], "Next file"], [["N"], "Next rule exception"], [["P"], "Previous rule exception"],
  [["M"], "Next `# lup:` marker"], [["Shift", "M"], "Previous `# lup:` marker"], [["F"], "Whole file in context"], [["?"], "This help"],
];

export function App() {
  const [access, setAccess] = useState(() => takeToken());
  const { token, notice } = access;
  const liveAccess = useRef(access);
  liveAccess.current = access;
  const [linked, setLinked] = useState(readReviewLink);
  const [accessDenied, setAccessDenied] = useState(false);
  const [streamed, setStreamed] = useState<ReviewSnapshot | null>(null);
  const [older, setOlder] = useState<ReviewSummary[]>([]);
  const [olderLoading, setOlderLoading] = useState(false);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState<"pending" | "history">("pending");
  const [fetched, setFetched] = useState<ReviewDetail | null>(null);
  const [connection, setConnection] = useState("Connecting…");
  const [error, setError] = useState("");
  const [failures, setFailures] = useState<Record<string, string>>({});
  const [retry, setRetry] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [answered, setAnswered] = useState<ReadonlyMap<string, ReviewDetail>>(new Map());
  const [sending, setSending] = useState<ReadonlyMap<string, ThreadEntry>>(new Map());
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [help, setHelp] = useState(false);
  const [advance, setAdvance] = useState(true);
  const [mobilePanel, setMobilePanel] = useState<"queue" | "review">("review");
  const [view, setView] = useState<"reviews" | "sessions" | "setup">("reviews");
  const [live, setLive] = useState<LiveState | null>(null);
  const liveState = useRef<LiveState | null>(null);
  const cursor = useRef("");
  const [panes, setPanes] = useState<SetupPane[] | null>(null);
  const [pane, setPane] = useState("");
  const fileNavigation = useRef<FileNavigation | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);
  const heldKeys = useRef(new Set<string>());
  const routedAddress = useRef(window.location.href);
  const inflight = useRef(new Set<string>());
  const toastSeq = useRef(0);
  const current = useRef(selected);
  const detailRequest = useRef<AbortController | null>(null);
  // Each request's detail as last read, beside the row it was read for: shown
  // at once while that row stands, and read again once the stream moves it.
  const readDetails = useRef(new Map<string, { row: ReviewSummary; detail: ReviewDetail }>());
  const searchedLinks = useRef(new Set<string>());
  current.current = selected;
  // What the page knows before the stream does: an answer given here stands
  // over the stream's row until the stream says the review is no longer
  // waiting, so an older snapshot can never undo it.
  const queue = useMemo<ReviewSnapshot | null>(() => streamed === null ? null
    : { ...streamed, reviews: streamed.reviews.map((row) => answered.get(row.key)?.summary ?? row) }, [streamed, answered]);
  const liveQueue = useRef(queue);
  liveQueue.current = queue;
  const rows = useMemo(() => merged(queue?.reviews ?? [], older), [queue, older]);
  const queuePartial = queue !== null && queue.errors.length > 0;
  const queueCurrent = queue !== null && connection === "Live" && !queuePartial;
  // Why the queue is not current, where the operator looks: the stream reconnecting, or each
  // checkout whose queue could not be read, by name and with what reading it said.
  const queueReasons = queue === null ? ["Loading review queue…"] : [
    ...(connection === "Live" ? [] : [connection === "Connecting…" ? "Reconnecting…" : connection]),
    ...queue.errors.map((issue) => `${issue.root.slice(issue.root.lastIndexOf("/") + 1) || issue.root} unavailable: ${issue.message}`),
  ];
  const queueStatus = queueReasons.join(" · ");
  /** A count as the header shows it: unknown before the first snapshot, the last one known while it refreshes. */
  const counted = (count: number) => queue === null ? "?" : queueCurrent ? `${count}` : `${count} · refreshing`;
  const pending = rows.filter((row) => row.state === "pending");
  const settled = settledFirst(rows.filter((row) => row.state !== "pending"));
  const historyTotal = Math.max(queue?.history ?? 0, settled.length);
  const groups = grouped(filter === "pending" ? pending : settled, queue?.roots ?? []);
  const visible = groups.flatMap((group) => group.sessions.flatMap((asking) => asking.rows));
  const position = visible.findIndex((row) => row.key === selected);
  const linkedRows = linked === null ? [] : rows.filter((row) => row.id === linked.id && (linked.root === null || row.root_id === linked.root));
  const row = rows.find((each) => each.key === selected) ?? null;
  const detail = answered.get(selected) ?? (fetched?.summary.key === selected ? fetched : null);

  function refreshAccess() {
    const fresh = takeToken(liveAccess.current.token);
    liveAccess.current = fresh;
    setAccess(fresh);
  }

  function reconnect() {
    refreshAccess();
    setConnection("Connecting…");
    setRetry((value) => value + 1);
  }

  function navigate(target: ReviewSummary | null, replace = false) {
    const url = new URL(window.location.href);
    url.hash = "";
    const address = target === null ? url.href : reviewLink(target.id, target.root_id);
    routedAddress.current = address;
    if (address !== window.location.href) {
      if (replace) window.history.replaceState(null, "", address);
      else window.history.pushState(null, "", address);
    }
    setLinked(target === null ? null : { id: target.id, root: target.root_id });
    setSelected(target?.key ?? "");
    current.current = target?.key ?? "";
  }

  useEffect(() => {
    function refreshed(event: StorageEvent) {
      if (event.key === TOKEN_KEY || event.key === null) refreshAccess();
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
      setConnection("Connecting…");
      setRetry((value) => value + 1);
    }
    window.addEventListener("hashchange", changed);
    window.addEventListener("popstate", changed);
    return () => { window.removeEventListener("hashchange", changed); window.removeEventListener("popstate", changed); };
  }, []);

  useEffect(() => {
    if (queue === null) return;
    if (linked !== null) {
      const found = linkedRows.length === 1 ? linkedRows[0] : undefined;
      if (found === undefined) { setSelected(""); current.current = ""; }
      else if (selected !== found.key) {
        setSelected(found.key);
        current.current = found.key;
        setFilter(found.state === "pending" ? "pending" : "history");
      }
      return;
    }
    if (selected === "" && filter === "pending") {
      const first = queue.reviews.find((item) => item.state === "pending");
      if (first !== undefined) navigate(first, true);
    }
  }, [queue, rows, filter, linked, selected]);

  // An answer given here is dropped once the stream shows the review settled,
  // which is the stream catching up with it.
  useEffect(() => {
    if (streamed === null) return;
    setAnswered((known) => {
      const kept = new Map([...known].filter(([key]) => !streamed.reviews.some((each) => each.key === key && each.state !== "pending")));
      return kept.size === known.size ? known : kept;
    });
  }, [streamed]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function connect() {
      setConnection("Connecting…");
      setAccessDenied(false);
      try {
        for await (const entry of followDashboard(token, controller.signal, cursor.current)) {
          if (controller.signal.aborted) return;
          if (entry.kind === "live") {
            setConnection("Live");
            continue;
          }
          const previous = liveState.current;
          const next = applied(previous, entry.frame);
          cursor.current = entry.frame.cursor;
          liveState.current = next;
          setLive(next);
          if (previous === null || next.reviews !== previous.reviews) setStreamed(next.reviews);
          if (entry.frame.event.type === "snapshot") setConnection("Live");
        }
        if (!controller.signal.aborted) throw new Error("The connection closed.");
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ReviewError && [401, 403].includes(failure.status)) {
          setConnection("Access denied. Open the dashboard using the operator's launch link.");
          setAccessDenied(true);
          return;
        }
        setConnection(`Reconnecting — ${String(failure)}`);
        timer = setTimeout(() => setRetry((value) => value + 1), 3000);
      }
    }
    void connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [token, retry]);

  useEffect(() => () => {
    detailRequest.current?.abort();
    detailRequest.current = null;
  }, [selected, token, retry]);

  // A request's detail is read when it is opened, and again only once the
  // stream moves its row: while the row stands, the detail read for it --
  // or read ahead of the operator reaching it -- is shown at once.
  useEffect(() => {
    if (selected === "" || connection !== "Live" || detailRequest.current !== null) return;
    const known = row === null ? undefined : readDetails.current.get(selected);
    if (known !== undefined && known.row === row) {
      setFetched(known.detail);
      return;
    }
    const controller = new AbortController();
    const readFor = row;
    detailRequest.current = controller;
    void readReview(selected, token, controller.signal).then((fresh) => {
      if (readFor !== null) readDetails.current.set(fresh.summary.key, { row: readFor, detail: fresh });
      if (!controller.signal.aborted) setFetched(fresh);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(String(failure));
    }).finally(() => {
      if (detailRequest.current === controller) detailRequest.current = null;
    });
  }, [selected, token, row, retry, connection]);

  // The next request in the list is read once the one open has arrived, so
  // moving on to it -- by hand or after an answer -- shows it at once.
  const following = position >= 0 ? visible[position + 1] : undefined;
  useEffect(() => {
    if (following === undefined || connection !== "Live" || fetched?.summary.key !== selected) return;
    const known = readDetails.current.get(following.key);
    if (known !== undefined && known.row === following) return;
    const controller = new AbortController();
    void readReview(following.key, token, controller.signal).then((fresh) => {
      readDetails.current.set(fresh.summary.key, { row: following, detail: fresh });
    }).catch(() => {
      // Opening it reads it again and says why where the operator looks.
      readDetails.current.delete(following.key);
    });
    return () => controller.abort();
  }, [following, fetched, selected, connection, token]);

  // A link to a request past the History the stream carries asks the server
  // for it once, rather than claiming it does not exist.
  useEffect(() => {
    if (linked === null || !queueCurrent || linkedRows.length > 0 || searchedLinks.current.has(linked.id)) return;
    const controller = new AbortController();
    void readHistory(0, HISTORY_PAGE, token, controller.signal, linked).then((found) => {
      searchedLinks.current.add(linked.id);
      setOlder((known) => merged(known, found.reviews));
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(String(failure));
    });
    return () => controller.abort();
  }, [linked, queueCurrent, linkedRows.length, token]);

  // One older page of a repository's messages, read back from where the ones
  // the page holds start, and folded into what the stream has moved so far.
  async function loadEarlierMessages(repository: string) {
    const before = liveState.current?.earlier.get(repository) ?? 0;
    if (before === 0) return;
    const page = await readMessages(repository, before, token);
    if (liveState.current === null) return;
    const next = paged(liveState.current, repository, before, page);
    liveState.current = next;
    setLive(next);
  }

  async function loadOlder() {
    setOlderLoading(true);
    try {
      const page = await readHistory(settled.length, HISTORY_PAGE, token);
      setOlder((known) => merged(known, page.reviews));
    } catch (failure) {
      setError(String(failure));
    } finally {
      setOlderLoading(false);
    }
  }

  useEffect(() => {
    if (view !== "setup") return;
    const controller = new AbortController();
    void readSetupPanes(token, controller.signal).then(setPanes).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(String(failure));
    });
    return () => controller.abort();
  }, [view, token]);

  function select(wanted: string) {
    navigate(rows.find((each) => each.key === wanted) ?? null);
    setMobilePanel("review");
    setError("");
  }

  function show(wanted: "pending" | "history") {
    setFilter(wanted);
    const matching = rows.filter((each) => wanted === "pending" ? each.state === "pending" : each.state !== "pending");
    if (!matching.some((each) => each.key === selected)) select(matching[0]?.key ?? "");
  }

  function move(offset: number) {
    const target = visible[position + offset];
    if (target !== undefined) select(target.key);
  }

  function toast(entry: Omit<Toast, "id">): number {
    toastSeq.current += 1;
    const id = toastSeq.current;
    setToasts((shown) => [...shown.filter((each) => each.key !== entry.key || each.status === "failed"), { ...entry, id }]);
    return id;
  }

  function settleToast(id: number, status: Toast["status"], heading: string, detail: string) {
    setToasts((shown) => shown.map((each) => each.id === id ? { ...each, status, heading, detail } : each));
    if (status === "done") setTimeout(() => setToasts((shown) => shown.filter((each) => each.id !== id)), 7000);
  }

  /**
   * Answer or comment on the selected review, at once. The page shows the
   * outcome before the server has it -- the review answered, the next one
   * open, the drafts cleared -- and reconciles with the server's reply; a
   * refusal puts everything back where it was, drafts included, and says
   * why where the operator will see it.
   */
  function act(action: Action) {
    const key = selected;
    const shown = detail;
    const summary = row;
    if (shown === null || summary === null || shown.summary.key !== key || inflight.current.has(key)) return;
    if (summary.state !== "pending" || !summary.answerable) return;
    if (action === "approve" && summary.stale.length > 0) return;
    const draft = drafts[key] ?? EMPTY_DRAFT;
    const comments = draft.comments.filter((comment) => comment.note.trim() !== "")
      .map(({ path, start, end, side, note }) => ({ path, start, end, side, note }));
    if (action === "remark" && draft.note.trim() === "" && comments.length === 0) return;
    const fingerprint = shown.question.fingerprint;
    const at = new Date().toISOString();
    inflight.current.add(key);
    setFailures((known) => Object.fromEntries(Object.entries(known).filter(([each]) => each !== key)));
    setDrafts((known) => ({ ...known, [key]: EMPTY_DRAFT }));
    const spoken = SPOKEN[action];
    const id = toast({ key, status: "sending", heading: spoken.sending, title: summary.title, detail: "" });
    if (action === "remark") {
      setSending((known) => new Map(known).set(key, { kind: "remark", author: "operator", text: draft.note, comments, at, approved: null }));
    } else {
      const answer = { approved: action === "approve", principal: "operator", receipt: "recorded" as const, unresolved_chain: false, note: draft.note, comments, at };
      const optimistic: ReviewDetail = {
        ...shown,
        summary: { ...summary, state: answer.approved ? "approved" : "rejected", answerable: false },
        question: { ...shown.question, answer },
        thread: [...shown.thread, { kind: "answer", author: "operator", text: draft.note, comments, at, approved: answer.approved }],
      };
      setAnswered((known) => new Map(known).set(key, optimistic));
      if (current.current === key && advance) {
        setFilter("pending");
        const following = liveQueue.current?.reviews.map((each) => each.key === key ? optimistic.summary : each) ?? [];
        const next = nextPending(following, key);
        navigate(following.find((each) => each.key === next) ?? null);
      }
    }
    const request: Promise<ReviewDecision> = action === "remark" ? remarkReview(key, { note: draft.note, comments, fingerprint }, token)
      : answerReview(key, { approved: action === "approve", note: draft.note, comments, fingerprint }, token);
    request.then((settled) => {
      if (action === "remark") {
        setSending((known) => { const next = new Map(known); next.delete(key); return next; });
        if (current.current === key) setFetched(settled.review);
      } else {
        setAnswered((known) => new Map(known).set(key, settled.review));
      }
      settleToast(id, "done", spoken.done, settled.notification.detail);
    }).catch((failure: unknown) => {
      const reason = failure instanceof Error ? failure.message : String(failure);
      setSending((known) => { const next = new Map(known); next.delete(key); return next; });
      setAnswered((known) => { const next = new Map(known); next.delete(key); return next; });
      setDrafts((known) => {
        const now = known[key] ?? EMPTY_DRAFT;
        return { ...known, [key]: { note: now.note === "" ? draft.note : `${draft.note}\n${now.note}`, comments: [...draft.comments, ...now.comments] } };
      });
      setFailures((known) => ({ ...known, [key]: reason }));
      settleToast(id, "failed", spoken.failed, reason);
    }).finally(() => {
      inflight.current.delete(key);
    });
  }

  useEffect(() => {
    function down(event: KeyboardEvent) {
      if (view !== "reviews" || event.defaultPrevented || event.isComposing) return;
      const code = event.code || event.key;
      const typing = event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']") !== null;
      const deciding = (event.ctrlKey || event.metaKey) && event.key === "Enter" ? "approve" as const
        : event.altKey && !event.shiftKey && event.key === "Delete" ? "decline" as const
        : event.altKey && event.key === "Enter" ? "remark" as const : null;
      if (deciding !== null) {
        event.preventDefault();
        if (event.repeat || heldKeys.current.has(code)) return;
        heldKeys.current.add(code);
        act(deciding);
        return;
      }
      if (event.altKey && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Escape" && typing && event.target instanceof HTMLElement) {
        event.target.blur();
        document.querySelector<HTMLElement>(".file-evidence, .request h2")?.focus({ preventScroll: true });
        return;
      }
      if (typing || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
      if (heldKeys.current.has(code)) return;
      const lower = event.key.toLowerCase();
      if (!["?", "j", "k", "c", "[", "]", "n", "p", "m", "f"].includes(lower)) return;
      heldKeys.current.add(code);
      event.preventDefault();
      switch (lower) {
        case "?": setHelp((value) => !value); break;
        case "j": move(1); break;
        case "k": move(-1); break;
        case "[": fileNavigation.current?.moveFile(-1); break;
        case "]": fileNavigation.current?.moveFile(1); break;
        case "n": fileNavigation.current?.moveException(1); break;
        case "p": fileNavigation.current?.moveException(-1); break;
        case "m": fileNavigation.current?.moveMarker(event.shiftKey ? -1 : 1); break;
        case "f": fileNavigation.current?.toggleWhole(); break;
        case "c": composer.current?.focus(); break;
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

  if (accessDenied) return <main className="access"><h1>Dashboard</h1>
    <p>This browser is not authorized, or its dashboard session has expired. Open the dashboard with the operator's <code>uv run lup-devtools dashboard open</code>, or the launch link <code>dashboard serve</code> printed, then return to this request link.</p>
    {linked !== null && <p>Requested review: <code>{linked.id}</code></p>}
    {notice !== "" && <p className="notice" role="alert">{notice}</p>}
    <button type="button" onClick={reconnect}>Check access again</button>
  </main>;

  return <div className="dashboard">
    <header className="masthead">
      <div className="masthead-identity"><h1>Dashboard <span className="eyebrow">Lup · operator review</span></h1>
        {queue === null ? <p className="watched-checkout">Loading watched checkout…</p> : queue.roots.length === 1 ? <p className="watched-checkout">Watching queue <code tabIndex={0}>{queue.roots[0]?.path}</code></p>
          : <details className="roots"><summary>Watching {queue.roots.length} checkout queues</summary>{queue.roots.map((root) => <p key={root.id}><code tabIndex={0}>{root.path}</code></p>)}</details>}
      </div>
      <nav className="views" aria-label="Dashboard view">
        <button type="button" aria-pressed={view === "reviews"} onClick={() => setView("reviews")}>Reviews</button>
        <button type="button" aria-pressed={view === "sessions"} onClick={() => setView("sessions")}>Sessions{live === null ? "" : ` (${[...live.sessions.values()].filter((each) => each.running && each.parent === "").length})`}</button>
        <button type="button" aria-pressed={view === "setup"} onClick={() => setView("setup")}>Setup</button>
      </nav>
      <div className="connection"><span role="status" className={queueCurrent ? "live" : "muted"}>{connection === "Live" && queuePartial ? "Some queues unavailable" : connection}</span>
        <button type="button" aria-expanded={help} aria-controls="shortcut-help" onClick={() => setHelp((value) => !value)}>Keyboard shortcuts</button>
        <button type="button" onClick={reconnect}>Reconnect</button></div>
    </header>
    {live !== null && codeNotice(live.code) !== "" && <p className="notice running-code" role="alert">{codeNotice(live.code)}</p>}
    {notice !== "" && <p className="notice" role="alert">{notice}</p>}
    {view === "sessions" ? <Sessions live={live} current={connection === "Live"} token={token} onEarlier={loadEarlierMessages} />
      : view === "setup" ? <>{error !== "" && <p className="error" role="alert">{error}</p>}<SetupView panes={panes} chosen={pane} onChoose={setPane} /></> : <>
    {help && <section className="shortcut-help" id="shortcut-help" aria-label="Keyboard shortcuts">
      {SHORTCUTS.map(([keys, meaning]) => <span key={`${keys.join("+")} ${meaning}`}>{keys.map((key, index) => <span key={key}>{index > 0 && " + "}<kbd>{key}</kbd></span>)} {meaning}</span>)}
      <p>The comment box is open on every waiting review: decisions and Alt+↑/↓ work from inside it, and Esc leaves it so the one-letter keys apply. Click a line number to comment on a line, Shift+click another to comment on the range. Holding a key cannot answer another request.</p>
    </section>}
    <nav className="mobile-switch" aria-label="Workspace panel"><button type="button" aria-pressed={mobilePanel === "queue"} onClick={() => setMobilePanel("queue")}>Queue ({counted(pending.length)})</button><button type="button" aria-pressed={mobilePanel === "review"} onClick={() => setMobilePanel("review")}>Review</button></nav>
    <div className="workspace" data-mobile-panel={mobilePanel}>
      <aside className="queue" aria-label="Review requests" aria-busy={!queueCurrent}>
        <div className="filters" aria-label="Request filter">
          <button type="button" aria-pressed={filter === "pending"} onClick={() => show("pending")}>Pending ({counted(pending.length)})</button>
          <button type="button" aria-pressed={filter === "history"} onClick={() => show("history")}>History ({counted(historyTotal)})</button>
        </div>
        {!queueCurrent && <p className="queue-state" role="status">{queueStatus}</p>}
        <label className="queue-setting"><input type="checkbox" checked={advance} onChange={(event) => setAdvance(event.target.checked)} /> Advance after decision</label>
        <div className="queue-navigation">
          <button type="button" disabled={position <= 0} onClick={() => move(-1)} aria-label="Previous request">← Previous</button>
          <span>{queue === null ? "Count unavailable" : position >= 0 ? `${position + 1} of ${visible.length}` : `${visible.length} requests`}</span>
          <button type="button" disabled={position + 1 >= visible.length} onClick={() => move(1)} aria-label="Next request">Next →</button>
        </div>
        {!queueCurrent && queue !== null && <p className="empty">Showing the requests last read; they may be incomplete until the queue is current again.</p>}
        {queueCurrent && visible.length === 0 && <p className="empty">{filter === "pending" ? "No requests waiting. This page will update when one arrives." : "No answered requests yet."}</p>}
        {groups.map((group) => <section className="repository-group" key={group.repository} aria-label={`Repository ${group.name}`}>
          <h2 className="repository-name" title={group.repository}>{group.name} <span className="count">({group.sessions.reduce((total, asking) => total + asking.rows.length, 0)})</span></h2>
          {group.sessions.map((asking) => <section className="session-group" key={asking.session} aria-label={`Session ${asking.session}`}>
            <h3 className="session-name">Asked by {asking.session} <span className="count">({asking.rows.length})</span></h3>
            {asking.rows.map((each) => <div className="queue-entry" key={each.key}><button type="button" className={`queue-row ${selected === each.key ? "selected" : ""}`}
              aria-current={selected === each.key ? "true" : undefined} onClick={() => select(each.key)}>
              <span className="row-top"><span className={`state ${stateClass(each)}`}>{stateLabel(each)}</span>{inflight.current.has(each.key) && <span className="muted">sending…</span>}<time>{new Date(each.created).toLocaleTimeString()}</time></span>
              <strong>{each.title}</strong><small>{each.requester}</small>
              {changedElsewhere(each, queue?.roots.find((root) => root.id === each.root_id)) !== "" && <small className="row-target" title={each.target}>in {changedElsewhere(each, queue?.roots.find((root) => root.id === each.root_id))}</small>}
              {each.stale.length > 0 && <small className="row-stale">Stale: {staleSentences(each).join("; ")}</small>}
              {each.unanswerable !== "" && <small className="row-unanswerable">{each.unanswerable}</small>}
              {failures[each.key] !== undefined && <small className="row-failed">{failures[each.key]}</small>}
              {each.said > 0 && <small className="row-said">{each.said} {each.said === 1 ? "comment" : "comments"} in its thread</small>}
              {each.total_files > 0 && <small className="review-file-count">{each.paths.length > 0 ? `${each.paths.length} ${each.paths.length === 1 ? "file" : "files"} to review` : "Operation review"} · {each.total_files} submitted</small>}
              <small className="root-path">Queue: {queue?.roots.find((root) => root.id === each.root_id)?.path ?? "Checkout unavailable"}</small>
            </button>{each.paths.length > 1 && <details className="queue-files"><summary>{each.paths.length} files to review</summary>{each.paths.map((path) => <code key={path}>{path}</code>)}</details>}</div>)}
          </section>)}
        </section>)}
        {filter === "history" && settled.length < historyTotal && <button type="button" className="load-older" disabled={olderLoading} onClick={() => void loadOlder()}>
          {olderLoading ? "Loading older requests…" : `Load older requests (${historyTotal - settled.length} more)`}</button>}
      </aside>
      <main className="stage">
        {queue?.errors.map((issue) => <p className="notice" role="alert" key={issue.root}>{issue.root}: {issue.message}</p>)}
        {error !== "" && <p className="error" role="alert">{error}</p>}
        {linked !== null && selected === "" ? <section className="missing-review" role="status">
          <h2>{!queueCurrent ? queuePartial ? "Requested review unavailable" : "Loading requested review…" : linkedRows.length > 1 ? "This request ID exists in multiple checkouts" : "Request not found"}</h2>
          <p>Requested review: <code>{linked.id}</code></p>
          <p>{linkedRows.length > 1 ? "Choose the intended checkout from the queue." : "This page will keep watching for that exact request in the selected repositories."}</p>
          <button type="button" onClick={() => { setFilter("pending"); select(""); }}>Show pending queue</button>
        </section> : selected === "" ? <section className="welcome"><h2>{!queueCurrent ? queueStatus : pending.length === 0 ? "Queue complete" : "Ready for the next request"}</h2><p>Keep this tab open. New requests appear automatically, with their complete changes and tool inputs.</p></section>
          : detail !== null && row !== null ? <RequestView key={selected} detail={detail} row={row} roots={queue?.roots ?? []} draft={drafts[selected] ?? EMPTY_DRAFT}
            sending={sending.get(selected) ?? null} error={failures[selected] ?? ""} fileNavigation={fileNavigation} composer={composer}
            onDraft={(draft) => setDrafts((known) => ({ ...known, [selected]: draft }))} onAct={act} />
          : <p className="empty" role="status">Loading request…</p>}
      </main>
    </div>
    <div className="toasts" role="region" aria-label="What your actions came to" aria-live="polite">
      {toasts.map((each) => <div className={`toast ${each.status}`} key={each.id} role={each.status === "failed" ? "alert" : "status"}>
        <strong>{each.heading}</strong> <span className="toast-title">{each.title}</span>
        {each.detail !== "" && <p>{each.detail}</p>}
        <span className="toast-actions">{each.status === "failed" && <button type="button" onClick={() => { setFilter("pending"); select(each.key); }}>Open it</button>}
          <button type="button" aria-label="Dismiss" onClick={() => setToasts((shown) => shown.filter((other) => other.id !== each.id))}>×</button></span>
      </div>)}
    </div>
    </>}
  </div>;
}

