import type { Feature, LiveMessage, LiveRepository, LiveSession, MessagePage, ReviewSnapshot, ReviewSummary, RunningCode, StreamFrame, UserRow } from "../generated/views";

/**
 * Everything live the page shows, as the stream has moved it so far.
 * Each collection is replaced only when a frame changes it, so a view reading
 * one — the review queue reading `reviews` — redraws only for its own frames.
 * `code` is which code the dashboard runs, and whether its checkout moved past it.
 * `earlier` is, per repository, the byte of its mail record its messages here
 * start at: older ones are read a page at a time from there back, and 0 is a
 * repository whose every message is here. `users` is the person's own row in
 * each repository, and `served` the supervision the dashboard serves.
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
};

/** What a dashboard that has not said which code it runs is taken to run. */
export const UNSAID: RunningCode = { source: "", root: "", since: null, older: false, failing: "", restarted: "" };

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
    };
  }
  const base: LiveState = { ...(state ?? { repositories: new Map(), sessions: new Map(), messages: new Map(), earlier: new Map(), reviews: { roots: [], reviews: [], errors: [], history: 0 }, code: UNSAID, users: new Map(), served: [] }), cursor: frame.cursor };
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

/** One session and the subagents running inside it. */
export type SessionNode = { session: LiveSession; subagents: LiveSession[] };

/** One repository's sessions, each with its subagents beneath it, working ones first. */
export type RepositorySessions = { repository: LiveRepository; sessions: SessionNode[] };

function byStanding(left: LiveSession, right: LiveSession): number {
  return Number(right.running) - Number(left.running) || (left.name || left.id).localeCompare(right.name || right.id);
}

export function sessionTree(state: LiveState): RepositorySessions[] {
  const sessions = [...state.sessions.values()];
  return [...state.repositories.values()]
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((repository) => {
      const here = sessions.filter((each) => each.repository === repository.key);
      const ids = new Set(here.map((each) => each.id));
      return {
        repository,
        sessions: here
          .filter((each) => each.parent === "" || !ids.has(each.parent))
          .sort(byStanding)
          .map((session) => ({ session, subagents: here.filter((each) => each.parent === session.id).sort(byStanding) })),
      };
    });
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
