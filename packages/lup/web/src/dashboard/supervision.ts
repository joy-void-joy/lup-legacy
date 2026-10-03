// Who is here in every repository, what each is doing, and what each asks of
// the operator: the agents tree, and the buffers beside it for an agent, the
// operator's own row, a repository's page, and the inbox. Worked out from what
// the stream carries today — the roster's rows with each one's current call,
// last words, holds and mailbox, the person's own row, and the mail between
// members. What a server older than the page does not serve stays behind the
// seam in `served.ts`.
import type { HeldBy, LiveMessage, LiveRepository, LiveSession, ReviewRoot, ReviewSummary } from "../generated/views";
import type { Reach } from "./api";
import { called, conversation, repositoryMessages, type LiveState } from "./live";
import { unserved, type Feature } from "./served";
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

/** The word an agent's row says while something holds its next tool call: `frozen`, `paused`, or `held` by a budget; nothing where nothing does. */
export function heldWord(session: LiveSession): string {
  const first = session.holds[0];
  if (first === undefined) return "";
  if (session.holds.some((hold) => hold.freeze)) return "frozen";
  return first.owner === "operator" ? "paused" : "held";
}

/** Running agents something holds at their next tool call, in one repository or in every one. */
export const heldCount = (live: LiveState, repository = "") =>
  [...live.sessions.values()].filter((each) => each.running && each.holds.length > 0 && (repository === "" || each.repository === repository)).length;

/** Whose a hold is, as the page says it to the operator: their pause or freeze, or a budget's hold and why. */
export const holdOwner = (hold: HeldBy) => hold.owner === "operator" ? `your ${hold.freeze ? "freeze" : "pause"}` : `the budget's hold (${hold.reason})`;

/** Whom a hold covers, its member named through the roster: `it`, where it was placed on this agent. */
export function holdReach(live: LiveState, session: LiveSession, hold: HeldBy): string {
  const on = hold.on === session.id ? "it" : called(live, session.repository, hold.on);
  switch (hold.scope) {
    case "self": return `${on} alone`;
    case "agent": return `${on} and its subagents`;
    case "tree": return `${on} and everything it spawned`;
    default: return "every agent of the repository";
  }
}

/** When a hold was placed and when it lifts by itself, where it does. */
export const holdTimes = (hold: HeldBy, now = Date.now()) =>
  [hold.since === null ? "" : `since ${clock(hold.since)} (${ago(hold.since, now)} ago)`, hold.until === null ? "" : `until ${clock(hold.until)}`].filter((each) => each !== "").join(" · ");

/** Every hold over an agent, one line each, as its row's title reads them. */
export function holdTitle(live: LiveState, session: LiveSession, now = Date.now()): string {
  return session.holds.map((hold) => [hold.said, `${holdOwner(hold)} of ${holdReach(live, session, hold)}`, holdTimes(hold, now), hold.freeze ? "frozen: its commands stopped and its turn interrupted" : ""]
    .filter((each) => each !== "").join(" · ")).join("\n");
}

/** Whether its hook holds a call it made now, a call it began before the hold runs on, or it is idle until it makes one. */
export function heldCall(session: LiveSession, now = Date.now()): string {
  if (session.held_since !== null) return `its hook holds the call it made at ${clock(session.held_since)} (${ago(session.held_since, now)} ago)`;
  const calling = session.activity.calling;
  if (calling !== "" && heldWord(session) === "frozen") return `the ${calling} call it was making is stopped with its commands until you resume it`;
  if (calling !== "") return `the ${calling} call it began before the hold runs on to its end; its next one is held`;
  return "no call of its waits now: it is idle, and its next one is held";
}

/** The pause a resume on this agent lifts: the one placed on it, over its tree where that is how it was placed. */
export function ownPause(session: LiveSession): Reach {
  const placed = session.holds.filter((hold) => hold.owner === "operator" && hold.on === session.id);
  return { kind: "agent", repository: session.repository, member: session.id, tree: placed.length > 0 && placed.every((hold) => hold.scope === "tree") };
}

export type Flag = { key: string; text: string };

/** What an agent needs from the operator: held at its next call, quiet, long idle, a review waiting, unread words to them, mail it has not taken, a contested hold. */
export function attention(live: LiveState, roots: ReviewRoot[], rows: ReviewSummary[], session: LiveSession, now = Date.now()): Flag[] {
  const flags: Flag[] = [];
  const held = session.holds[0];
  if (session.running && held !== undefined) {
    const more = session.holds.length > 1 ? ` · ${plural(session.holds.length - 1, "more hold")}` : "";
    flags.push({ key: "held", text: `⏸ ${held.said}${held.since === null ? "" : ` for ${ago(held.since, now)}`}${heldWord(session) === "frozen" ? " · frozen" : ""}${more}` });
  }
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
  for (const hold of session.running ? session.holds : []) {
    const times = holdTimes(hold, now);
    out.push({ t: "msg", key: `hold:${hold.owner}:${hold.reason}:${hold.scope}:${hold.on}`, tone: "warn", text: `⏸ ${hold.said}: ${holdOwner(hold)} of ${holdReach(live, session, hold)}${times !== "" ? ` · ${times}` : ""}${hold.freeze ? " · frozen: its commands stopped and its turn interrupted" : ""}` });
  }
  if (session.running && session.holds.length > 0) out.push({ t: "msg", key: "held-call", tone: "muted", text: `${heldCall(session, now)}; Space a u resumes it.` });
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

/** The operator's working verbs, one level down, each with the supervision it needs from the dashboard's server. */
export const VERBS: { command: string; what: string; needs?: Feature }[] = [
  { command: ":msg <agent> <text>", what: "write to any member, session or subagent; c does it for the one selected" },
  { command: ":broadcast <text>", what: "one post to every working member of the repository, each woken" },
  { command: ":reply <text>", what: "reply in the thread of the message under the cursor (r)", needs: "reply-thread" },
  { command: ":notice <text>", what: "a standing notice every session reads at the head of each prompt until you withdraw it (:unnotice)", needs: "notices" },
  { command: ":redirect <agent> <text>", what: "refuse its next tool call with your words as the reason", needs: "redirect" },
  { command: ":describe <text>", what: "say what you are on; agents read it in coordination_peers", needs: "describe" },
  { command: ":lock <path>", what: "hold a path; an agent writing under it parks a review for you", needs: "claims" },
  { command: ":release <path>", what: "give a hold back; only what you hold", needs: "claims" },
  { command: ":rename <agent> <name>", what: "call a session or subagent something else; its id still reaches it", needs: "rename" },
  { command: ":pause, :freeze, :resume [tree|repo|all]", what: "hold an agent, its tree, the repository or every one at the next tool call; a freeze also stops their commands", needs: "pause" },
  { command: ":read, x, X", what: "mark your inbox read, one or all", needs: "inbox-read" },
];

/** The operator's own page in one repository: what was sent to them, what they sent, and their verbs. */
export function youBuffer(live: LiveState, repository: LiveRepository): Buffer {
  const out = new Rows();
  out.push({ t: "sec", key: "you", text: `you in ${repository.name}`, sub: "a peer like any agent, reached at user" });
  const row = [...live.users.values()].find((each) => each.repository === repository.key);
  out.push({ t: "msg", key: "doing", tone: row?.description ? "" : "muted", text: row?.description ? `you are on: ${row.description}` : "You have not said what you are on (:describe); agents read it in coordination_peers." });
  for (const claim of row?.holding ?? []) out.push({ t: "msg", key: `held:${claim}`, tone: row?.contested.includes(claim) ? "warn" : "", text: `you hold ${claim}${row?.contested.includes(claim) ? " · held by another too" : ""}` });
  for (const notice of row?.notices ?? []) out.push({ t: "msg", key: `notice:${notice.id}`, tone: "", text: `notice ${notice.id}: ${notice.text}` });
  const inbox = inboxOf(live).filter((each) => each.repository === repository.key);
  out.push({ t: "sec", key: "to-you", text: `to you · ${inbox.filter((each) => each.waiting).length} unread of ${inbox.length}`, sub: "gi opens the inbox" });
  for (const each of inbox.slice(0, 6)) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: each.waiting });
  const sent = repositoryMessages(live, repository.key).filter((each) => each.sender === "user").slice(-5);
  out.push({ t: "sec", key: "sent", text: `what you sent lately · ${sent.length}`, sub: "" });
  for (const each of sent) out.push({ t: "mail", key: `mail:${each.key}`, m: each, unread: false });
  out.push({ t: "sec", key: "verbs", text: "your verbs", sub: "one level down: the command line, Space p, and the finder (Space fc)" });
  VERBS.forEach(({ command, what, needs }, at) => out.push({ t: "verb", key: `verb:${at}`, command, what, server: needs === undefined ? "works" : unserved(live.served, needs) || "works" }));
  return out.buffer();
}

/** One line of what the stream moved, as a repository's page logs it. */
export type LogLine = { at: string; text: string; repository: string };

/** A repository's page: its members counted, every message between them, and what the stream moved. */
export function repoBuffer(live: LiveState, repository: LiveRepository, waiting: number, log: LogLine[]): Buffer {
  const out = new Rows();
  const here = membersOf(live, repository.key);
  const held = heldCount(live, repository.key);
  out.push({ t: "sec", key: "repo", text: repository.name, sub: `${here.filter((each) => each.running).length} working${held > 0 ? ` · ⏸${held} held` : ""} · ${here.filter((each) => !each.running).length} stopped · ${waiting} ${waiting === 1 ? "review waits" : "reviews wait"} on you` });
  out.push({ t: "kv", key: "repository", k: "repository", v: repository.repository });
  out.push({ t: "kv", key: "checkout", k: "checkout", v: repository.checkout });
  const messages = repositoryMessages(live, repository.key);
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

/** A message's sender and recipient as a reader calls them; a bare prompt a runtime was woken with comes from `prompt`, not from whoever sent it. */
export function mailHeads(live: LiveState, message: LiveMessage): { from: string; to: string } {
  const from = message.prompt ? "prompt" : message.sender === "" ? message.door : called(live, message.repository, message.sender);
  return { from, to: called(live, message.repository, message.recipient) };
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
