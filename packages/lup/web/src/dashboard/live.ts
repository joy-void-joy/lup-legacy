import type { BudgetView, Feature, KeyBindings, LiveMessage, LiveRepository, LiveSession, MessagePage, ReviewSnapshot, ReviewSummary, RunningCode, StreamFrame, UserRow } from "../generated/views";

/**
 * Everything live the page shows, as the stream has moved it so far.
 * Each collection is replaced only when a frame changes it, so a view reading
 * one — the review queue reading `reviews` — redraws only for its own frames.
 * `code` is which code the dashboard runs, and whether its checkout moved past it.
 * `earlier` is, per repository, the byte of its mail record its messages here
 * start at: older ones are read a page at a time from there back, and 0 is a
 * repository whose every message is here. `users` is the person's own row in
 * each repository, and `served` the supervision the dashboard serves.
 * `budget` is each account's meter and each agent's spend, where the
 * dashboard governs a budget.
 */
export type LiveState = {
  cursor: string;
  repositories: ReadonlyMap<string, LiveRepository>;
  sessions: ReadonlyMap<string, LiveSession>;
  messages: ReadonlyMap<string, LiveMessage>;
  earlier: ReadonlyMap<string, number>;
  reviews: ReviewSnapshot;
  code: RunningCode;
  users: ReadonlyMap<string, UserRow>;
  served: readonly Feature[];
  keys: KeyBindings;
  budget: BudgetView;
};

/** What a dashboard that has not said which code it runs is taken to run. */
export const UNSAID: RunningCode = { source: "", root: "", since: null, older: false, failing: "", restarted: "" };

/** What a dashboard that has not said the person's keys is taken to say: every key lup's own. */
export const NO_KEYS: KeyBindings = { source: "", unread: "", changed: [], report: { applied: [], refused: [], waits: [] } };

/** What a dashboard that governs no budget is taken to show: no account, no agent's spend. */
export const NO_BUDGET: BudgetView = { accounts: [], agents: [], turtle: false, telemetry: false, refused: "", holds: false };

/** One line of what the stream moved, as a repository's page logs it, newest last. */
export type Moved = { repository: string; text: string };

/**
 * What one frame moved, in words: an agent arriving, starting a call or
 * stopping, a message posted, a review parked or leaving the queue. The page
 * writes its live log from the frames it applies; a snapshot says what it
 * handed over whole.
 */
export function moved(previous: LiveState | null, frame: StreamFrame): Moved[] {
  const event = frame.event;
  const name = (repository: string, id: string) => called(previous ?? applied(null, frame), repository, id);
  switch (event.type) {
    case "snapshot":
      return [{ repository: "", text: `the stream handed this tab the whole state: ${event.repositories.length} ${event.repositories.length === 1 ? "repository" : "repositories"}, ${event.sessions.length} members, ${event.messages.length} messages, ${event.reviews.reviews.filter((row) => row.state === "pending").length} waiting reviews` }];
    case "session": {
      const before = previous?.sessions.get(event.session.key);
      const session = event.session;
      const who = session.name || session.id;
      if (before === undefined) return [{ repository: session.repository, text: `${who} arrived` }];
      if (before.running && !session.running) return [{ repository: session.repository, text: `${who} stopped${session.summary || session.error ? `: ${session.summary || session.error}` : ""}` }];
      if (session.activity.calling !== "" && session.activity.calling !== before.activity.calling) return [{ repository: session.repository, text: `${who} calling ${session.activity.calling}` }];
      if (session.doing !== before.doing && session.doing !== "") return [{ repository: session.repository, text: `${who} is on: ${session.doing}` }];
      return [];
    }
    case "message": {
      if (previous?.messages.has(event.message.key) === true) return [];
      const message = event.message;
      return [{ repository: message.repository, text: `${message.sender === "" ? message.door : name(message.repository, message.sender)} → ${name(message.repository, message.recipient)}: ${message.text}` }];
    }
    case "review": {
      const was = previous?.reviews.reviews.find((row) => row.key === event.review.key);
      if (was === undefined && event.review.state === "pending") return [{ repository: "", text: `review ${event.review.id.slice(0, 8)} parked by ${event.review.session || event.review.requester}` }];
      if (was !== undefined && was.state !== event.review.state) return [{ repository: "", text: `review ${event.review.id.slice(0, 8)} is ${event.review.state === "rejected" ? "declined" : event.review.state}` }];
      return [];
    }
    case "session_gone": {
      const gone = previous?.sessions.get(event.key);
      return gone === undefined ? [] : [{ repository: gone.repository, text: `${gone.name || gone.id} left the roster` }];
    }
    default: return [];
  }
}

/**
 * What the page says of the dashboard it follows: the code it runs, where
 * its checkout moved past it, and why it stopped, for a few minutes after
 * the sessions holding it started it again; nothing while it runs on.
 */
export function codeNotice(code: RunningCode): string {
  const restarted = code.restarted ? `This dashboard was restarted after it stopped: ${code.restarted}.` : "";
  if (!code.older) return restarted;
  const older = code.failing
    ? `This dashboard runs older code than its checkout, whose newer code does not start (${code.failing}). It keeps answering with the code it runs.`
    : "This dashboard runs older code than its checkout and is restarting onto it; answers wait until the page reconnects.";
  return restarted ? `${older} ${restarted}` : older;
}

function newestFirst(rows: ReviewSummary[]): ReviewSummary[] {
  return [...rows].sort((left, right) => Date.parse(right.created) - Date.parse(left.created));
}

function keyed<Value extends { key: string }>(values: Value[]): Map<string, Value> {
  return new Map(values.map((value) => [value.key, value]));
}

function set<Value>(map: ReadonlyMap<string, Value>, key: string, value: Value): Map<string, Value> {
  return new Map(map).set(key, value);
}

function without<Value>(map: ReadonlyMap<string, Value>, key: string): Map<string, Value> {
  const next = new Map(map);
  next.delete(key);
  return next;
}

/** The state one frame leaves: a snapshot replaces it whole, every other frame moves one thing. */
export function applied(state: LiveState | null, frame: StreamFrame): LiveState {
  const event = frame.event;
  if (event.type === "snapshot") {
    return {
      cursor: frame.cursor,
      repositories: keyed(event.repositories),
      sessions: keyed(event.sessions),
      messages: keyed(event.messages),
      earlier: new Map(event.extents.map((extent) => [extent.repository, extent.earlier])),
      reviews: { ...event.reviews, reviews: newestFirst(event.reviews.reviews) },
      code: event.code,
      users: keyed(event.users),
      served: event.served,
      keys: event.keys,
      budget: event.budget,
    };
  }
  const base: LiveState = { ...(state ?? { repositories: new Map(), sessions: new Map(), messages: new Map(), earlier: new Map(), reviews: { roots: [], reviews: [], errors: [], history: 0 }, code: UNSAID, users: new Map(), served: [], keys: NO_KEYS, budget: NO_BUDGET }), cursor: frame.cursor };
  switch (event.type) {
    case "repository": return { ...base, repositories: set(base.repositories, event.repository.key, event.repository) };
    case "repository_gone": return { ...base, repositories: without(base.repositories, event.key) };
    case "session": return { ...base, sessions: set(base.sessions, event.session.key, event.session) };
    case "session_gone": return { ...base, sessions: without(base.sessions, event.key) };
    case "message": return { ...base, messages: set(base.messages, event.message.key, event.message) };
    case "review": {
      const others = base.reviews.reviews.filter((row) => row.key !== event.review.key);
      return { ...base, reviews: { ...base.reviews, reviews: newestFirst([...others, event.review]) } };
    }
    case "review_gone": return { ...base, reviews: { ...base.reviews, reviews: base.reviews.reviews.filter((row) => row.key !== event.key) } };
    case "review_scope": return { ...base, reviews: { ...base.reviews, roots: event.roots, errors: event.errors, history: event.history } };
    case "service": return { ...base, code: event.code };
    case "user": return { ...base, users: set(base.users, event.user.key, event.user) };
    // A followed transcript is read where it is shown, never held as state.
    case "transcript": return base;
    case "keys": return { ...base, keys: event.keys };
    case "budget": return { ...base, budget: event.budget };
  }
}

/**
 * The state with one older page of a repository's messages folded in, as read back from `before`.
 * A message the state already holds keeps the stream's copy. A page read from a byte the state no
 * longer starts at — a snapshot replaced it meanwhile — is dropped, so what is shown never skips
 * the messages between the two.
 */
export function paged(state: LiveState, repository: string, before: number, page: MessagePage): LiveState {
  if (state.earlier.get(repository) !== before) return state;
  const messages = new Map([...page.messages.map((message): [string, LiveMessage] => [message.key, message]), ...state.messages]);
  return { ...state, messages, earlier: new Map(state.earlier).set(repository, page.earlier) };
}

function oldestFirst(messages: LiveMessage[]): LiveMessage[] {
  return messages.sort((left, right) => Date.parse(left.sent_at) - Date.parse(right.sent_at) || left.at - right.at);
}

/** Every message one repository's sessions said to each other, oldest first. */
export function repositoryMessages(state: LiveState, repository: string): LiveMessage[] {
  return oldestFirst([...state.messages.values()].filter((each) => each.repository === repository));
}

/** What one session was sent and what it sent, oldest first. */
export function conversation(state: LiveState, repository: string, member: string): LiveMessage[] {
  return repositoryMessages(state, repository).filter((each) => each.recipient === member || each.sender === member);
}

/** What a reader calls a member id: its roster name, `you` for the operator, else the id itself. */
export function called(state: LiveState, repository: string, id: string): string {
  if (id === "user") return "you";
  return [...state.sessions.values()].find((each) => each.repository === repository && each.id === id)?.name || id;
}
