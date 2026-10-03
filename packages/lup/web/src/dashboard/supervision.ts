// Who is here in every repository, what each is doing, and what each asks of
// the operator: the agents tree, and the buffers beside it for an agent, the
// operator's own row, a repository's page, and the inbox. Worked out from what
// the stream carries today — the roster's rows with each one's current call,
// last words, holds and mailbox, and the mail between members. What needs new
// server work (decision 62) stays behind the seam in `served.ts`.
import type { LiveMessage, LiveRepository, LiveSession, ReviewRoot, ReviewSummary } from "../generated/views";
import { called, conversation, type LiveState } from "./live";
import { askedBy, claimCovers, plural, Rows, type Buffer, type Holder } from "./review";

/** How long ago something happened, as the tree says it: `45s`, `17m`, `6h`, `3d`. */
export function ago(when: string | null, now = Date.now()): string {
  if (when === null || when === "") return "";
  const seconds = Math.max(0, Math.round((now - Date.parse(when)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export const clock = (when: string | null) => when === null || when === "" ? "" : new Date(when).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
export const stamp = (when: string | null) => when === null || when === "" ? "" : new Date(when).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const minutesSince = (when: string | null, now: number) => when === null || when === "" ? Infinity : (now - Date.parse(when)) / 60000;

export type Standing = "working" | "idle" | "quiet" | "stopped";
export const GLYPH: Record<Standing, string> = { working: "●", idle: "◌", quiet: "◷", stopped: "○" };

/** Whether an agent is working, idle, quiet (a call running ten minutes with nothing new) or stopped. */
export function standing(session: LiveSession, now = Date.now()): Standing {
  if (!session.running) return "stopped";
  if (session.activity.calling === "") return "idle";
  return minutesSince(session.activity.at ?? session.heard, now) >= 10 ? "quiet" : "working";
}

/** The repository a review's queue belongs to, as the stream keys it. */
export function repositoryOf(live: LiveState, roots: ReviewRoot[], rootId: string): string {
  const root = roots.find((each) => each.id === rootId);
  if (root === undefined) return "";
  return [...live.repositories.values()].find((repository) => repository.repository === root.repository || repository.repository === root.path)?.key ?? "";
}

export const membersOf = (live: LiveState, repository: string) => [...live.sessions.values()].filter((session) => session.repository === repository);

/** The member a name answers to: the running one, else the one heard last, as the roster resolves a name. */
export function memberByName(live: LiveState, repository: string, name: string): LiveSession | undefined {
  return membersOf(live, repository).filter((session) => session.name === name)
    .sort((left, right) => Number(right.running) - Number(left.running) || Date.parse(right.heard ?? "0") - Date.parse(left.heard ?? "0"))[0];
}

export const memberById = (live: LiveState, repository: string, id: string) => membersOf(live, repository).find((session) => session.id === id);

export function memberOfReview(live: LiveState, roots: ReviewRoot[], row: ReviewSummary): LiveSession | undefined {
  return memberByName(live, repositoryOf(live, roots, row.root_id), askedBy(row));
}

/** The reviews one member parked, waiting ones only unless asked for all. */
export function reviewsOf(live: LiveState, roots: ReviewRoot[], rows: ReviewSummary[], session: LiveSession, pendingOnly = true): ReviewSummary[] {
  return rows.filter((row) => (!pendingOnly || row.state === "pending")
    && askedBy(row) === session.name && repositoryOf(live, roots, row.root_id) === session.repository
    && memberOfReview(live, roots, row)?.key === session.key);
}

export const parentOf = (live: LiveState, session: LiveSession) => session.parent === "" ? undefined : memberById(live, session.repository, session.parent);

/** What kind of agent it is: a subagent, or a session, named by the runtime its wake reaches where it declares one. */
export function kindWords(session: LiveSession): string {
  if (session.parent !== "") return "subagent";
  return session.wake !== "" ? `${session.wake} session` : "session";
}

/** How a message reaches it, in the words a reader decides by. */
export function reached(session: LiveSession): string {
  if (session.wake !== "") return `its mailbox, then a wake through ${session.wake}`;
  if (session.delivery === "hook") return "its mailbox, handed over before its next tool call";
  return "its mailbox, read when it next looks";
}

/** Every message addressed to the operator, newest first. */
export function inboxOf(live: LiveState): LiveMessage[] {
  return [...live.messages.values()].filter((each) => each.recipient === "user").sort((left, right) => Date.parse(right.sent_at) - Date.parse(left.sent_at) || right.at - left.at);
}

export const unreadCount = (live: LiveState, repository = "") => inboxOf(live).filter((each) => each.waiting && (repository === "" || each.repository === repository)).length;
export const wroteYou = (live: LiveState, session: LiveSession) => inboxOf(live).filter((each) => each.repository === session.repository && each.sender === session.id && each.waiting).length;

/** A call's arguments in one line: its description, command, path, query or address, whichever it names first. */
export function callSummary(args: Record<string, unknown>): string {
  for (const name of ["description", "command", "file_path", "path", "pattern", "query", "url", "prompt", "subject"]) {
    const value = args[name];
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  const keys = Object.keys(args);
  return keys.length > 0 ? keys.join(", ") : "";
}

/** What an agent is doing now, as one passage: its call, else its last words, else what it says it is on. */
export function activityBrief(session: LiveSession, now = Date.now()): string {
  const activity = session.activity;
  if (!session.running) return session.summary || session.error || `stopped ${ago(session.heard, now)} ago`;
  if (activity.calling !== "") {
    const said = callSummary(activity.arguments);
    return `calling ${activity.calling}${said !== "" ? ` · ${said}` : ""}`;
  }
  return activity.said || session.doing || "";
}

export type Flag = { key: string; text: string };

/** What an agent needs from the operator: quiet, long idle, a review waiting, unread words to them, mail it has not taken, a contested hold. */
export function attention(live: LiveState, roots: ReviewRoot[], rows: ReviewSummary[], session: LiveSession, now = Date.now()): Flag[] {
  const flags: Flag[] = [];
  const state = standing(session, now);
  if (state === "quiet") flags.push({ key: "quiet", text: `quiet ${Math.floor(minutesSince(session.activity.at, now))}m while calling ${session.activity.calling}` });
  const since = minutesSince(session.activity.at ?? session.heard, now);
  if (state === "idle" && session.parent === "" && since >= 30) flags.push({ key: "idle", text: `idle ${Math.floor(since)}m` });
  const asks = reviewsOf(live, roots, rows, session).length;
  if (asks > 0) flags.push({ key: "asks", text: `${plural(asks, "review")} wait${asks === 1 ? "s" : ""} on you` });
  const wrote = wroteYou(live, session);
  if (wrote > 0) flags.push({ key: "wrote", text: `wrote to you (${wrote} unread)` });
  if (session.running && session.waiting > 0) flags.push({ key: "mail", text: `${plural(session.waiting, "message")} it has not taken` });
  if (session.contested.length > 0) flags.push({ key: "contested", text: `${plural(session.contested.length, "path")} another member holds too` });
  return flags;
}

/** Who holds one path now: running members that touched or locked it. */
export function holdersOf(live: LiveState, path: string): Holder[] {
  return [...live.sessions.values()].filter((session) => session.running).flatMap((session) => session.holding
    .filter((claim) => claimCovers(claim, path)).map((claim) => ({ name: session.name || session.id, lock: claim.startsWith("under ") })));
}

/** The other members holding a path one member holds. */
export function heldOthers(live: LiveState, session: LiveSession, claim: string): string[] {
  return [...live.sessions.values()].filter((each) => each.key !== session.key && each.holding.includes(claim)).map((each) => each.name || each.id);
}

const byStanding = (left: LiveSession, right: LiveSession) => Number(right.running) - Number(left.running) || (left.name || left.id).localeCompare(right.name || right.id);

export const childrenOf = (live: LiveState, session: LiveSession) => membersOf(live, session.repository).filter((each) => each.parent === session.id).sort(byStanding);

function subtreeHas(live: LiveState, session: LiveSession, test: (each: LiveSession) => boolean): boolean {
  return test(session) || childrenOf(live, session).some((child) => subtreeHas(live, child, test));
}

/** Which agents the tree shows: every one, those that need the operator, or only the reviews waiting. */
export type TreeFilter = "all" | "attention" | "reviews";

export type TreeItem =
  | { t: "repo"; key: string; depth: number }
  | { t: "you"; key: string; depth: number }
  | { t: "member"; key: string; depth: number }
  | { t: "folded"; key: string; depth: number; count: number; open: boolean }
  | { t: "review"; key: string; depth: number; orphan: boolean };

export type TreeOptions = {
  filter: TreeFilter;
  collapsed: ReadonlySet<string>;
  showStopped: boolean;
  stoppedOpen: ReadonlySet<string>;
  now: number;
};

/**
 * The agents tree: each repository, the operator's own row, each session with
 * its subagents nested beneath it, and under each agent the reviews it parked
 * that wait on the operator. A repository's stopped agents fold into one row
 * at its end, unless something under one still runs, waits on the operator,
 * or wrote to them unread.
 */
export function treeItems(live: LiveState, roots: ReviewRoot[], pending: ReviewSummary[], options: TreeOptions): TreeItem[] {
  const items: TreeItem[] = [];
  const repositories = [...live.repositories.values()].sort((left, right) => left.name.localeCompare(right.name));
  for (const repository of repositories) {
    items.push({ t: "repo", key: repository.key, depth: 0 });
    if (options.collapsed.has(repository.key)) continue;
    if (options.filter !== "reviews") items.push({ t: "you", key: repository.key, depth: 1 });
    const here = membersOf(live, repository.key);
    const ids = new Set(here.map((each) => each.id));
    const tops = here.filter((each) => each.parent === "" || !ids.has(each.parent)).sort(byStanding);
    const expanded = options.showStopped || options.stoppedOpen.has(repository.key);
    const reviews = (session: LiveSession) => reviewsOf(live, roots, pending, session);
    const lives = (session: LiveSession) => subtreeHas(live, session, (each) => each.running || reviews(each).length > 0 || wroteYou(live, each) > 0);
    const size = (session: LiveSession): number => 1 + childrenOf(live, session).reduce((total, child) => total + size(child), 0);
    let folded = 0;
    const visit = (session: LiveSession, depth: number) => {
      const shown = options.filter === "all"
        || (options.filter === "reviews" ? subtreeHas(live, session, (each) => reviews(each).length > 0)
          : subtreeHas(live, session, (each) => attention(live, roots, pending, each, options.now).length > 0));
      if (!shown) return;
      if (!lives(session)) {
        folded += size(session);
        if (!expanded) return;
      }
      items.push({ t: "member", key: session.key, depth });
      if (options.collapsed.has(session.key)) return;
      for (const row of reviews(session)) items.push({ t: "review", key: row.key, depth: depth + 1, orphan: false });
      for (const child of childrenOf(live, session)) visit(child, depth + 1);
    };
    for (const session of tops) visit(session, 1);
    if (folded > 0) items.push({ t: "folded", key: repository.key, depth: 1, count: folded, open: expanded });
    for (const row of pending) {
      if (repositoryOf(live, roots, row.root_id) === repository.key && memberByName(live, repository.key, askedBy(row)) === undefined) {
        items.push({ t: "review", key: row.key, depth: 1, orphan: true });
      }
    }
  }
  // A review whose queue no repository on the stream serves still waits on the operator.
  for (const row of pending) {
    if (repositoryOf(live, roots, row.root_id) === "") items.push({ t: "review", key: row.key, depth: 0, orphan: true });
  }
  return items;
}

/** The kinds of thing the centre can hold, besides a review. */
export type Selection = { kind: "review" | "member" | "you" | "repo" | "thread" | ""; key: string };

const json = (value: unknown) => JSON.stringify(value, null, 2) ?? "null";

/** An agent's buffer: what it is doing now, and every message to or from it. */
export function memberBuffer(live: LiveState, session: LiveSession, now = Date.now()): Buffer {
  const out = new Rows();
  const activity = session.activity;
  const state = standing(session, now);
  out.push({ t: "sec", key: "now", text: "now", sub: session.running ? (activity.calling !== "" ? `calling ${activity.calling} · since ${clock(activity.at)} (${ago(activity.at, now)} ago)` : `${state} · last in its transcript ${ago(activity.at ?? session.heard, now)} ago`) : `stopped ${ago(session.heard, now)} ago` });
  if (!session.running && (session.summary !== "" || session.error !== "")) out.push({ t: "msg", key: "ended", tone: session.error !== "" ? "err" : "", text: session.summary || session.error });
  if (activity.calling !== "") {
    out.push({ t: "kv", key: "calling", k: "calling", v: `${activity.calling} — the call nothing has answered yet` });
    json(activity.arguments).split("\n").forEach((text, at) => out.push({ t: "json", key: `args:${at}`, text }));
  }
  if (activity.said !== "") out.push({ t: "said", key: "said", text: activity.said });
  else if (session.running) out.push({ t: "msg", key: "silent", tone: "muted", text: activity.transcript !== "" || session.parent !== "" ? "Nothing said in its own words yet." : "Its roster row names no transcript, so what it is doing now is not known here." });
  const messages = conversation(live, session.repository, session.id);
  out.push({ t: "sec", key: "messages", text: `messages · ${messages.length}`, sub: "oldest first · c writes" });
  mailTail(out, live, session.repository, messages, "Nothing said to it or by it");
  return out.buffer();
}

function mailTail(out: Rows, live: LiveState, repository: string, messages: LiveMessage[], none: string): void {
  const before = live.earlier.get(repository) ?? 0;
  if (before > 0) out.push({ t: "earlier", key: `earlier:${repository}`, repository, before });
  if (messages.length === 0) out.push({ t: "msg", key: "nomail", tone: "muted", text: before > 0 ? `${none} among the messages loaded.` : `${none} yet.` });
  for (const each of messages) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: each.recipient === "user" && each.waiting });
}

/** The operator's working verbs, one level down, each with what the dashboard's server needs to carry it. */
export const VERBS: { command: string; what: string; server: string }[] = [
  { command: ":msg <agent> <text>", what: "write to any member, session or subagent; c does it for the one selected", server: "today" },
  { command: ":broadcast <text>", what: "one message to every working member of the repository", server: "today, as one send each" },
  { command: ":reply <text>", what: "reply in the thread of the message under the cursor (r)", server: "new: in_reply_to on the message route" },
  { command: ":notice <text>", what: "a standing notice every session reads at the head of each prompt until you withdraw it", server: "new: a route over RepositoryPeers.notify" },
  { command: ":redirect <agent> <text>", what: "refuse its next tool call with your words as the reason", server: "new: redirect on the message route" },
  { command: ":describe <text>", what: "say what you are on; agents read it in coordination_peers", server: "new: the user row in the listing" },
  { command: ":lock <path>", what: "hold a path; an agent writing under it parks a review for you", server: "new: the user's claims counted" },
  { command: ":release <path>", what: "give a hold back; only what you hold", server: "new" },
  { command: ":rename <agent> <name>", what: "call a session or subagent something else; its id still reaches it", server: "new: a route over RepositoryPeers.rename" },
  { command: ":read, x, X", what: "mark your inbox read, one or all", server: "new: a route committing your mailbox" },
];

/** The operator's own page in one repository: what was sent to them, what they sent, and their verbs. */
export function youBuffer(live: LiveState, repository: LiveRepository): Buffer {
  const out = new Rows();
  out.push({ t: "sec", key: "you", text: `you in ${repository.name}`, sub: "a peer like any agent, reached at user" });
  out.push({ t: "msg", key: "proposed", tone: "muted", text: "What you are on, your holds and your notices need new server work (decision 62, items 4 and 11–14); this page shows them once the dashboard serves them." });
  const inbox = inboxOf(live).filter((each) => each.repository === repository.key);
  out.push({ t: "sec", key: "to-you", text: `to you · ${inbox.filter((each) => each.waiting).length} unread of ${inbox.length}`, sub: "gi opens the inbox" });
  for (const each of inbox.slice(0, 6)) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: each.waiting });
  const sent = conversation(live, repository.key, "").filter((each) => each.sender === "user").slice(-5);
  out.push({ t: "sec", key: "sent", text: `what you sent lately · ${sent.length}`, sub: "" });
  for (const each of sent) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: false });
  out.push({ t: "sec", key: "verbs", text: "your verbs", sub: "one level down: the command line, Space p, and the finder (Space fc)" });
  VERBS.forEach((verb, at) => out.push({ t: "verb", key: `verb:${at}`, ...verb }));
  return out.buffer();
}

/** One line of what the stream moved, as a repository's page logs it. */
export type LogLine = { at: string; text: string; repository: string };

/** A repository's page: its members counted, every message between them, and what the stream moved. */
export function repoBuffer(live: LiveState, repository: LiveRepository, waiting: number, log: LogLine[]): Buffer {
  const out = new Rows();
  const here = membersOf(live, repository.key);
  out.push({ t: "sec", key: "repo", text: repository.name, sub: `${here.filter((each) => each.running).length} working · ${here.filter((each) => !each.running).length} stopped · ${waiting} ${waiting === 1 ? "review waits" : "reviews wait"} on you` });
  out.push({ t: "kv", key: "repository", k: "repository", v: repository.repository });
  out.push({ t: "kv", key: "checkout", k: "checkout", v: repository.checkout });
  const messages = conversation(live, repository.key, "");
  out.push({ t: "sec", key: "messages", text: `every message between its members · ${messages.length}`, sub: "c broadcasts to every working member" });
  mailTail(out, live, repository.key, messages, "No member here has written to another");
  out.push({ t: "sec", key: "log", text: "live · what the stream moved", sub: "newest last" });
  const shown = log.filter((line) => line.repository === repository.key || line.repository === "").slice(-20);
  if (shown.length === 0) out.push({ t: "msg", key: "quiet", tone: "muted", text: "Nothing has moved since this tab connected." });
  shown.forEach((line, at) => out.push({ t: "log", key: `log:${at}:${line.at}`, at: line.at, text: line.text }));
  return out.buffer();
}

/** Every message to the operator, grouped by repository, newest first. */
export function inboxBuffer(live: LiveState): Buffer {
  const out = new Rows();
  const all = inboxOf(live);
  out.push({ t: "sec", key: "inbox", text: `to you · ${all.filter((each) => each.waiting).length} unread of ${all.length}`, sub: "newest first · Enter opens its sender, ready to write back" });
  for (const repository of [...live.repositories.values()].sort((left, right) => left.name.localeCompare(right.name))) {
    const here = all.filter((each) => each.repository === repository.key);
    if (here.length === 0) continue;
    out.push({ t: "sec", key: `inbox:${repository.key}`, text: repository.name, sub: `${here.filter((each) => each.waiting).length} unread` });
    for (const each of here) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: each.waiting });
  }
  if (all.length === 0) out.push({ t: "msg", key: "none", tone: "muted", text: "Nothing has been sent to you." });
  return out.buffer();
}

/** A message's sender and recipient as a reader calls them. */
export function mailHeads(live: LiveState, message: LiveMessage): { from: string; to: string } {
  return { from: message.sender === "" ? message.door : called(live, message.repository, message.sender), to: called(live, message.repository, message.recipient) };
}

/** The member a message's thread is with: its sender, or its recipient where the operator sent it. */
export function counterpart(live: LiveState, message: LiveMessage): LiveSession | undefined {
  return memberById(live, message.repository, message.sender === "user" ? message.recipient : message.sender);
}

/** A path as read beside its repository: relative to the directory holding its checkouts, `tree/feature/src/app.py`. */
export function inRepository(path: string, repository: LiveRepository | undefined): string {
  const named = repository?.repository ?? "";
  const home = named.endsWith("/.git") ? named.slice(0, -"/.git".length) : named;
  if (home === "") return path;
  if (path === home) return home.slice(home.lastIndexOf("/") + 1);
  return path.startsWith(`${home}/`) ? path.slice(home.length + 1) : path;
}
