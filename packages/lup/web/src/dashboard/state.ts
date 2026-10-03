// Everything the page shows, as one record the controller moves and React
// draws from. Each part is replaced, never edited in place, so a view that
// reads one part redraws only when that part moves.
import { useSyncExternalStore } from "react";
import type { CodeHover, CodeLocation, CodeSource, KeyBindings, KeyLine, ReviewDetail, ReviewSummary, SetupPane, ThreadEntry, TranscriptEntry } from "../generated/views";
import type { ReviewAccess, ReviewLink } from "./api";
import type { Paint } from "./highlight";
import type { LiveState } from "./live";
import type { Draft, PaneView, ReviewUi } from "./review";
import type { LogLine, Selection, TreeFilter } from "./supervision";

export type View = "supervise" | "history" | "inbox" | "threads" | "setup";
export const VIEWS: View[] = ["supervise", "history", "inbox", "threads", "setup"];
export const VIEW_NAMES: Record<View, string> = { supervise: "Supervise", history: "History", inbox: "Inbox", threads: "Threads", setup: "Setup" };

/**
 * The places focus moves between, exactly one holding it: the tree (History's
 * list in History, the discussions in Threads), the buffer (`editor`), the box
 * under it (`composer`), and the context; in Setup, its list and its page.
 */
export type Win = "queue" | "editor" | "composer" | "context" | "setup" | "setupframe";

/**
 * What one pane of the editor shows, and where its cursor is: a row, and the
 * column the operator chose on it (`want`), which a shorter line clamps and a
 * longer one gives back, as an editor's does; `$` wants the end, Infinity.
 */
export type Pane = { view: PaneView; cur: number; want: number };

/**
 * A document a window shows in place of the review it was opened from, as
 * Neovim's `gd` shows a definition: read-only, the review it belongs to, the
 * document it is and its text, the name jumped to, and where.
 */
export type Peek = { owner: string; source: CodeSource; text: string; name: string; line: number; column: number };

/** Where a window stood before a jump, so `Ctrl+o` comes back to it. */
export type Jump = { peek: Peek | null; cur: number; want: number };

/** A place in a document a question about code is asked at: the document, a one-based line and a UTF-16 column, and the name there. */
export type CodeAt = { source: CodeSource; line: number; column: number; name: string };

/** What the hover asked a language server, and its answer once it came; null while it is on its way. */
export type CodeAsk = { at: CodeAt; hover: CodeHover | null; pointer: boolean };

/** Every use `gr` found of one name, listed in the context: null while the server is asked. */
export type References = { owner: string; at: CodeAt; locations: CodeLocation[] | null; why: string };

export type Tone = "" | "ok" | "err" | "warn" | "info";

/** A notice: an outcome, a refusal, or word from the dashboard; sticky ones stand until dismissed. */
export type Notice = { id: number; heading: string; title: string; detail: string; tone: Tone; key: string; sticky: boolean; open: boolean; failed: string };

/** A float: help, full context, what the page said, the palette, your keys, the checkouts, a hover, or the finder. */
export type Float =
  | { kind: "help" }
  | { kind: "context" }
  | { kind: "messages" }
  | { kind: "contrast" }
  | { kind: "keys" }
  | { kind: "checkouts" }
  | { kind: "hover"; top: number; left: number; code?: CodeAsk }
  | { kind: "finder"; picker: string; query: string; cur: number }
  | { kind: "transcript" };

/** An agent's transcript as the float shows it: the pages read, and what the stream carried on since. */
export type TranscriptView = {
  session: string;
  repository: string;
  member: string;
  name: string;
  entries: TranscriptEntry[];
  /** The byte the earliest page read starts at; 0 where it is the transcript's start. */
  earlier: number;
  /** The byte just past the last whole line read, which the stream carries on from. */
  end: number;
  loading: boolean;
  error: string;
};

/** The command line: `:` runs a command, `/` searches the focused window. */
export type CommandLine = { prefix: ":" | "/"; text: string; wild: string[]; wildAt: number; base: string; history: number };

/** A search: the last pattern, and while one is typed, where the cursor was before it. */
export type Search = { pattern: string; typing: string | null; from: { win: Win; pane: number; cur: number; tree: number } | null; lit: boolean };

/** What the touch layout has open: a drawer, and a sheet over it. */
export type Touch = { drawer: "" | "tree" | "context"; sheet: "" | "more" | "agent" | "approve" | "decline" | "remark" | "hover" | "nav" };

/** What the phone's strip under a review steps by (decision 123). */
export type NavKind = "exception" | "change" | "file" | "marker" | "asked";

export type Settings = { wrap: boolean; numbers: boolean; advance: boolean; size: number };

export type PageState = {
  access: ReviewAccess;
  denied: boolean;
  connection: string;
  live: LiveState | null;
  linked: ReviewLink | null;
  older: ReviewSummary[];
  olderLoading: boolean;
  fetched: ReadonlyMap<string, ReviewDetail>;
  answered: ReadonlyMap<string, ReviewDetail>;
  sending: ReadonlyMap<string, ThreadEntry>;
  failures: Readonly<Record<string, string>>;
  drafts: Readonly<Record<string, Draft>>;
  panes: SetupPane[] | null;
  setupAt: number;

  view: View;
  sel: Selection;
  focus: Win;
  pane: 0 | 1;
  split: "" | "v" | "s";
  editor: [Pane, Pane];
  /** The column the caret stands on in the focused pane, as its line clamps what the pane wants. */
  column: number;
  visual: { pane: 0 | 1; anchor: number } | null;
  ui: Readonly<Record<string, ReviewUi>>;
  editing: string | null;
  anchor: { fi: number; side: "before" | "after"; line: number } | null;
  deleted: { key: string; comment: Draft["comments"][number] } | null;
  judgedAt: number;
  exceptionAt: number;
  markerAt: number;

  tree: TreeFilter;
  treeCur: number;
  collapsed: ReadonlySet<string>;
  showStopped: boolean;
  stoppedOpen: ReadonlySet<string>;
  ctxCur: number;
  queueShown: boolean;
  contextShown: boolean;
  wide: boolean;
  narrow: boolean;

  settings: Settings;
  armed: boolean;
  typing: boolean;
  pending: string;
  whichKey: boolean;
  float: Float | null;
  cmdline: CommandLine | null;
  search: Search;
  notes: Notice[];
  said: string[];
  message: { text: string; tone: Tone };
  replyDrafts: Readonly<Record<string, string>>;
  replyOutcome: Readonly<Record<string, { text: string; error: boolean }>>;
  /** The post `r` chose to answer in the open discussion; empty answers its last. */
  threadReply: string;
  /** The post an agent's box answers, by the box's key; empty starts a thread of its own. */
  replyTo: Readonly<Record<string, string>>;
  /** The agent whose stop is asked for once and waits for the second ask, by key. */
  stopArmed: string;
  transcript: TranscriptView | null;
  /** The document each window shows in place of its review after `gd`, and where each stood before. */
  peeks: [Peek | null, Peek | null];
  jumps: [Jump[], Jump[]];
  /** What `gr` found, listed in the context. */
  refs: References | null;
  /** A language server's paint over each document it classified, by the document's key. */
  semantic: ReadonlyMap<string, Paint[]>;
  log: LogLine[];
  keyLines: KeyLine[];
  tried: KeyBindings | null;
  touch: Touch;
  /** On a phone, the long prose opened past its four lines, by row or context key. */
  unclamped: ReadonlySet<string>;
  navKind: NavKind | "";
  now: number;
};

export function initialState(access: ReviewAccess, linked: ReviewLink | null, width: number): PageState {
  return {
    access, denied: false, connection: "Connecting…", live: null, linked, older: [], olderLoading: false,
    fetched: new Map(), answered: new Map(), sending: new Map(), failures: {}, drafts: {}, panes: null, setupAt: 0,
    view: "supervise", sel: { kind: "", key: "" }, focus: "editor", pane: 0, split: "",
    editor: [{ view: "diff", cur: 0, want: 0 }, { view: "after", cur: 0, want: 0 }], column: 0, visual: null, ui: {}, editing: null, anchor: null, deleted: null,
    judgedAt: -1, exceptionAt: -1, markerAt: -1,
    tree: "all", treeCur: 0, collapsed: new Set(), showStopped: false, stoppedOpen: new Set(), ctxCur: -1,
    queueShown: true, contextShown: width >= 1280, wide: width >= 1280, narrow: width <= 860,
    settings: { wrap: true, numbers: true, advance: true, size: 13 },
    armed: false, typing: false, pending: "", whichKey: false, float: null, cmdline: null,
    search: { pattern: "", typing: null, from: null, lit: false },
    notes: [], said: [], message: { text: "", tone: "" }, replyDrafts: {}, replyOutcome: {}, threadReply: "", replyTo: {}, stopArmed: "", transcript: null, peeks: [null, null], jumps: [[], []], refs: null, semantic: new Map(), log: [], keyLines: [], tried: null,
    touch: { drawer: "", sheet: "" }, unclamped: new Set(), navKind: "", now: Date.now(),
  };
}

/** One page state, moved by replacing its parts, and the views following it. */
export class Store {
  private state: PageState;
  private readonly listeners = new Set<() => void>();

  constructor(state: PageState) {
    this.state = state;
  }

  readonly get = (): PageState => this.state;

  set(update: Partial<PageState> | ((state: PageState) => Partial<PageState>)): void {
    const changes = typeof update === "function" ? update(this.state) : update;
    this.state = { ...this.state, ...changes };
    for (const listener of this.listeners) listener();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

export function useStore(store: Store): PageState {
  return useSyncExternalStore(store.subscribe, store.get);
}
