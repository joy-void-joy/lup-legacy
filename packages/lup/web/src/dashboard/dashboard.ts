// The page's controller: the one place that moves its state. It follows the
// dashboard's stream, reads each review's detail when it is needed and the
// next ones ahead of the operator, answers optimistically and puts everything
// back where an answer is refused, and owns where the page is — the view, what
// is selected, which window has focus, and the cursor in each pane. The keymap
// and the `:` commands call into it; React draws what it holds.
import type { AgentBudgetRequest, KeyLine, LiveMessage, LiveSession, ReviewDecision, ReviewDetail, ReviewRoot, ReviewSummary } from "../generated/views";
import {
  answerReview, broadcastTo, describeYou, followDashboard, followTranscripts, holdPath, pauseAt, postInto, postNotice, readHistory, readInbox, readMessages, readReview, readReviewLink,
  readSetupPanes, readTranscript, releasePath, remarkReview, renameAgent, resumeAt, reviewLink, ReviewError, sendReply, setTurtle, settleBudget, stopAgent, switchProfile, takeToken, tryKeys,
  wakeAgent, withdrawNotice, writeKeys, type Reach, type Sending,
} from "./api";
import { Keymap, Sequencer, type Where } from "./keys";
import { discussions, threadBuffer, type Discussion } from "./threads";
import { applied, codeNotice, moved, NO_KEYS, paged, type LiveState } from "./live";
import { askedBy, CLOSED_UI, EMPTY_DRAFT, headOf, headShort, headText, plural, reviewBuffer, type Buffer, type Draft, type Entry, type ReviewUi } from "./review";
import { checkoutLabel } from "./review";
import { capsText, fullest, inboxBuffer, memberBuffer, memberById, memberOfReview, repoBuffer, repositoryOf, treeItems, youBuffer, type TreeItem, holdersOf, parentOf, counterpart, inboxOf } from "./supervision";
import { initialState, Store, type Float, type Notice, type PageState, type Tone, type View, type Win } from "./state";
import { unserved, type Feature } from "./served";

/** What an interrupt says where the operator wrote nothing of their own. */
export const INTERRUPTING = "The person watching interrupts your turn: stop what you are doing, read your mailbox, and say where you are with `coordination_describe` before carrying on.";

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
  private followTimer: ReturnType<typeof setTimeout> | undefined;
  private stopArmedAt = 0;
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
    clearTimeout(this.followTimer);
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
    if (previous === null || next.budget !== previous.budget) this.budgetNotices(previous, next);
    // A followed transcript is not state: what it recorded since joins the open one, from where that ends.
    if (event.type === "transcript") {
      this.set((state) => {
        const shown = state.transcript;
        if (shown === null || shown.session !== event.session) return {};
        const fresh = event.entries.filter((entry) => entry.at >= shown.end);
        return fresh.length === 0 ? {} : { transcript: { ...shown, entries: [...shown.entries, ...fresh], end: Math.max(shown.end, event.end) } };
      });
    }
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
    if (target.kind === "thread") { void this.post(target.discussion, text); return; }
    void this.sendTo(target.session, text, { in_reply_to: this.state.replyTo[target.key] ?? "" });
  }

  /**
   * Write to one agent the way the message route does, and say what came of it in the server's words.
   * *sending* names the post it answers, whether it redirects the agent's next call, and whether it
   * interrupts the turn; each is refused, the draft kept, where this dashboard's server does not serve it.
   */
  async sendTo(session: LiveSession, text: string, sending: Sending = {}): Promise<boolean> {
    if (text.trim() === "") { this.say("write something first: c opens the box"); return false; }
    if (!session.running) { this.say(`${session.name || session.id} has stopped; nothing would read a message to it`, "err"); return false; }
    const needed: Feature[] = [
      ...((sending.in_reply_to ?? "") !== "" ? ["reply-thread" as const] : []),
      ...(sending.redirect === true ? ["redirect" as const] : []),
      ...(sending.priority === "now" ? ["interrupt" as const] : []),
    ];
    const refused = needed.map((feature) => this.lacks(feature)).find((reason) => reason !== "");
    if (refused !== undefined) { this.told(`writing to ${session.name || session.id}`, refused, "err"); return false; }
    const key = session.key;
    this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: "Sending…", error: false } } }));
    try {
      const outcome = await sendReply(session.repository, session.id, text, this.state.access.token, sending);
      this.set((state) => ({
        replyDrafts: { ...state.replyDrafts, [key]: "" },
        replyTo: { ...state.replyTo, [key]: "" },
        replyOutcome: { ...state.replyOutcome, [key]: { text: outcome.detail, error: false } },
        log: [...state.log, { at: new Date().toISOString(), repository: session.repository, text: `you → ${session.name || session.id}: ${outcome.detail}` }],
      }));
      this.told(`to ${session.name || session.id}`, outcome.detail, "ok");
      return true;
    } catch (failure) {
      const reason = failure instanceof Error ? failure.message : String(failure);
      this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: reason, error: true } } }));
      this.told(`to ${session.name || session.id}`, reason, "err");
      return false;
    }
  }

  /**
   * What came of a supervising action, as a notice: an outcome closes on its own, and a refusal
   * stands until dismissed, the way an answer's does — on a phone as on a desktop.
   */
  private told(heading: string, detail: string, tone: Tone): void {
    this.notify(heading, "", detail, tone, { sticky: tone === "err" });
  }

  /** What this dashboard's server says it serves of supervising, from the stream's whole state. */
  served(): readonly Feature[] {
    return this.state.live?.served ?? [];
  }

  /** Why an action needing *feature* cannot run against this server, or nothing where it can. */
  lacks(feature: Feature): string {
    return unserved(this.served(), feature);
  }

  /**
   * One supervising write: refused naming its route where the server does not serve it, else sent,
   * its answer said in the server's words and its refusal as the server gave it.
   */
  private async supervise<Reply>(feature: Feature | null, doing: string, write: () => Promise<Reply>, said: (reply: Reply) => string): Promise<Reply | null> {
    const refused = feature === null ? "" : this.lacks(feature);
    if (refused !== "") { this.told(doing, refused, "err"); return null; }
    try {
      const reply = await write();
      this.told(said(reply), "", "ok");
      return reply;
    } catch (failure) {
      this.told(doing, failure instanceof Error ? failure.message : String(failure), "err");
      return null;
    }
  }

  /** Make an agent look: whatever waits for it, or a line saying the person asked it to. */
  async wake(session: LiveSession): Promise<void> {
    const named = session.name || session.id;
    await this.supervise("bare-wake", `waking ${named}`, () => wakeAgent(session.repository, session.id, this.state.access.token), (outcome) => `${named}: ${outcome.detail}`);
  }

  /** Interrupt an agent's turn with these words, or the standard ones where none are given. */
  async interrupt(session: LiveSession, text: string): Promise<boolean> {
    return this.sendTo(session, text.trim() !== "" ? text : INTERRUPTING, { priority: "now" });
  }

  /** What an agent is called from now on. */
  async rename(session: LiveSession, name: string): Promise<void> {
    if (name.trim() === "") { this.say("E: :rename [agent] <name>", "err"); return; }
    const named = session.name || session.id;
    await this.supervise("rename", `renaming ${named}`, () => renameAgent(session.repository, session.id, name.trim(), this.state.access.token), (renamed) => `${named} is ${renamed.name} from now on`);
  }

  /**
   * Stop an agent's runtime. Asked twice: the first asks for the second, saying which process it
   * would signal, and only a second within ten seconds sends it; *confirmed* is `:stop!`, which is the second.
   */
  async stopRuntime(session: LiveSession, confirmed = false): Promise<void> {
    const named = session.name || session.id;
    const armed = this.state.stopArmed === session.key && Date.now() - this.stopArmedAt < 10_000;
    if (!confirmed && !armed) {
      const process = session.process;
      const which = process === null ? "its row records no runtime process" : process.stoppable ? `pid ${process.pid}` : `this dashboard cannot stop it: ${process.why}`;
      this.stopArmedAt = Date.now();
      this.set({ stopArmed: session.key });
      this.told(`stop ${named}'s runtime?`, `${which}: press again, or :stop!, within ten seconds`, "warn");
      return;
    }
    this.set({ stopArmed: "" });
    await this.supervise("stop", `stopping ${named}`, () => stopAgent(session.repository, session.id, this.state.access.token), (stopped) => stopped.detail);
  }

  /** What a pause or a resume reaches, in the words a notice says it with. */
  private reachWords(reach: Reach): string {
    const live = this.state.live;
    switch (reach.kind) {
      case "agent": {
        const named = (live === null ? undefined : memberById(live, reach.repository, reach.member))?.name || reach.member;
        return reach.tree ? `${named} and everything it spawned` : named;
      }
      case "repository": return `every agent of ${live?.repositories.get(reach.repository)?.name ?? reach.repository}`;
      case "all": return "every agent of every repository";
    }
  }

  /**
   * Hold what *reach* names at its next tool call; a freeze also stops the commands its tools run
   * and interrupts its turn. What came of it is said in the server's words, whom it could not freeze included.
   */
  async pause(reach: Reach, freeze: boolean): Promise<void> {
    if (reach.kind === "repository" && reach.repository === "") { this.say(`E: :${freeze ? "freeze" : "pause"} repo, in a repository`, "err"); return; }
    await this.supervise("pause", `${freeze ? "freezing" : "pausing"} ${this.reachWords(reach)}`, () => pauseAt(reach, freeze, this.state.access.token), (outcome) => outcome.detail);
  }

  /** Lift the pause placed on what *reach* names; a pause placed elsewhere is refused naming it, in the server's words. */
  async resume(reach: Reach): Promise<void> {
    if (reach.kind === "repository" && reach.repository === "") { this.say("E: :resume repo, in a repository", "err"); return; }
    await this.supervise("pause", `resuming ${this.reachWords(reach)}`, () => resumeAt(reach, this.state.access.token), (outcome) => outcome.detail);
  }

  /**
   * A post into a discussion reaches everyone in it, as one post replying to the one `r` chose or
   * its latest, every copy sharing its id and its thread.
   */
  async post(discussion: Discussion, text: string): Promise<void> {
    if (text.trim() === "") { this.say("write something first: c opens the box"); return; }
    const key = `thread:${discussion.key}`;
    const answering = this.state.threadReply;
    const thread = discussion.posts.find((each) => each.id === answering)?.thread || discussion.thread;
    const outcome = await this.supervise("thread-post", `posting into “${discussion.title}”`,
      () => postInto(discussion.repository, thread, { text, in_reply_to: answering, to: [] }, this.state.access.token),
      (posted) => `posted to ${plural(posted.deliveries.length, "member")}${posted.refused.length > 0 ? `; not to ${posted.refused.map((each) => `${each.address} (${each.reason})`).join(", ")}` : ""}`);
    if (outcome !== null) this.set((state) => ({ replyDrafts: { ...state.replyDrafts, [key]: "" }, threadReply: "" }));
  }

  /** One post to every working member of a repository, each woken as a message is. */
  async broadcast(repository: string, text: string): Promise<void> {
    if (text.trim() === "") { this.say("write something first: c opens the box"); return; }
    const key = `repo:${repository}`;
    this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: "Sending to every working member…", error: false } } }));
    try {
      const sent = await broadcastTo(repository, text, this.state.access.token);
      const woken = sent.outcomes.filter((each) => each.woken).length;
      this.set((state) => ({
        replyDrafts: { ...state.replyDrafts, [key]: "" },
        replyOutcome: { ...state.replyOutcome, [key]: { text: `Sent to ${plural(sent.outcomes.length, "working member")} as one post; ${woken} woken.`, error: false } },
      }));
      this.told(`broadcast to ${plural(sent.outcomes.length, "working member")}`, `one post; ${woken} woken`, "ok");
    } catch (failure) {
      const reason = failure instanceof Error ? failure.message : String(failure);
      this.set((state) => ({ replyOutcome: { ...state.replyOutcome, [key]: { text: reason, error: true } } }));
    }
  }

  /** A standing notice every session of a repository reads at the head of each prompt, until withdrawn. */
  async notice(repository: string, text: string): Promise<void> {
    if (repository === "" || text.trim() === "") { this.say("E: :notice <text>, in a repository", "err"); return; }
    await this.supervise("notices", "posting a notice", () => postNotice(repository, text, this.state.access.token), (posted) => `notice ${posted.id} stands over ${this.state.live?.repositories.get(repository)?.name ?? repository}`);
  }

  /** Take one standing notice down. */
  async withdraw(repository: string, id: string): Promise<void> {
    await this.supervise("notices", `withdrawing notice ${id}`, () => withdrawNotice(repository, id, this.state.access.token), (gone) => gone.withdrawn ? `notice ${gone.id} withdrawn` : `notice ${gone.id} was not standing`);
  }

  /** What the operator is on, said on their row in every repository served. */
  async describe(text: string): Promise<void> {
    await this.supervise("describe", "saying what you are on", () => describeYou(text, this.state.access.token), (described) => `your row says it in ${plural(described.repositories.length, "repository")}`);
  }

  /** Hold a path as the operator, or give one back: a relative path is the repository's checkout's. */
  async claim(repository: string, typed: string, holding: boolean): Promise<void> {
    const checkout = this.state.live?.repositories.get(repository)?.checkout ?? "";
    if (repository === "" || typed.trim() === "") { this.say(`E: :${holding ? "lock" : "release"} <path>, in a repository`, "err"); return; }
    const path = typed.trim().startsWith("/") ? typed.trim() : `${checkout.replace(/\/$/, "")}/${typed.trim()}`;
    const token = this.state.access.token;
    await this.supervise("claims", `${holding ? "holding" : "giving back"} ${path}`,
      () => holding ? holdPath(repository, path, token) : releasePath(repository, path, token),
      (claim) => claim.holders.length === 0 ? `nobody holds ${claim.path} now` : `${claim.path} is held by ${claim.holders.join(", ")}`);
  }

  /** Take these messages to the operator out of their mailbox, as read, each in its own repository. */
  async markRead(messages: LiveMessage[]): Promise<void> {
    const unread = messages.filter((message) => message.recipient === "user" && message.waiting);
    if (unread.length === 0) { this.say("nothing unread to mark"); return; }
    for (const repository of new Set(unread.map((message) => message.repository))) {
      const [first, ...rest] = unread.filter((message) => message.repository === repository).map((message) => message.id);
      if (first === undefined) continue;
      await this.supervise("inbox-read", "marking your mail read", () => readInbox(repository, [first, ...rest], this.state.access.token), (read) => `${plural(read.read.length, "message")} marked read`);
    }
  }

  /** Open an agent's whole transcript, at its latest page, and follow it while it shows. */
  async openTranscript(session: LiveSession): Promise<void> {
    const refused = this.lacks("transcript");
    const named = session.name || session.id;
    if (refused !== "") { this.told(`${named}'s transcript`, refused, "err"); return; }
    this.set({ float: { kind: "transcript" }, transcript: { session: session.key, repository: session.repository, member: session.id, name: named, entries: [], earlier: 0, end: 0, loading: true, error: "" } });
    try {
      const page = await readTranscript(session.repository, session.id, this.state.access.token);
      this.set((state) => state.transcript?.session !== session.key ? {} : { transcript: { ...state.transcript, entries: page.entries, earlier: page.earlier, end: page.end, loading: false } });
      await this.renewTranscript();
    } catch (failure) {
      const reason = failure instanceof Error ? failure.message : String(failure);
      this.set((state) => state.transcript?.session !== session.key ? {} : { transcript: { ...state.transcript, loading: false, error: reason } });
    }
  }

  /** The page before the earliest the open transcript holds. */
  async transcriptEarlier(): Promise<void> {
    const shown = this.state.transcript;
    if (shown === null || shown.earlier === 0) return;
    try {
      const page = await readTranscript(shown.repository, shown.member, this.state.access.token, shown.earlier);
      this.set((state) => state.transcript?.session !== shown.session ? {} : { transcript: { ...state.transcript, entries: [...page.entries, ...state.transcript.entries], earlier: page.earlier } });
    } catch (failure) {
      this.say(`${shown.name}'s transcript: ${failure instanceof Error ? failure.message : String(failure)}`, "err");
    }
  }

  /** Ask the stream to carry the open transcript on from where it stands, renewed while it shows. */
  private async renewTranscript(): Promise<void> {
    clearTimeout(this.followTimer);
    const shown = this.state.transcript;
    if (shown === null || this.state.float?.kind !== "transcript") return;
    try {
      const outcome = await followTranscripts([{ repository: shown.repository, member: shown.member, after: shown.end }], this.state.access.token);
      const refused = outcome.refused.find((each) => each.session === shown.session);
      if (refused !== undefined) { this.set((state) => ({ transcript: state.transcript === null ? null : { ...state.transcript, error: refused.reason } })); return; }
      this.followTimer = setTimeout(() => void this.renewTranscript(), Math.max(5, outcome.seconds / 2) * 1000);
    } catch (failure) {
      this.set((state) => ({ transcript: state.transcript === null ? null : { ...state.transcript, error: failure instanceof Error ? failure.message : String(failure) } }));
    }
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

  /**
   * A notice for each account whose window is newly used up, standing until it clears, which says
   * when, and that its sessions can be moved to another profile with room by `:switch`.
   */
  private budgetNotices(previous: LiveState | null, live: LiveState): void {
    const was = (key: string) => previous?.budget.accounts.find((each) => each.key === key)?.exhausted ?? "";
    const exhausted = live.budget.accounts.filter((each) => each.exhausted !== "");
    this.set((state) => ({ notes: state.notes.filter((note) => !note.key.startsWith("budget:") || exhausted.some((each) => note.key === `budget:${each.key}`)) }));
    for (const account of exhausted) {
      if (was(account.key) === account.exhausted) continue;
      const room = live.budget.accounts.filter((each) => each.account.runtime === account.account.runtime && each.key !== account.key && each.signed_in && each.exhausted === "")
        .sort((left, right) => (fullest(left)?.window.utilization_pct ?? 0) - (fullest(right)?.window.utilization_pct ?? 0));
      const switching = room.length === 0 ? "No other profile of this runtime has room."
        : this.lacks("profiles") !== "" ? `${room.map((each) => each.account.profile).join(", ")} ${room.length === 1 ? "has" : "have"} room; this dashboard cannot move sessions.`
          : `:switch ${room[0]?.account.profile ?? ""} moves a repository's contained sessions to it (${room.map((each) => each.account.profile).join(", ")} ${room.length === 1 ? "has" : "have"} room).`;
      this.notify(`${account.key}: ${account.exhausted}`, "", `Its agents wait until it clears. ${switching}`, "warn", { key: `budget:${account.key}`, sticky: true });
    }
  }

  /** One agent's priority or caps, as the operator sets them. */
  async settleBudget(session: LiveSession, request: AgentBudgetRequest): Promise<void> {
    const named = session.name || session.id;
    await this.supervise("budgets", `setting ${named}'s limits`, () => settleBudget(session.repository, session.id, request, this.state.access.token),
      (meter) => `${named}: ${meter.priority} priority, ${capsText(meter.caps) === "" ? "no caps" : `caps ${capsText(meter.caps)}`}`);
  }

  /**
   * Move a repository's sessions of one runtime onto a profile. Its contained sessions take the
   * login at their next request where the runtime rereads it; each other one is answered with the
   * command that opens it again there, and why — every line kept in `:messages`.
   */
  async switchProfile(repository: string, profile: string, runtime: "claude" | "codex"): Promise<void> {
    const reply = await this.supervise("profiles", `moving ${runtime} sessions to ${profile}`, () => switchProfile(repository, { profile, runtime }, this.state.access.token),
      (switched) => switched.said[0] ?? `moved to ${profile}`);
    if (reply !== null && reply.said.length > 1) this.notify(`${runtime} sessions and ${profile}`, "", reply.said.slice(1).join("\n"), reply.outcome.held === null ? "warn" : "info", { sticky: true });
  }

  /** Put the turtle's slower limits in place, or take them away; flips it where *on* is not said. */
  async turtle(on?: boolean): Promise<void> {
    const wanted = on ?? !(this.state.live?.budget.turtle ?? false);
    await this.supervise("budgets", wanted ? "turning the turtle on" : "turning the turtle off", () => setTurtle(wanted, this.state.access.token),
      (turned) => turned.on ? "🐢 turtle on: every account is under its slower limits" : "turtle off: every account is under its usual limits");
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
