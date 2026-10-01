// The page's controller: the one place that moves its state. It follows the
// dashboard's stream, reads each review's detail when it is needed and the
// next ones ahead of the operator, answers optimistically and puts everything
// back where an answer is refused, and owns where the page is — the view, what
// is selected, which window has focus, and the cursor in each pane. The keymap
// and the `:` commands call into it; React draws what it holds.
import type { KeyLine, LiveMessage, LiveSession, ReviewDecision, ReviewDetail, ReviewRoot, ReviewSummary } from "../generated/views";
import { answerReview, followDashboard, readHistory, readMessages, readReview, readReviewLink, readSetupPanes, remarkReview, reviewLink, ReviewError, sendReply, takeToken, tryKeys, writeKeys } from "./api";
import { Keymap, Sequencer, type Where } from "./keys";
import { discussions, threadBuffer, type Discussion } from "./threads";
import { applied, codeNotice, moved, NO_KEYS, paged, type LiveState } from "./live";
import { askedBy, CLOSED_UI, EMPTY_DRAFT, headOf, headShort, headText, plural, reviewBuffer, type Buffer, type Draft, type Entry, type ReviewUi } from "./review";
import { checkoutLabel } from "./review";
import { inboxBuffer, memberBuffer, memberById, memberOfReview, repoBuffer, repositoryOf, treeItems, youBuffer, type TreeItem, holdersOf, parentOf, counterpart, inboxOf } from "./supervision";
import { initialState, Store, type Float, type Notice, type PageState, type Tone, type View, type Win } from "./state";
import { unserved } from "./served";

/** What the operator can do to a waiting review. */
export type Answer = "approve" | "decline" | "remark";

/** What each answer is called while it is on its way, once it landed, and when it failed. */
const SPOKEN: Record<Answer, { sending: string; done: string; failed: string }> = {
  approve: { sending: "Approving…", done: "Approved", failed: "Approval not recorded" },
  decline: { sending: "Declining…", done: "Declined", failed: "Decline not recorded" },
  remark: { sending: "Sending comments…", done: "Comments sent", failed: "Comments not sent" },
};

/** How many older requests one page of History reads beyond what the stream carries. */
const HISTORY_PAGE = 50;

/** What the centre of the page holds. */
export type CenterKind = "review" | "member" | "you" | "repo" | "inbox" | "thread" | "empty" | "setup";

/** The elements the controller moves focus and scroll on, registered by the views that draw them. */
export type Elements = {
  panes: [HTMLElement | null, HTMLElement | null];
  queue: HTMLElement | null;
  context: HTMLElement | null;
  box: HTMLTextAreaElement | null;
  command: HTMLInputElement | null;
  finder: HTMLInputElement | null;
  setup: HTMLElement | null;
  float: HTMLElement | null;
};

function settledFirst(rows: ReviewSummary[]): ReviewSummary[] {
  const when = (row: ReviewSummary) => Date.parse(row.settled ?? row.created);
  return [...rows].sort((left, right) => when(right) - when(left));
}

function merged(streamed: ReviewSummary[], older: ReviewSummary[]): ReviewSummary[] {
  const carried = new Set(streamed.map((row) => row.key));
  return [...streamed, ...older.filter((row) => !carried.has(row.key))];
}

/** Reviews in the order the tree and History list them: by repository, then by the session that asked. */
export function grouped(rows: ReviewSummary[], roots: ReviewRoot[]): { repository: string; name: string; sessions: { session: string; rows: ReviewSummary[] }[] }[] {
  const where = new Map(roots.map((root) => [root.id, root]));
  const groups: { repository: string; name: string; sessions: { session: string; rows: ReviewSummary[] }[] }[] = [];
  for (const row of rows) {
    const root = where.get(row.root_id);
    const repository = root?.repository || root?.path || row.root_id;
    let group = groups.find((each) => each.repository === repository);
    if (group === undefined) {
      group = { repository, name: root?.repository_name || root?.path || "Checkout unavailable", sessions: [] };
      groups.push(group);
    }
    const session = askedBy(row);
    let asking = group.sessions.find((each) => each.session === session);
    if (asking === undefined) {
      asking = { session, rows: [] };
      group.sessions.push(asking);
    }
    asking.rows.push(row);
  }
  return groups;
}

let drafted = 0;
/** A fresh id for a comment drafted on this page. */
export function draftId(): string {
  drafted += 1;
  return `draft-${Date.now()}-${drafted}`;
}

/** Whether the page is drawn as the touch layout: one column below 861 px. */
export const narrowWidth = (width: number) => width <= 860;

export class Dashboard {
  readonly store: Store;
  readonly sequencer = new Sequencer();
  readonly elements: Elements = { panes: [null, null], queue: null, context: null, box: null, command: null, finder: null, setup: null, float: null };
  readonly heldKeys = new Set<string>();
  keymap = new Keymap();
  keyTimer: ReturnType<typeof setTimeout> | undefined;
  whichTimer: ReturnType<typeof setTimeout> | undefined;
  /** How the next cursor move in each pane scrolls its row into view. */
  scroll: ["nearest" | "center" | "top", "nearest" | "center" | "top"] = ["nearest", "nearest"];
  /** A focus the next render carries out, once the element it names is drawn. */
  focusing: { win: Win; caret: boolean } | null = null;
  private readonly inflight = new Set<string>();
  private readonly readFor = new Map<string, ReviewSummary>();
  private readonly searchedLinks = new Set<string>();
  private detailRequest: AbortController | null = null;
  private stream: AbortController | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private messageTimer: ReturnType<typeof setTimeout> | undefined;
  private clockTimer: ReturnType<typeof setInterval> | undefined;
  private cursor = "";
  private routedAddress = "";
  private noticeSeq = 0;
  private listeners: (() => void)[] = [];
  private buffers = new WeakMap<object, Map<string, Buffer>>();
  private landed = false;

  constructor(width = typeof window === "undefined" ? 1920 : window.innerWidth) {
    this.store = new Store(initialState(takeToken(), readReviewLink(), width));
  }

  get state(): PageState {
    return this.store.get();
  }

  set(update: Partial<PageState> | ((state: PageState) => Partial<PageState>)): void {
    this.store.set(update);
  }

  // ───────────────────────────── lifecycle ─────────────────────────────

  start(): void {
    this.routedAddress = window.location.href;
    const storage = (event: StorageEvent) => {
      if (event.key === "lup-dashboard-token" || event.key === null) this.refreshAccess();
    };
    const routed = () => this.routeChanged();
    const resized = () => this.resized();
    window.addEventListener("storage", storage);
    window.addEventListener("hashchange", routed);
    window.addEventListener("popstate", routed);
    window.addEventListener("resize", resized);
    this.listeners = [
      () => window.removeEventListener("storage", storage),
      () => window.removeEventListener("hashchange", routed),
      () => window.removeEventListener("popstate", routed),
      () => window.removeEventListener("resize", resized),
    ];
    this.clockTimer = setInterval(() => this.set({ now: Date.now() }), 15_000);
    this.connect();
  }

  stop(): void {
    for (const remove of this.listeners) remove();
    this.listeners = [];
    this.stream?.abort();
    this.stream = null;
    this.detailRequest?.abort();
    this.detailRequest = null;
    clearTimeout(this.retryTimer);
    clearTimeout(this.messageTimer);
    clearTimeout(this.keyTimer);
    clearTimeout(this.whichTimer);
    clearInterval(this.clockTimer);
  }

  private resized(): void {
    const width = window.innerWidth;
    const wide = width >= 1280;
    const narrow = narrowWidth(width);
    this.set((state) => ({
      narrow,
      wide,
      ...(state.wide !== wide ? { contextShown: wide, queueShown: wide || !state.contextShown ? true : state.queueShown } : {}),
      ...(narrow ? {} : { touch: { drawer: "", sheet: "" } }),
    }));
  }

  // ───────────────────────────── the stream ─────────────────────────────

  private refreshAccess(): void {
    this.set((state) => ({ access: takeToken(state.access.token) }));
  }

  reconnect(): void {
    this.refreshAccess();
    this.connect();
  }

  private routeChanged(): void {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    if (fragment.has("token")) {
      this.refreshAccess();
      if (!fragment.has("review")) window.history.replaceState(null, "", this.routedAddress);
    }
    if (this.routedAddress === window.location.href) return;
    this.routedAddress = window.location.href;
    this.set({ linked: readReviewLink(), sel: { kind: "", key: "" } });
    this.landed = false;
    this.connect();
  }

  /** Follow the stream from where this tab last stood, and keep following it, reconnecting on a drop. */
  connect(): void {
    this.stream?.abort();
    clearTimeout(this.retryTimer);
    const controller = new AbortController();
    this.stream = controller;
    this.set({ connection: "Connecting…", denied: false });
    void (async () => {
      try {
        for await (const entry of followDashboard(this.state.access.token, controller.signal, this.cursor)) {
          if (controller.signal.aborted) return;
          if (entry.kind === "live") {
            this.set({ connection: "Live" });
            this.settle();
            continue;
          }
          this.apply(entry.frame);
          if (entry.frame.event.type === "snapshot") this.set({ connection: "Live" });
          this.settle();
        }
        if (!controller.signal.aborted) throw new Error("The connection closed.");
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof ReviewError && [401, 403].includes(failure.status)) {
          this.set({ connection: "Access denied. Open the dashboard using the operator's launch link.", denied: true });
          return;
        }
        this.set({ connection: `Reconnecting — ${String(failure)}` });
        this.retryTimer = setTimeout(() => this.connect(), 3000);
      }
    })();
  }

  private apply(frame: Parameters<typeof applied>[1]): void {
    const previous = this.state.live;
    const next = applied(previous, frame);
    this.cursor = frame.cursor;
    const lines = moved(previous, frame).map((line) => ({ at: new Date().toISOString(), ...line }));
    const event = frame.event;
    this.set((state) => ({ live: next, log: [...state.log, ...lines].slice(-400) }));
    if (previous === null || next.keys !== previous.keys) this.keysArrived(previous === null);
    if (previous === null || next.code !== previous.code) this.codeNotices(next);
    if (event.type === "message" && event.message.recipient === "user" && event.message.waiting && previous !== null && !previous.messages.has(event.message.key)) {
      const sender = memberById(next, event.message.repository, event.message.sender);
      this.notify(`${sender?.name || event.message.sender || event.message.door} wrote to you`, "", "gi opens your inbox.", "info");
    }
    // An answer given here stands over the stream's row until the stream says the review is no longer waiting.
    if (event.type === "snapshot" || event.type === "review") {
      this.set((state) => {
        const kept = new Map([...state.answered].filter(([key]) => !next.reviews.reviews.some((row) => row.key === key && row.state !== "pending")));
        return kept.size === state.answered.size ? {} : { answered: kept };
      });
    }
  }

  /** After a frame: land on a review on first load, follow a link, and read what the open review needs. */
  private settle(): void {
    const state = this.state;
    if (state.live === null) return;
    if (state.linked !== null) {
      this.followLink();
    } else if (!this.landed && state.connection === "Live") {
      this.landed = true;
      const first = this.pending()[0];
      if (first !== undefined) this.openReview(first.key, { mode: "box", replace: true });
      else this.fill();
    }
    this.readDetails();
  }

  private followLink(): void {
    const { linked } = this.state;
    if (linked === null) return;
    const found = this.rows().filter((row) => row.id === linked.id && (linked.root === null || row.root_id === linked.root));
    const only = found.length === 1 ? found[0] : undefined;
    if (only !== undefined) {
      if (this.state.sel.kind !== "review" || this.state.sel.key !== only.key) this.openReview(only.key, { mode: "box", replace: true, keepLink: true });
      return;
    }
    if (found.length > 0 || !this.queueCurrent() || this.searchedLinks.has(linked.id)) return;
    // A link to a request past the History the stream carries asks the server for it once.
    this.searchedLinks.add(linked.id);
    void readHistory(0, HISTORY_PAGE, this.state.access.token, undefined, linked).then((page) => {
      this.set((state) => ({ older: merged(state.older, page.reviews) }));
      this.settle();
    }).catch((failure: unknown) => this.say(String(failure), "err"));
  }

  /** Whether what is shown is current: the stream live and every queue read. */
  queueCurrent(state = this.state): boolean {
    return state.live !== null && state.connection === "Live" && state.live.reviews.errors.length === 0;
  }

  /** A count as the page shows it: unknown before the first snapshot, the last one known while it refreshes. */
  counted(count: number, state = this.state): string {
    if (state.live === null) return "?";
    return this.queueCurrent(state) ? count.toLocaleString("en") : `${count.toLocaleString("en")} · refreshing`;
  }

  // ───────────────────────────── reviews ─────────────────────────────

  roots(state = this.state): ReviewRoot[] {
    return state.live?.reviews.roots ?? [];
  }

  /** Every review the page holds: the stream's, with answers given here over them, then the older History pages read. */
  rows(state = this.state): ReviewSummary[] {
    const streamed = (state.live?.reviews.reviews ?? []).map((row) => state.answered.get(row.key)?.summary ?? row);
    return merged(streamed, state.older);
  }

  pending(state = this.state): ReviewSummary[] {
    return this.rows(state).filter((row) => row.state === "pending");
  }

  settled(state = this.state): ReviewSummary[] {
    return settledFirst(this.rows(state).filter((row) => row.state !== "pending"));
  }

  historyTotal(state = this.state): number {
    return Math.max(state.live?.reviews.history ?? 0, this.settled(state).length);
  }

  /** The reviews `j`/`k` walk: those waiting, in the tree's order, or History's. */
  walked(state = this.state): ReviewSummary[] {
    const rows = state.view === "history" ? this.settled(state) : this.pending(state);
    return grouped(rows, this.roots(state)).flatMap((group) => group.sessions.flatMap((asking) => asking.rows));
  }

  entry(key: string, state = this.state): Entry | null {
    const row = this.rows(state).find((each) => each.key === key);
    if (row === undefined) return null;
    const detail = state.answered.get(key) ?? state.fetched.get(key) ?? null;
    return { row, detail: detail?.summary.key === key ? detail : null };
  }

  /** The review open in the editor, where one is. */
  current(state = this.state): Entry | null {
    return state.sel.kind === "review" ? this.entry(state.sel.key, state) : null;
  }

  draft(key: string, state = this.state): Draft {
    return state.drafts[key] ?? EMPTY_DRAFT;
  }

  ui(key: string, state = this.state): ReviewUi {
    return state.ui[key] ?? CLOSED_UI;
  }

  setUi(key: string, change: (ui: ReviewUi) => Partial<ReviewUi>): void {
    this.set((state) => {
      const ui = state.ui[key] ?? CLOSED_UI;
      return { ui: { ...state.ui, [key]: { ...ui, ...change(ui) } } };
    });
  }

  setDraft(key: string, change: (draft: Draft) => Partial<Draft>): void {
    this.set((state) => {
      const draft = state.drafts[key] ?? EMPTY_DRAFT;
      return { drafts: { ...state.drafts, [key]: { ...draft, ...change(draft) } } };
    });
  }

  /**
   * Read the open review's detail, and then the ones the operator will reach
   * next: every review waiting, so the tree names each in plain terms and
   * moving on shows it at once. A detail is read again only once the stream
   * moves its row.
   */
  private readDetails(): void {
    const state = this.state;
    if (state.connection !== "Live" || this.detailRequest !== null) return;
    const open = state.sel.kind === "review" ? state.sel.key : "";
    const wanted = [open, ...this.walked(state).map((row) => row.key)].filter((key) => key !== "");
    const rows = this.rows(state);
    const stale = wanted.find((key) => {
      const row = rows.find((each) => each.key === key);
      return row !== undefined && (this.readFor.get(key) !== row || !state.fetched.has(key));
    });
    if (stale === undefined) return;
    const row = rows.find((each) => each.key === stale);
    const controller = new AbortController();
    this.detailRequest = controller;
    void readReview(stale, state.access.token, controller.signal).then((fresh) => {
      if (row !== undefined) this.readFor.set(stale, row);
      this.set((now) => ({ fetched: new Map(now.fetched).set(fresh.summary.key, fresh) }));
    }).catch((failure: unknown) => {
      if (row !== undefined) this.readFor.set(stale, row);
      if (!controller.signal.aborted && stale === this.state.sel.key) this.say(String(failure), "err");
    }).finally(() => {
      if (this.detailRequest === controller) this.detailRequest = null;
      if (!controller.signal.aborted) this.readDetails();
    });
  }

  async loadOlder(): Promise<void> {
    if (this.state.view !== "history") this.setView("history");
    if (this.settled().length >= this.historyTotal()) {
      this.say("every request that left the queue is here");
      return;
    }
    this.set({ olderLoading: true });
    try {
      const page = await readHistory(this.settled().length, HISTORY_PAGE, this.state.access.token);
      this.set((state) => ({ older: merged(state.older, page.reviews) }));
      this.say(`loaded ${plural(page.reviews.length, "older request")}`);
    } catch (failure) {
      this.say(String(failure), "err");
    } finally {
      this.set({ olderLoading: false });
    }
  }

  /** One older page of a repository's messages, folded into what the stream moved so far. */
  async loadEarlier(repository: string): Promise<void> {
    const live = this.state.live;
    const before = live?.earlier.get(repository) ?? 0;
    if (repository === "" || before === 0) {
      this.say("every message of this repository is here");
      return;
    }
    try {
      const page = await readMessages(repository, before, this.state.access.token);
      const now = this.state.live;
      if (now === null) return;
      this.set({ live: paged(now, repository, before, page) });
      this.say(`loaded ${plural(page.messages.length, "earlier message")}${page.earlier === 0 ? "; that is the start of the record" : ""}`);
    } catch (failure) {
      this.say(failure instanceof Error ? failure.message : String(failure), "err");
    }
  }

  async loadSetup(): Promise<void> {
    try {
      const panes = await readSetupPanes(this.state.access.token);
      this.set({ panes });
    } catch (failure) {
      this.say(String(failure), "err");
    }
  }

  // ───────────────────────────── answering ─────────────────────────────

  /**
   * Answer or comment on the open review, at once. The page shows the outcome
   * before the server has it — the review answered, the next one open, the
   * drafts cleared — and reconciles with the server's reply; a refusal puts
   * everything back where it was, drafts included, and says why where the
   * operator will see it.
   */
  answer(action: Answer): void {
    const state = this.state;
    const entry = this.current(state);
    if (entry === null) {
      if (action === "remark") this.sendBox();
      else this.say("an answer goes on an open review; open one first (j, or Space Space)");
      return;
    }
    const { row, detail } = entry;
    const key = row.key;
    if (detail === null) { this.say("this review is still being read; answer it once it shows"); return; }
    if (this.inflight.has(key)) { this.say("already sending this answer"); return; }
    if (row.state !== "pending") { this.say(`this review is ${row.state === "rejected" ? "declined" : row.state}; it takes no answer`); return; }
    if (!row.answerable && action !== "remark") { this.say(row.unanswerable || "nobody here may answer it", "err"); return; }
    if (action === "approve" && row.stale.length > 0) { this.say("a stale review cannot be approved", "err"); return; }
    const draft = this.draft(key, state);
    const comments = draft.comments.filter((comment) => comment.note.trim() !== "").map(({ path, start, end, side, note }) => ({ path, start, end, side, note }));
    if (action === "remark" && draft.note.trim() === "" && comments.length === 0) { this.say("nothing to send: write a note or a line comment first"); return; }
    const fingerprint = detail.question.fingerprint;
    const at = new Date().toISOString();
    this.inflight.add(key);
    const spoken = SPOKEN[action];
    const id = this.notify(spoken.sending, headText(headOf(entry)), "", "info", { key });
    this.set((now) => {
      const failures = Object.fromEntries(Object.entries(now.failures).filter(([each]) => each !== key));
      return { failures, drafts: { ...now.drafts, [key]: EMPTY_DRAFT }, editing: null };
    });
    if (action === "remark") {
      this.set((now) => ({ sending: new Map(now.sending).set(key, { kind: "remark", author: "operator", text: draft.note, comments, at, approved: null }) }));
    } else {
      const answer = { approved: action === "approve", principal: "operator", receipt: "recorded" as const, unresolved_chain: false, note: draft.note, comments, at };
      const optimistic: ReviewDetail = {
        ...detail,
        summary: { ...row, state: answer.approved ? "approved" : "rejected", answerable: false, settled: at },
        question: { ...detail.question, answer },
        thread: [...detail.thread, { kind: "answer", author: "operator", text: draft.note, comments, at, approved: answer.approved }],
      };
      const order = this.walked(this.state).map((each) => each.key);
      this.set((now) => ({ answered: new Map(now.answered).set(key, optimistic) }));
      if (this.state.sel.key === key && this.state.settings.advance) this.advance(key, order);
    }
    const request: Promise<ReviewDecision> = action === "remark"
      ? remarkReview(key, { note: draft.note, comments, fingerprint }, state.access.token)
      : answerReview(key, { approved: action === "approve", note: draft.note, comments, fingerprint }, state.access.token);
    request.then((decided) => {
      if (action === "remark") {
        this.set((now) => {
          const sending = new Map(now.sending);
          sending.delete(key);
          return { sending, fetched: new Map(now.fetched).set(key, decided.review) };
        });
      } else {
        this.set((now) => ({ answered: new Map(now.answered).set(key, decided.review) }));
      }
      this.settleNotice(id, "ok", spoken.done, decided.notification.detail);
    }).catch((failure: unknown) => {
      const reason = failure instanceof Error ? failure.message : String(failure);
      this.set((now) => {
        const sending = new Map(now.sending);
        sending.delete(key);
        const answered = new Map(now.answered);
        answered.delete(key);
        const kept = now.drafts[key] ?? EMPTY_DRAFT;
        return {
          sending,
          answered,
          drafts: { ...now.drafts, [key]: { note: kept.note === "" ? draft.note : `${draft.note}\n${kept.note}`, comments: [...draft.comments, ...kept.comments] } },
          failures: { ...now.failures, [key]: reason },
        };
      });
      this.settleNotice(id, "err", spoken.failed, reason, key);
    }).finally(() => {
      this.inflight.delete(key);
    });
  }

  /** After an answer: the next review waiting, landing in its box, or the tree when none is left. */
  private advance(answered: string, order: string[]): void {
    const waiting = new Set(this.pending().map((row) => row.key));
    const position = order.indexOf(answered);
    const nextKey = order.find((key, index) => index > position && waiting.has(key)) ?? order.find((key) => waiting.has(key));
    const next = nextKey === undefined ? undefined : { key: nextKey };
    if (next !== undefined) this.openReview(next.key, { mode: "box" });
    else {
      this.set({ view: "supervise", sel: { kind: "", key: "" } });
      this.fill();
    }
  }

  isInflight(key: string): boolean {
    return this.inflight.has(key);
  }

  // ───────────────────────────── the operator's messages ─────────────────────────────

  /** What the box under the editor writes to, beside an agent, a repository, or a message in the inbox. */
  boxTarget(state = this.state): { kind: "member"; session: LiveSession; key: string } | { kind: "repo"; repository: string; key: string } | { kind: "thread"; discussion: Discussion; key: string } | { kind: "none" } {
    const live = state.live;
    if (live === null) return { kind: "none" };
    switch (this.centerKind(state)) {
      case "thread": {
        const discussion = this.discussion(state);
        return discussion === undefined ? { kind: "none" } : { kind: "thread", discussion, key: `thread:${discussion.key}` };
      }
      case "member": {
        const session = live.sessions.get(state.sel.key);
        return session === undefined ? { kind: "none" } : { kind: "member", session, key: session.key };
      }
      case "repo": return { kind: "repo", repository: state.sel.key, key: `repo:${state.sel.key}` };
      case "inbox": {
        const row = this.buffer(0, state).rows[state.editor[0].cur];
        const message = row?.t === "mail" ? row.m : inboxOf(live)[0];
        const session = message === undefined ? undefined : counterpart(live, message);
        return session === undefined ? { kind: "none" } : { kind: "member", session, key: session.key };
      }
      default: return { kind: "none" };
    }
  }

  /** Send what the box holds, the way the message route does, and say what came of it in the server's words. */
  sendBox(): void {
    const target = this.boxTarget();
    if (target.kind === "none") { this.say("choose an agent, a repository or a message to write to"); return; }
    const text = this.state.replyDrafts[target.key] ?? "";
    if (target.kind === "repo") { void this.broadcast(target.repository, text); return; }
    if (target.kind === "thread") { this.post(target.discussion, text); return; }
    void this.sendTo(target.session, text);
  }

  async sendTo(session: LiveSession, text: string): Promise<boolean> {
    if (text.trim() === "") { this.say("write something first: c opens the box"); return false; }
    if (!session.running) { this.say(`${session.name || session.id} has stopped; nothing would read a message to it`, "err"); return false; }
    const key = session.key;
    this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: "Sending…", error: false } } }));
    try {
      const outcome = await sendReply(session.repository, session.id, text, this.state.access.token);
      this.set((state) => ({ replyDrafts: { ...state.replyDrafts, [key]: "" }, replyOutcome: { ...state.replyOutcome, [key]: { text: outcome.detail, error: false } } }));
      this.set((state) => ({ log: [...state.log, { at: new Date().toISOString(), repository: session.repository, text: `you → ${session.name || session.id}: ${outcome.detail}` }] }));
      return true;
    } catch (failure) {
      const reason = failure instanceof Error ? failure.message : String(failure);
      this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: reason, error: true } } }));
      return false;
    }
  }

  /**
   * A post into a discussion reaches everyone in it, as one send replying to
   * its last post or the one `r` chose. That is a route the dashboard's server
   * does not serve yet (decision 116), so the draft stays and the refusal names
   * the route; one message per member through today's route would split the
   * post and drop the thread.
   */
  post(discussion: Discussion, text: string): void {
    if (text.trim() === "") { this.say("write something first: c opens the box"); return; }
    const refused = unserved("thread-post");
    if (refused !== "") { this.say(`posting into “${discussion.title}” ${refused}`, "err"); return; }
  }

  /** One message to every working member of a repository: today, one send each. */
  async broadcast(repository: string, text: string): Promise<void> {
    const live = this.state.live;
    if (live === null) return;
    if (text.trim() === "") { this.say("write something first: c opens the box"); return; }
    const working = [...live.sessions.values()].filter((each) => each.repository === repository && each.running);
    const key = `repo:${repository}`;
    this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: `Sending to ${plural(working.length, "working member")}…`, error: false } } }));
    const sent = await Promise.all(working.map((session) => sendReply(session.repository, session.id, text, this.state.access.token).then(() => true, () => false)));
    const delivered = sent.filter(Boolean).length;
    this.set((state) => ({
      replyDrafts: delivered === working.length ? { ...state.replyDrafts, [key]: "" } : state.replyDrafts,
      replyOutcome: { ...state.replyOutcome, [key]: { text: `Sent to ${delivered} of ${plural(working.length, "working member")}.`, error: delivered !== working.length } },
    }));
  }

  // ───────────────────────────── where the page is ─────────────────────────────

  centerKind(state = this.state): CenterKind {
    if (state.view === "setup") return "setup";
    if (state.view === "inbox") return "inbox";
    if (state.view === "threads") return state.sel.kind === "thread" && this.discussion(state) !== undefined ? "thread" : "empty";
    if (state.sel.kind === "review") return this.entry(state.sel.key, state) === null ? "empty" : "review";
    if (state.sel.kind === "member" && state.live?.sessions.has(state.sel.key) !== true) return "empty";
    if ((state.sel.kind === "repo" || state.sel.kind === "you") && state.live?.repositories.has(state.sel.key) !== true) return "empty";
    if (state.sel.kind === "thread") return "empty";
    return state.sel.kind === "" ? "empty" : state.sel.kind;
  }

  /** Where a binding acts: what the centre holds, and whether the buffer has focus. */
  where(state = this.state): Where {
    return { places: [this.centerKind(state)], buffer: state.focus === "editor" && state.view !== "setup" };
  }

  private discussed: { messages: unknown; sessions: unknown; found: Discussion[] } | null = null;

  /** Every discussion the mail the page holds adds up to, worked out once per change to the mail or the roster. */
  discussions(state = this.state): Discussion[] {
    const live = state.live;
    if (live === null) return [];
    if (this.discussed?.messages !== live.messages || this.discussed.sessions !== live.sessions) {
      this.discussed = { messages: live.messages, sessions: live.sessions, found: discussions(live) };
    }
    return this.discussed.found;
  }

  /** The discussion open in Threads. */
  discussion(state = this.state): Discussion | undefined {
    return state.sel.kind === "thread" ? this.discussions(state).find((each) => each.key === state.sel.key) : undefined;
  }

  /** What the page's mode block says. */
  mode(state = this.state): "normal" | "insert" | "visual" | "command" | "find" {
    if (state.cmdline !== null) return "command";
    if (state.float?.kind === "finder") return "find";
    if (state.visual !== null) return "visual";
    if (state.typing) return "insert";
    return "normal";
  }

  /** One pane's buffer: the open review's rows, or the agent, your row, the repository or the inbox. */
  buffer(pane: 0 | 1, state = this.state): Buffer {
    const live = state.live;
    const empty: Buffer = { rows: [], index: new Map() };
    if (live === null) return empty;
    const kind = this.centerKind(state);
    const view = state.editor[pane].view;
    switch (kind) {
      case "review": {
        const entry = this.current(state);
        if (entry === null || entry.detail === null) return empty;
        const detail = entry.detail;
        const ui = this.ui(entry.row.key, state);
        const drafts = this.draft(entry.row.key, state).comments;
        const cacheKey = `${view}`;
        const byDetail = this.buffers.get(detail) ?? new Map<string, Buffer>();
        this.buffers.set(detail, byDetail);
        const stamp = `${cacheKey}|${pane}`;
        const cached = byDetail.get(stamp) as (Buffer & { made?: unknown[] }) | undefined;
        const made = [ui, drafts, live.sessions, entry.row];
        if (cached?.made !== undefined && cached.made.every((each, index) => each === made[index])) return cached;
        const root = this.roots(state).find((each) => each.id === entry.row.root_id);
        const built: Buffer & { made?: unknown[] } = reviewBuffer({
          entry: { row: entry.row, detail }, view, ui, drafts,
          holders: (path) => holdersOf(live, path),
          runsIn: checkoutLabel(detail.question.operation.cwd, root),
        });
        built.made = made;
        byDetail.set(stamp, built);
        return built;
      }
      case "member": {
        const session = live.sessions.get(state.sel.key);
        return session === undefined ? empty : this.cached(session, live.messages, live.earlier, () => memberBuffer(live, session, state.now));
      }
      case "you": {
        const repository = live.repositories.get(state.sel.key);
        return repository === undefined ? empty : this.cached(repository, live.messages, live.earlier, () => youBuffer(live, repository));
      }
      case "repo": {
        const repository = live.repositories.get(state.sel.key);
        if (repository === undefined) return empty;
        const waiting = this.pending(state).filter((row) => repositoryOf(live, this.roots(state), row.root_id) === repository.key).length;
        return this.cached(repository, live.messages, state.log, () => repoBuffer(live, repository, waiting, state.log), `${live.sessions.size}:${waiting}`);
      }
      case "inbox": return this.cached(live.messages, live.repositories, live.earlier, () => inboxBuffer(live));
      case "thread": {
        const discussion = this.discussion(state);
        return discussion === undefined ? empty : this.cached(discussion, live.sessions, live.repositories, () => threadBuffer(live, discussion));
      }
      default: return empty;
    }
  }

  private cached(owner: object, first: unknown, second: unknown, build: () => Buffer, extra = ""): Buffer {
    const byOwner = this.buffers.get(owner) ?? new Map<string, Buffer>();
    this.buffers.set(owner, byOwner);
    const held = byOwner.get("buffer") as (Buffer & { made?: unknown[] }) | undefined;
    const made = [first, second, extra, this.state.now];
    if (held?.made !== undefined && held.made.every((each, index) => each === made[index])) return held;
    const built: Buffer & { made?: unknown[] } = build();
    built.made = made;
    byOwner.set("buffer", built);
    return built;
  }

  tree(state = this.state): TreeItem[] {
    if (state.live === null) return [];
    return treeItems(state.live, this.roots(state), this.pending(state), {
      filter: state.tree, collapsed: state.collapsed, showStopped: state.showStopped, stoppedOpen: state.stoppedOpen, now: state.now,
    });
  }

  /** What the page selected becomes on the address bar, so a link reopens it. */
  private route(row: ReviewSummary | null, replace: boolean): void {
    const url = new URL(window.location.href);
    url.hash = "";
    const address = row === null ? url.href : reviewLink(row.id, row.root_id);
    this.routedAddress = address;
    if (address !== window.location.href) {
      if (replace) window.history.replaceState(null, "", address);
      else window.history.pushState(null, "", address);
    }
  }

  /**
   * Open a review. `box` lands in its note box — armed while the note is empty,
   * so `j`/`k` move on until the first letter typed — except on a touch screen,
   * whose keyboard would cover the diff; `keep` keeps whether the operator was
   * typing; `queue` keeps focus in the tree.
   */
  openReview(key: string, options: { mode?: "box" | "normal" | "keep" | "queue"; replace?: boolean; keepLink?: boolean } = {}): void {
    const row = this.rows().find((each) => each.key === key);
    if (row === undefined) return;
    const typing = this.state.typing;
    const view: View = row.state === "pending" ? "supervise" : "history";
    this.route(row, options.replace ?? false);
    this.set((state) => ({
      view,
      sel: { kind: "review", key },
      linked: options.keepLink === true ? state.linked : null,
      visual: null, editing: null, ctxCur: -1, judgedAt: -1, exceptionAt: -1, markerAt: -1,
      editor: [{ ...state.editor[0], cur: 0, want: 0 }, { ...state.editor[1], cur: 0, want: 0 }],
      touch: { drawer: "", sheet: "" },
    }));
    this.syncTree();
    this.landOnJudged();
    const box = options.mode === "box" || (options.mode === "keep" && typing);
    if (box && row.state === "pending" && !this.state.narrow) {
      this.focusWin("composer");
      this.set({ armed: this.draft(key).note === "" });
    } else if (options.mode === "queue") this.focusWin("queue");
    else this.focusWin("editor");
    this.readDetails();
  }

  /** Put the cursor on the first part the policy asked about, once the review's rows are read. */
  landOnJudged(): void {
    const rows = this.buffer(0).rows;
    const first = rows.findIndex((row) => row.jg === true);
    this.scroll[0] = "top";
    this.set((state) => ({ editor: [{ ...state.editor[0], cur: Math.max(0, first) }, state.editor[1]] }));
  }

  /** Open an agent, your own row or a repository's page beside the tree. */
  openOther(kind: "member" | "you" | "repo", key: string, mode: "normal" | "box" | "queue" = "normal"): void {
    this.set((state) => ({
      view: "supervise", sel: { kind, key }, visual: null, editing: null, ctxCur: -1, split: "", pane: 0,
      editor: [{ ...state.editor[0], cur: 0, want: 0, view: "diff" }, state.editor[1]], linked: null, touch: { drawer: "", sheet: "" },
    }));
    if (this.state.view === "supervise") this.route(null, true);
    this.syncTree();
    if (mode === "queue") this.focusWin("queue");
    else if (mode === "box") this.focusWin("composer");
    else this.focusWin("editor");
  }

  /** Fill an empty centre: the newest review waiting, else a working agent. */
  fill(): void {
    const first = this.pending()[0];
    if (first !== undefined) { this.openReview(first.key, { mode: "normal" }); return; }
    const member = [...(this.state.live?.sessions.values() ?? [])].find((each) => each.running);
    if (member !== undefined) this.openOther("member", member.key);
  }

  setView(view: View): void {
    this.set({ view, visual: null, touch: { drawer: "", sheet: "" } });
    const state = this.state;
    switch (view) {
      case "supervise": {
        const open = this.current(state);
        if (state.sel.kind === "" || (open !== null && open.row.state !== "pending") || this.centerKind(state) === "empty") this.fill();
        else this.focusWin("editor");
        return;
      }
      case "history": {
        const open = this.current(state);
        if (open === null || open.row.state === "pending") {
          const first = this.settled(state)[0];
          if (first !== undefined) this.openReview(first.key, { mode: "normal" });
          else this.set({ sel: { kind: "", key: "" } });
        }
        this.focusWin("editor");
        return;
      }
      case "inbox": {
        this.set((now) => ({ editor: [{ ...now.editor[0], cur: 0, want: 0, view: "diff" }, now.editor[1]], split: "", pane: 0 }));
        const rows = this.buffer(0).rows;
        const first = rows.findIndex((row) => row.t === "mail");
        this.set((now) => ({ editor: [{ ...now.editor[0], cur: Math.max(0, first) }, now.editor[1]] }));
        this.focusWin("editor");
        return;
      }
      case "threads": {
        const kept = this.discussion(state)?.key ?? this.discussions(state)[0]?.key;
        if (kept !== undefined) this.openThread(kept, "normal");
        else this.focusWin("editor");
        return;
      }
      case "setup":
        if (state.panes === null) void this.loadSetup();
        this.focusWin("setup");
    }
  }

  /** Open a discussion whole, in Threads: `queue` keeps focus in the list, `box` goes to the post box. */
  openThread(key: string, mode: "normal" | "box" | "queue" = "normal"): void {
    this.set((state) => ({
      view: "threads", sel: { kind: "thread", key }, visual: null, editing: null, ctxCur: -1, split: "", pane: 0, threadReply: "",
      editor: [{ ...state.editor[0], cur: 0, want: 0, view: "diff" }, state.editor[1]], linked: null, touch: { drawer: "", sheet: "" },
    }));
    this.route(null, true);
    if (mode === "queue") this.focusWin("queue");
    else if (mode === "box") this.focusWin("composer");
    else this.focusWin("editor");
  }

  /** The next or previous discussion in the list, opening it where focus is. */
  moveThread(delta: number): void {
    const all = this.discussions();
    if (all.length === 0) { this.say("no discussion yet: nobody has written to anybody"); return; }
    const at = all.findIndex((each) => each.key === this.state.sel.key);
    const next = all[Math.min(Math.max(0, (at < 0 ? (delta > 0 ? -1 : all.length) : at) + delta), all.length - 1)];
    if (next !== undefined) this.openThread(next.key, this.state.focus === "queue" ? "queue" : "normal");
  }

  /** Keep the tree's cursor on what is selected. */
  syncTree(): void {
    const items = this.tree();
    const sel = this.state.sel;
    const found = items.findIndex((item) => item.t === sel.kind && item.key === sel.key);
    if (found >= 0) this.set({ treeCur: found });
  }

  /** Move focus to a window. Entering the box yourself never arms it; only landing on a review does. */
  focusWin(win: Win, caret = true): void {
    this.focusing = { win, caret };
    this.set((state) => ({ focus: win, armed: false, ctxCur: win === "context" && state.ctxCur < 0 ? 0 : state.ctxCur }));
  }

  /**
   * Carry out the focus the last move asked for, now that its element is
   * drawn, and read back whether the page is typing: a window without the
   * system's focus moves its own focus without a `focusin` to say so.
   */
  applyFocus(): void {
    const asked = this.focusing;
    if (asked === null) return;
    this.focusing = null;
    this.moveFocus(asked);
    const active = document.activeElement;
    const typing = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement;
    if (this.state.typing !== typing) this.set({ typing });
  }

  private moveFocus(asked: { win: Win; caret: boolean }): void {
    const elements = this.elements;
    switch (asked.win) {
      case "composer": {
        const box = elements.box;
        if (box === null) {
          const kind = this.centerKind();
          this.set({ focus: "editor" });
          elements.panes[this.state.pane]?.focus({ preventScroll: true });
          if (kind === "you") this.say("you write through your verbs: :msg, :broadcast (Space p)");
          else if (kind === "review" && this.current()?.row.state !== "pending") this.say("this review is settled; it takes no note");
          return;
        }
        box.focus({ preventScroll: true });
        // The caret goes to the end, so a draft you come back to carries on where it stopped.
        if (asked.caret) box.setSelectionRange(box.value.length, box.value.length);
        return;
      }
      case "editor": elements.panes[this.state.pane]?.focus({ preventScroll: true }); return;
      case "queue": elements.queue?.focus({ preventScroll: true }); return;
      case "context": elements.context?.focus({ preventScroll: true }); return;
      case "setup": elements.setup?.focus({ preventScroll: true }); return;
      case "setupframe": return;
    }
  }

  // ───────────────────────────── what the page says ─────────────────────────────

  /** Say something on the command line's line, and keep it in `:messages`. */
  say(text: string, tone: Tone = ""): void {
    clearTimeout(this.messageTimer);
    const at = new Date().toLocaleTimeString([], { hour12: false });
    this.set((state) => ({ message: { text, tone }, said: text === "" ? state.said : [...state.said, `${at} ${text}`] }));
    if (text !== "") this.messageTimer = setTimeout(() => { if (this.state.message.text === text) this.set({ message: { text: "", tone: "" } }); }, 9000);
  }

  /** A notice float: an action's outcome closes on its own, a refusal or word from the dashboard stands until dismissed. */
  notify(heading: string, title: string, detail: string, tone: Tone = "info", options: { key?: string; sticky?: boolean } = {}): number {
    this.noticeSeq += 1;
    const id = this.noticeSeq;
    const key = options.key ?? "";
    const sticky = options.sticky ?? false;
    const at = new Date().toLocaleTimeString([], { hour12: false });
    this.set((state) => ({
      notes: [...state.notes.filter((note) => !(key !== "" && note.key === key && note.tone !== "err")), { id, heading, title, detail, tone, key, sticky, open: false, failed: "" }],
      said: [...state.said, `${at} ${heading}${title !== "" ? ` · ${title}` : ""}${detail !== "" ? ` — ${detail}` : ""}`],
    }));
    if (!sticky && (tone !== "info" || key === "")) setTimeout(() => this.dismiss(id), tone === "info" ? 6000 : 8000);
    return id;
  }

  private settleNotice(id: number, tone: Tone, heading: string, detail: string, failed = ""): void {
    const at = new Date().toLocaleTimeString([], { hour12: false });
    this.set((state) => {
      const note = state.notes.find((each) => each.id === id);
      return {
        notes: state.notes.map((each) => each.id === id ? { ...each, tone, heading, detail, failed } : each),
        said: [...state.said, `${at} ${heading}${note !== undefined && note.title !== "" ? ` · ${note.title}` : ""}${detail !== "" ? ` — ${detail}` : ""}`],
      };
    });
    if (tone === "ok") setTimeout(() => this.dismiss(id), 7000);
  }

  dismiss(id: number): void {
    this.set((state) => ({ notes: state.notes.filter((note) => note.id !== id) }));
  }

  /** The dashboard's own word on the code it runs: sticky while it stands, gone once the server stops saying it. */
  private codeNotices(live: LiveState): void {
    this.set((state) => ({ notes: state.notes.filter((note) => note.key !== "code") }));
    const said = codeNotice(live.code);
    if (said === "") return;
    const heading = live.code.older ? "dashboard code" : "dashboard restarted";
    this.notify(heading, "", said, live.code.failing !== "" ? "err" : "warn", { key: "code", sticky: true });
  }

  // ───────────────────────────── the person's keys ─────────────────────────────

  /** The keymap rebuilt from the keys the stream says, with this tab's tried lines over them. */
  private keysArrived(first: boolean): void {
    if (this.state.keyLines.length > 0) {
      void this.tryLines(this.state.keyLines);
      return;
    }
    const keys = this.state.live?.keys ?? NO_KEYS;
    this.keymap = new Keymap(keys.changed);
    this.set({ tried: null });
    const applied = keys.report.applied.length;
    const refused = keys.report.refused.length;
    if (keys.unread !== "") this.notify("Your keys", "your lup config could not be read", `${keys.unread} lup's keys stand.`, "warn", { key: "keys", sticky: true });
    else if (applied > 0 || refused > 0) {
      this.notify("Your keys", `${plural(applied, "binding")} from ${keys.source}${refused > 0 ? `, ${refused} refused` : ""}`, ":map lists each one, and why a refused one did not apply.", refused > 0 ? "warn" : "info", { key: "keys", sticky: refused > 0 });
    } else if (!first) this.notify("Your keys", "lup's own keys", "Your [dashboard.keys] changes nothing now.", "info", { key: "keys" });
  }

  /** Check a tab's lines over the person's keys, as the server checks a config, and bind what applies. */
  async tryLines(lines: KeyLine[]): Promise<{ refused: string }> {
    try {
      const tried = await tryKeys(lines, this.state.access.token);
      this.keymap = new Keymap(tried.changed);
      this.set({ keyLines: lines, tried });
      return { refused: "" };
    } catch (failure) {
      return { refused: failure instanceof Error ? failure.message : String(failure) };
    }
  }

  async writeLines(): Promise<void> {
    const lines = this.state.keyLines;
    if (lines.length === 0) { this.say("nothing tried in this tab: :map {action} {keys} first"); return; }
    try {
      const written = await writeKeys(lines, this.state.access.token);
      this.say(`wrote ${plural(lines.length, "line")} to [dashboard.keys] in ${written.source || "your lup config"}; its comments are kept`);
      this.set({ keyLines: [], tried: null });
    } catch (failure) {
      this.say(failure instanceof Error ? failure.message : String(failure), "err");
    }
  }

  /** The person's keys as this tab holds them: the server's word, or what this tab tried over it. */
  keys(state = this.state) {
    return state.tried ?? state.live?.keys ?? NO_KEYS;
  }

  // ───────────────────────────── small reads the views share ─────────────────────────────

  /** The member a review's asker is, where the roster has it. */
  asker(row: ReviewSummary, state = this.state): LiveSession | undefined {
    return state.live === null ? undefined : memberOfReview(state.live, this.roots(state), row);
  }

  parentName(row: ReviewSummary, state = this.state): string {
    const asker = this.asker(row, state);
    if (asker === undefined || state.live === null) return "";
    const parent = parentOf(state.live, asker);
    return parent?.name ?? "";
  }

  /** A review's head in short form, from its detail where the page read it. */
  short(row: ReviewSummary, state = this.state): string {
    const detail = state.answered.get(row.key) ?? state.fetched.get(row.key) ?? null;
    return headShort(headOf({ row, detail }));
  }

  floatOpen(kind: Float["kind"]): boolean {
    return this.state.float?.kind === kind;
  }

  message(state: PageState, key: string): LiveMessage | undefined {
    return state.live?.messages.get(key);
  }

  notices(): Notice[] {
    return this.state.notes;
  }
}
