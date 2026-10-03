// The command line: `:` runs a command, Tab completes its name and its
// argument, ↑ and ↓ recall earlier ones; `/` searches the focused window on the
// same line. A command whose supervision this dashboard's server does not
// serve says so and names the route, the way a key bound to it does.
import type { Dashboard } from "./dashboard";
import type { LiveSession } from "../generated/views";
import { cancelSearch, confirmSearch, copyLink, gotoLine, memberHere, moveReview, previewSearch, quit, replyHere, repositoryHere, rowHere, setPaneView, split, toggleFull, toggleWhole } from "./editor";
import { openFinder, PICKERS } from "./finder";
import { CATALOG } from "./keys";
import type { Feature } from "./served";
import { VIEWS, type View } from "./state";
import { counterpart, inboxOf, parentOf, parseCaps } from "./supervision";

export type Command = {
  name: string;
  description: string;
  /** What Tab completes after the name, where it takes an argument. */
  args?: (d: Dashboard) => string[];
  /** Whether it takes free text after the name. */
  takes?: boolean;
  alias?: string[];
  /** The supervision it needs from the dashboard's server, where it needs any. */
  needs?: Feature;
  run: (d: Dashboard, arg: string, bang: boolean) => void;
};

const agentNames = (d: Dashboard) => [...new Set([...(d.state.live?.sessions.values() ?? [])].map((each) => each.name || each.id))];

/** The agent a command names first, where its first word names one exactly; else the one in view. */
function agentAndText(d: Dashboard, arg: string): { session: ReturnType<typeof memberHere>; text: string } {
  const [first = "", ...rest] = arg.trim().split(/\s+/);
  const live = [...(d.state.live?.sessions.values() ?? [])].sort((left, right) => Number(right.running) - Number(left.running));
  const named = live.find((each) => each.name === first || each.id === first);
  return named !== undefined ? { session: named, text: rest.join(" ") } : { session: memberHere(d), text: arg.trim() };
}

function resolveMember(d: Dashboard, spelling: string) {
  if (spelling === "" || spelling === ".") return memberHere(d);
  const live = [...(d.state.live?.sessions.values() ?? [])].sort((left, right) => Number(right.running) - Number(left.running) || Date.parse(right.heard ?? "0") - Date.parse(left.heard ?? "0"));
  return live.find((each) => each.name === spelling || each.id === spelling || each.key === spelling) ?? live.find((each) => each.name.startsWith(spelling));
}

const SETTINGS: Record<string, (d: Dashboard) => string> = {
  stopped: (d) => { d.set({ showStopped: true }); return "the tree shows every stopped agent"; },
  nostopped: (d) => { d.set({ showStopped: false, stoppedOpen: new Set() }); return "stopped agents fold into one row per repository"; },
  hlsearch: (d) => { d.set((state) => ({ search: { ...state.search, lit: true } })); return "search matches are highlighted"; },
  nohlsearch: (d) => { d.set((state) => ({ search: { ...state.search, lit: false } })); return "search matches are not highlighted"; },
  advance: (d) => { d.set((state) => ({ settings: { ...state.settings, advance: true } })); return "advance after a decision"; },
  noadvance: (d) => { d.set((state) => ({ settings: { ...state.settings, advance: false } })); return "stay on the answered request"; },
  wrap: (d) => { d.set((state) => ({ settings: { ...state.settings, wrap: true } })); return "long lines wrap"; },
  nowrap: (d) => { d.set((state) => ({ settings: { ...state.settings, wrap: false } })); return "long lines scroll sideways"; },
  number: (d) => { d.set((state) => ({ settings: { ...state.settings, numbers: true } })); return "line numbers shown"; },
  nonumber: (d) => { d.set((state) => ({ settings: { ...state.settings, numbers: false } })); return "line numbers hidden"; },
  "scope=review": (d) => { const open = d.current(); if (open !== null) d.setUi(open.row.key, () => ({ full: false })); return "needs review: automatic files folded, new exceptions only"; },
  "scope=full": (d) => { const open = d.current(); if (open !== null) d.setUi(open.row.key, () => ({ full: true })); return "full operation: every file, and existing exceptions"; },
  "tree=all": (d) => { d.set({ tree: "all" }); return "the tree shows every agent"; },
  "tree=attention": (d) => { d.set({ tree: "attention" }); return "the tree shows the agents that need you"; },
  "tree=reviews": (d) => { d.set({ tree: "reviews" }); return "the tree shows only the reviews waiting, as the old queue did"; },
  ...Object.fromEntries([12, 13, 14, 15, 16].map((size) => [`size=${size}`, (d: Dashboard) => { d.set((state) => ({ settings: { ...state.settings, size } })); return `font size ${size}px`; }])),
};

function set(d: Dashboard, arg: string): void {
  if (arg === "") {
    const { settings, showStopped, search, tree } = d.state;
    d.say(`:set ${showStopped ? "" : "no"}stopped ${settings.advance ? "" : "no"}advance ${settings.wrap ? "" : "no"}wrap ${settings.numbers ? "" : "no"}number ${search.lit ? "" : "no"}hlsearch tree=${tree} size=${settings.size}`);
    return;
  }
  const setter = SETTINGS[arg];
  if (setter === undefined) { d.say(`E518: Unknown option: ${arg}`, "err"); return; }
  d.say(`:set ${arg} — ${setter(d)}`);
}

/** `:map`: list what the person's table and this tab did; `:map {action}` names its keys; `:map {action} {keys…}` tries keys in this tab. */
async function map(d: Dashboard, arg: string): Promise<void> {
  const [name = "", ...keys] = arg.trim().split(/\s+/).filter((word) => word !== "");
  if (name === "") { d.set({ float: { kind: "keys" } }); return; }
  const action = CATALOG.actions.find((each) => each.name === name);
  if (keys.length === 0) {
    if (action === undefined) { d.say(`E: no action is called ${name}`, "err"); return; }
    const bound = d.keymap.of(name);
    d.say(`${name}: ${d.keymap.spoken(name)}${bound !== undefined && bound.origin !== "lup" ? ` (yours; lup's is ${action.keys.join(", ") || "unbound"})` : ""}`);
    return;
  }
  const lines = [...d.state.keyLines, { action: name, keys }];
  const tried = await d.tryLines(lines);
  if (tried.refused !== "") { d.say(`E: the dashboard could not check it: ${tried.refused}`, "err"); return; }
  const refused = d.state.tried?.report.refused.find((each) => each.origin === "tab" && each.action === name);
  if (refused !== undefined) {
    await d.tryLines(lines.slice(0, -1));
    d.say(`E: :map ${name} ${keys.join(" ")} refused: \`${refused.what}\` ${refused.why}${refused.way !== "" ? ` — ${refused.way}` : ""}`, "err");
    return;
  }
  d.say(`${name} is ${d.keymap.spoken(name)} in this tab; :map shows the line to write down, :mapwrite writes it`);
}

async function unmap(d: Dashboard, arg: string): Promise<void> {
  const keys = arg.trim();
  if (keys === "") { d.say("E: :unmap {keys}", "err"); return; }
  const lines = [...d.state.keyLines, { action: "", keys: [keys] }];
  const tried = await d.tryLines(lines);
  if (tried.refused !== "") { d.say(`E: the dashboard could not check it: ${tried.refused}`, "err"); return; }
  const refused = d.state.tried?.report.refused.find((each) => each.origin === "tab" && each.action === "" && each.what === keys);
  if (refused !== undefined) {
    await d.tryLines(lines.slice(0, -1));
    d.say(`E31: No such mapping: ${keys}${refused.why !== "no action runs on it" ? ` (${refused.why})` : ""}`, "err");
    return;
  }
  d.say(`${keys} no longer runs what it did, in this tab`);
}

/** Act on the agent a command names, or the one in view. */
function onAgent(d: Dashboard, arg: string, act: (session: LiveSession) => void): void {
  const session = resolveMember(d, arg.trim());
  if (session === undefined) { d.say(`E: no agent answers to ${arg.trim() || "nothing in view"}`, "err"); return; }
  act(session);
}

/** `:reply <text>`: answer the message or post under the cursor with the text, or open its box where none is given. */
function replyHereWith(d: Dashboard, text: string): void {
  const row = rowHere(d);
  if (text.trim() === "") { replyHere(d); return; }
  if (row?.t === "post") {
    const discussion = d.discussion();
    if (discussion !== undefined) { d.set({ threadReply: row.post.id }); void d.post(discussion, text); }
    return;
  }
  if (row?.t !== "mail") { d.say("E: :reply answers the message under the cursor; put it on one first", "err"); return; }
  const live = d.state.live;
  const session = live === null ? undefined : counterpart(live, row.m);
  if (session === undefined) { d.say("E: whoever wrote it is not on the roster this page holds", "err"); return; }
  void d.sendTo(session, text, { in_reply_to: row.m.post || row.m.id });
}

/** `:read`: the message to you under the cursor; `:read all`: every one waiting. */
function readWith(d: Dashboard, arg: string): void {
  const live = d.state.live;
  if (live === null) return;
  if (arg === "all") { void d.markRead(inboxOf(live)); return; }
  const row = rowHere(d);
  if (row?.t === "mail") void d.markRead([row.m]);
  else d.say("E: :read marks the message under the cursor; :read all marks every one", "err");
}

/** The ids of the notices standing over the repository here, for Tab. */
const noticeIds = (d: Dashboard) => [...(d.state.live?.users.values() ?? [])].filter((row) => row.repository === repositoryHere(d)).flatMap((row) => row.notices.map((notice) => notice.id));

export const COMMANDS: Command[] = [
  { name: "approve", description: "approve the open review (Ctrl+Enter)", run: (d) => d.answer("approve") },
  { name: "decline", description: "decline the open review (Alt+Delete)", run: (d) => d.answer("decline") },
  { name: "send", description: "send the note without deciding, or the message box (Alt+Enter)", run: (d) => d.centerKind() === "review" ? d.answer("remark") : d.sendBox() },
  { name: "next", description: "next review waiting on you", run: (d) => moveReview(d, 1, "normal") },
  { name: "prev", description: "previous review", run: (d) => moveReview(d, -1, "normal") },
  { name: "review", description: "open a review by its id", args: (d) => d.rows().map((row) => row.id), run: (d, arg) => {
    const found = d.rows().filter((row) => row.id.startsWith(arg));
    const only = found.length === 1 ? found[0] : undefined;
    if (only !== undefined) d.openReview(only.key, { mode: "normal" });
    else d.say(found.length > 0 ? `E: ${found.length} reviews start with ${arg}` : `E: no review ${arg}`, "err");
  } },
  { name: "agent", description: "open an agent by name or id", args: agentNames, run: (d, arg) => { const session = resolveMember(d, arg.trim()); if (session !== undefined) d.openOther("member", session.key); else d.say(`E: no agent answers to ${arg}`, "err"); } },
  { name: "you", description: "your own row: what was sent to you, what you sent, your verbs", run: (d) => { const repository = repositoryHere(d); if (repository !== "") d.openOther("you", repository); } },
  { name: "tab", description: "switch view", args: () => VIEWS, run: (d, arg) => view(d, arg) },
  { name: "supervise", description: "every agent, and what waits on you", alias: ["reviews", "sessions", "agents"], run: (d) => d.setView("supervise") },
  { name: "history", description: "requests that left the queue", run: (d) => d.setView("history") },
  { name: "inbox", description: "everything addressed to you (gi)", run: (d) => d.setView("inbox") },
  { name: "threads", description: "the discussions: every conversation between the agents, and you", alias: ["discussions"], run: (d) => d.setView("threads") },
  { name: "post", description: "post to everyone in the discussion here: :post <text>", takes: true, needs: "thread-post", run: (d, arg) => {
    const discussion = d.discussion();
    if (d.centerKind() !== "thread" || discussion === undefined) { d.say("E: :post writes into a discussion; open one first (:threads)", "err"); return; }
    void d.post(discussion, arg);
  } },
  { name: "setup", description: "each repository's setup", run: (d) => d.setView("setup") },
  { name: "tree", description: "the tree shows every agent, those that need you, or only reviews", args: () => ["all", "attention", "reviews"], run: (d, arg) => set(d, `tree=${arg || "all"}`) },
  { name: "msg", description: "write to an agent: :msg [agent] <text>", args: agentNames, takes: true, run: (d, arg) => { const { session, text } = agentAndText(d, arg); if (session === undefined) d.say("E: :msg <agent> <text>", "err"); else void d.sendTo(session, text); } },
  { name: "broadcast", description: "one message to every working member of the repository here", takes: true, run: (d, arg) => void d.broadcast(repositoryHere(d), arg) },
  { name: "ask-parent", description: "ask an agent's parent session about it", args: agentNames, takes: true, run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    const live = d.state.live;
    const parent = session === undefined || live === null ? undefined : parentOf(live, session);
    if (session === undefined || parent === undefined) { d.say("E: :ask-parent <subagent> [text]: it needs a parent session on the roster", "err"); return; }
    void d.sendTo(parent, text !== "" ? text : `What is ${session.name || session.id} doing?`);
  } },
  { name: "wake", description: "wake an agent to read its mailbox, or to look where nothing waits", args: agentNames, needs: "bare-wake", run: (d, arg) => onAgent(d, arg, (session) => void d.wake(session)) },
  { name: "reply", description: "reply in the thread of the message under the cursor: :reply <text>", takes: true, needs: "reply-thread", run: (d, arg) => replyHereWith(d, arg) },
  { name: "notice", description: "a standing notice every session here reads at the head of each prompt: :notice <text>", takes: true, needs: "notices", run: (d, arg) => void d.notice(repositoryHere(d), arg) },
  { name: "unnotice", description: "take a standing notice down: :unnotice <id>", args: noticeIds, needs: "notices", run: (d, arg) => void d.withdraw(repositoryHere(d), arg.trim()) },
  { name: "redirect", description: "refuse an agent's next tool call with your words: :redirect <agent> <text>", args: agentNames, takes: true, needs: "redirect", run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    if (session === undefined) d.say("E: :redirect <agent> <text>", "err"); else void d.sendTo(session, text, { redirect: true });
  } },
  { name: "describe", description: "say what you are on; agents read it in coordination_peers", takes: true, needs: "describe", run: (d, arg) => void d.describe(arg.trim()) },
  { name: "lock", description: "hold a path as the operator: agents writing under it park a review for you", takes: true, needs: "claims", run: (d, arg) => void d.claim(repositoryHere(d), arg, true) },
  { name: "release", description: "give back a path you hold", takes: true, needs: "claims", run: (d, arg) => void d.claim(repositoryHere(d), arg, false) },
  { name: "rename", description: "rename an agent: :rename [agent] <name>", args: agentNames, takes: true, needs: "rename", run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    if (session === undefined) d.say("E: :rename [agent] <name>", "err"); else void d.rename(session, text);
  } },
  { name: "read", description: "mark the message under the cursor read; :read all for every one", args: () => ["all"], needs: "inbox-read", run: (d, arg) => readWith(d, arg.trim()) },
  { name: "nudge", description: "interrupt an agent's turn with a message: :nudge [agent] <text>", args: agentNames, takes: true, needs: "interrupt", run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    if (session === undefined) d.say("E: :nudge [agent] <text>", "err"); else void d.interrupt(session, text);
  } },
  { name: "interrupt", description: "interrupt an agent's turn with the standard words", args: agentNames, needs: "interrupt", run: (d, arg) => onAgent(d, arg, (session) => void d.interrupt(session, "")) },
  { name: "transcript", description: "an agent's whole transcript, live (T)", args: agentNames, needs: "transcript", run: (d, arg) => onAgent(d, arg, (session) => void d.openTranscript(session)) },
  { name: "stop", description: "stop an agent's runtime; :stop! confirms", args: agentNames, needs: "stop", run: (d, arg, bang) => onAgent(d, arg, (session) => void d.stopRuntime(session, bang)) },
  { name: "turtle", description: "the turtle: every account under its slower limits; :turtle on or off, or flip it", args: () => ["on", "off"], needs: "budgets", run: (d, arg) => {
    const word = arg.trim();
    if (word !== "" && word !== "on" && word !== "off") { d.say("E: :turtle [on|off]", "err"); return; }
    void d.turtle(word === "" ? undefined : word === "on");
  } },
  { name: "priority", description: "an agent's priority under its account's limits: :priority [agent] high|normal|low", args: () => ["high", "normal", "low"], takes: true, needs: "budgets", run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    const priority = text.trim();
    if (session === undefined || (priority !== "high" && priority !== "normal" && priority !== "low")) { d.say("E: :priority [agent] high|normal|low", "err"); return; }
    void d.settleBudget(session, { priority, caps: null });
  } },
  { name: "cap", description: "an agent's caps, a rate per hour and a total: :cap [agent] $2/h $10, 500k/h 2M; nothing clears them", args: agentNames, takes: true, needs: "budgets", run: (d, arg) => {
    const { session, text } = agentAndText(d, arg);
    if (session === undefined) { d.say("E: :cap [agent] <caps>", "err"); return; }
    const caps = parseCaps(text);
    if (typeof caps === "string") { d.say(`E: ${caps}`, "err"); return; }
    void d.settleBudget(session, { priority: null, caps });
  } },
  { name: "switch", description: "move this repository's sessions onto a profile: :switch <profile> [claude|codex]", args: (d) => [...new Set((d.state.live?.budget.accounts ?? []).map((each) => each.account.profile))], takes: true, needs: "profiles", run: (d, arg) => {
    const [profile = "", runtime = "claude"] = arg.trim().split(/\s+/);
    if (profile === "" || (runtime !== "claude" && runtime !== "codex")) { d.say("E: :switch <profile> [claude|codex]", "err"); return; }
    const repository = repositoryHere(d);
    if (repository === "") { d.say("E: :switch acts on a repository: open one of its agents first", "err"); return; }
    void d.switchProfile(repository, profile, runtime);
  } },
  { name: "older", description: "load older requests into History", run: (d) => void d.loadOlder() },
  { name: "earlier", description: "load earlier messages: an older page of the mail record here", alias: ["ea"], run: (d) => void d.loadEarlier(repositoryHere(d)) },
  { name: "map", description: "your keys: :map lists them; :map {action} {keys} tries one in this tab", args: () => CATALOG.actions.map((action) => action.name), takes: true, run: (d, arg) => void map(d, arg) },
  { name: "unmap", description: "take a key off whatever action holds it, in this tab", takes: true, run: (d, arg) => void unmap(d, arg) },
  { name: "mapwrite", description: "write this tab's :map lines to [dashboard.keys] in your lup config, its comments kept", run: (d) => void d.writeLines() },
  { name: "nohlsearch", description: "clear the search highlight until the next search", alias: ["noh", "nohl"], run: (d) => { d.set((state) => ({ search: { ...state.search, lit: false } })); d.say(""); } },
  { name: "set", description: "an option: [no]advance, [no]wrap, [no]number, [no]stopped, [no]hlsearch, tree=…, scope=review|full, size=N", args: () => Object.keys(SETTINGS), run: (d, arg) => set(d, arg.trim()) },
  { name: "view", description: "what this window shows: diff, before, after, raw", args: () => ["diff", "before", "after", "raw"], run: (d, arg) => setPaneView(d, arg.trim()) },
  { name: "whole", description: "whole file with the changes marked in place (f)", run: (d) => toggleWhole(d) },
  { name: "full", description: "toggle the full operation (F)", run: (d) => toggleFull(d) },
  { name: "vsplit", description: "before | after side by side", alias: ["vs", "vsp"], run: (d) => split(d, "v") },
  { name: "split", description: "a second window below", alias: ["sp"], run: (d) => split(d, "s") },
  { name: "only", description: "close the other window", alias: ["on"], run: (d) => split(d, "") },
  { name: "q", description: "close the float or the split you are in", alias: ["quit", "close", "clo"], run: (d) => quit(d) },
  { name: "context", description: "full context: tool input and record, or an agent's whole row (I)", run: (d) => d.set({ float: { kind: "context" } }) },
  { name: "link", description: "copy a link to this review", run: (d) => copyLink(d) },
  { name: "checkouts", description: "the checkout queues this page watches", run: (d) => d.set({ float: { kind: "checkouts" } }) },
  { name: "reconnect", description: "reconnect the stream", run: (d) => d.reconnect() },
  { name: "messages", description: "what this page said, oldest first", alias: ["mes"], run: (d) => d.set({ float: { kind: "messages" } }) },
  { name: "help", description: "every key (?)", alias: ["h"], run: (d) => d.set({ float: { kind: "help" } }) },
  { name: "contrast", description: "the palette, its sources and contrast", run: (d) => d.set({ float: { kind: "contrast" } }) },
  { name: "find", description: `a finder: ${Object.keys(PICKERS).join(", ")}`, args: () => Object.keys(PICKERS), run: (d, arg) => openFinder(d, arg.trim() || "agents") },
];

function view(d: Dashboard, arg: string): void {
  const named: Record<string, View> = { reviews: "supervise", sessions: "supervise", agents: "supervise" };
  const wanted = named[arg] ?? arg;
  if (!VIEWS.includes(wanted as View)) { d.say(`E: no view ${arg}`, "err"); return; }
  d.setView(wanted as View);
}

const HISTORY = new WeakMap<Dashboard, string[]>();
const historyOf = (d: Dashboard) => HISTORY.get(d) ?? HISTORY.set(d, []).get(d) ?? [];

/** Run one command line as typed: a number goes to that line, a write is refused, anything else is looked up. */
export function runCommand(d: Dashboard, typed: string): void {
  const line = typed.trim();
  if (line === "") return;
  historyOf(d).push(line);
  const [spelled = ""] = line.split(/\s+/);
  if (/^\d+$/.test(spelled)) { gotoLine(d, Number(spelled)); return; }
  const name = spelled.replace(/!$/, "");
  if (["w", "wq", "x", "wa", "wqa", "xa"].includes(name)) {
    d.say(`E: :${name} writes nothing here. An answer is deliberate: :approve, :decline or :send (or their chords), never a save reflex.`, "err");
    return;
  }
  const command = COMMANDS.find((each) => each.name === name) ?? COMMANDS.find((each) => each.alias?.includes(name) === true);
  if (command === undefined) { d.say(`E492: Not an editor command: ${line}`, "err"); return; }
  command.run(d, line.slice(spelled.length).trim(), spelled.endsWith("!"));
}

/** What Tab offers for what was typed: command names, or the arguments of the command named. */
export function completions(d: Dashboard, text: string): { value: string; description: string }[] {
  const [name = "", ...rest] = text.split(" ");
  if (rest.length === 0) return COMMANDS.filter((command) => command.name.startsWith(name)).map((command) => ({ value: command.name, description: command.description }));
  const command = COMMANDS.find((each) => each.name === name);
  if (command?.args === undefined) return [];
  const typed = rest.join(" ");
  return command.args(d).filter((arg) => arg.startsWith(typed)).map((arg) => ({ value: `${name} ${arg}`, description: "" }));
}

/** Open the command line: `:` for a command, `/` for a search of the focused window. */
export function openCommand(d: Dashboard, prefill: string, prefix: ":" | "/" = ":"): void {
  d.set({ float: null, cmdline: { prefix, text: prefill, wild: [], wildAt: -1, base: "", history: historyOf(d).length }, message: { text: "", tone: "" } });
}

function close(d: Dashboard): void {
  d.set({ cmdline: null });
  d.focusWin(d.state.focus === "composer" ? "editor" : d.state.focus);
}

/** What the operator typed on the command line; a search moves as each key is typed. */
export function commandInput(d: Dashboard, text: string): void {
  const line = d.state.cmdline;
  if (line === null) return;
  d.set({ cmdline: { ...line, text, wild: [], wildAt: -1 } });
  if (line.prefix === "/") previewSearch(d, text);
}

function complete(d: Dashboard, delta: 1 | -1): void {
  const line = d.state.cmdline;
  if (line === null) return;
  const fresh = line.wild.length === 0;
  const wild = fresh ? completions(d, line.text).map((each) => each.value) : line.wild;
  if (wild.length === 0) return;
  const wildAt = ((fresh ? -1 : line.wildAt) + delta + wild.length) % wild.length;
  d.set({ cmdline: { ...line, wild, wildAt, base: fresh ? line.text : line.base, text: wild[wildAt] ?? line.text } });
}

/** The command line's keys: Enter runs, Esc leaves, Tab completes, ↑ and ↓ recall, Backspace on nothing leaves. */
export function commandKey(d: Dashboard, key: string, event: KeyboardEvent): void {
  const line = d.state.cmdline;
  if (line === null) return;
  const take = () => event.preventDefault();
  if (line.prefix === "/") {
    switch (key) {
      case "<Esc>": case "<C-[>": take(); close(d); cancelSearch(d); return;
      case "<Enter>": take(); close(d); confirmSearch(d, line.text); return;
      case "<BS>": if (line.text === "") { take(); close(d); cancelSearch(d); } return;
      default: return;
    }
  }
  const history = historyOf(d);
  switch (key) {
    case "<Esc>": case "<C-[>": take(); close(d); return;
    case "<Enter>": take(); close(d); runCommand(d, line.text); return;
    case "<Tab>": take(); complete(d, 1); return;
    case "<S-Tab>": take(); complete(d, -1); return;
    case "<Up>": {
      take();
      const at = Math.max(0, line.history - 1);
      d.set({ cmdline: { ...line, history: at, text: history[at] ?? line.text, wild: [], wildAt: -1 } });
      return;
    }
    case "<Down>": {
      take();
      const at = Math.min(history.length, line.history + 1);
      d.set({ cmdline: { ...line, history: at, text: history[at] ?? "", wild: [], wildAt: -1 } });
      return;
    }
    case "<BS>": if (line.text === "") { take(); close(d); } return;
    default:
  }
}
