import { describe, expect, test } from "bun:test";
import type { AccountMeter, AgentMeter, BudgetView, HeldBy, LiveMessage, LiveSession, ReviewSummary } from "../generated/views";
import { applied, NO_BUDGET, NO_KEYS, UNSAID } from "./live";
import {
  activityBrief, attention, callSummary, capsText, fullest, heldCall, heldCount, heldWord, holdOwner, holdReach, holdTitle, inboxOf, inRepository, mailHeads, metered, money, NO_CAPS,
  ownPause, parseCaps, repoBuffer, spendLine, standing, tokenCount, treeItems, unreadCount, windowAt, wouldHold, youBuffer,
} from "./supervision";
import type { Row } from "./review";

const now = Date.parse("2026-09-29T12:00:00Z");
const minutesAgo = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
const repository = { key: "r1", name: "lup", repository: "/src/lup.git", checkout: "/src/lup.git/tree/dev" };
const root = { id: "root", path: "/src/lup.git/tree/dev", repository: "/src/lup.git", repository_name: "lup" };

function session(id: string, fields: Partial<LiveSession> = {}): LiveSession {
  return {
    key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name: id, doing: "", task: "", running: true, worktree: "", holding: [], contested: [],
    delivery: "hook", wake: "claude", arrived: null, heard: minutesAgo(1), summary: "", error: "", waiting: 0, runtime: "claude", spawned_by: "", process: null,
    holds: [], held_since: null, activity: { said: "", calling: "", arguments: {}, at: minutesAgo(1), transcript: "", recent: [] }, ...fields,
  };
}

function review(key: string, asker: string): ReviewSummary {
  return {
    key, root_id: "root", id: key, state: "pending", requester: asker, reason: "", title: key, paths: [], total_files: 0, operation: "Bash", rule: "",
    created: minutesAgo(5), answerable: true, unanswerable: "", stale: [], said: 0, target: "", session: asker, settled: null, archived: false,
  };
}

function message(id: string, fields: Partial<LiveMessage> = {}): LiveMessage {
  return { key: `r1/${id}`, repository: "r1", id, at: 0, sender: "lead", recipient: "user", recipient_kind: "user", text: id, door: "agent", redirect: false, in_reply_to: "", sent_at: minutesAgo(2), waiting: true, post: id, thread: id, prompt: false, ...fields };
}

const state = (sessions: LiveSession[], messages: LiveMessage[] = [], reviews: ReviewSummary[] = []) => applied(null, {
  cursor: "c",
  event: { type: "snapshot", repositories: [repository], sessions, messages, extents: [], reviews: { roots: [root], reviews, errors: [], history: 0 }, code: UNSAID, keys: NO_KEYS, users: [], served: [], budget: NO_BUDGET },
});

describe("who is here, and what each needs", () => {
  test("an agent is working, idle, quiet after ten minutes on one call, or stopped", () => {
    expect(standing(session("a", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(2), transcript: "", recent: [] } }), now)).toBe("working");
    expect(standing(session("a", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(12), transcript: "", recent: [] } }), now)).toBe("quiet");
    expect(standing(session("a"), now)).toBe("idle");
    expect(standing(session("a", { running: false }), now)).toBe("stopped");
  });

  test("what it is doing reads its call in one line, from the call's own words", () => {
    expect(callSummary({ command: "uv run pytest", description: "Run the tests" })).toBe("Run the tests");
    expect(callSummary({ file_path: "/a.py" })).toBe("/a.py");
    expect(activityBrief(session("a", { activity: { said: "", calling: "Read", arguments: { file_path: "a.py" }, at: minutesAgo(1), transcript: "", recent: [] } }), now)).toBe("calling Read · a.py");
    expect(activityBrief(session("a", { running: false, summary: "Handed back." }), now)).toBe("Handed back.");
  });

  test("an agent needs the operator when quiet, waited on, or written from", () => {
    const quiet = session("lead", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(20), transcript: "", recent: [] }, contested: ["at /x"] });
    const live = state([quiet], [message("m1")], [review("q1", "lead")]);
    expect(attention(live, [root], [review("q1", "lead")], quiet, now).map((flag) => flag.key)).toEqual(["quiet", "asks", "wrote", "contested"]);
    expect(inboxOf(live).map((each) => each.id)).toEqual(["m1"]);
    expect(unreadCount(live)).toBe(1);
  });

  test("the tree nests subagents under their session, reviews under who asked, and folds the stopped", () => {
    const sessions = [session("lead"), session("lead-a1", { parent: "lead", kind: "subagent", name: "scout" }), session("gone", { running: false }), session("idle")];
    const pending = [review("q1", "scout")];
    const live = state(sessions, [], pending);
    const options = { filter: "all" as const, collapsed: new Set<string>(), showStopped: false, stoppedOpen: new Set<string>(), now };
    expect(treeItems(live, [root], pending, options).map((item) => `${item.t}:${item.key}:${item.depth}`)).toEqual([
      "repo:r1:0", "you:r1:1", "member:r1/idle:1", "member:r1/lead:1", "member:r1/lead-a1:2", "review:q1:3", "folded:r1:1",
    ]);
    const shown = treeItems(live, [root], pending, { ...options, showStopped: true });
    expect(shown.some((item) => item.t === "member" && item.key === "r1/gone")).toBe(true);
    const reviews = treeItems(live, [root], pending, { ...options, filter: "reviews" });
    expect(reviews.filter((item) => item.t === "member").map((item) => item.key)).toEqual(["r1/lead", "r1/lead-a1"]);
  });

  test("a held agent says what holds it, whose hold it is and whom it covers, and a resume lifts the pause placed on it", () => {
    const tree: HeldBy = { reason: "paused", owner: "operator", scope: "tree", on: "lead", said: "paused by the operator", since: minutesAgo(3), until: null, freeze: true };
    const budget: HeldBy = { reason: "over-rate", owner: "budget", scope: "self", on: "lead-a1", said: "over its rate", since: minutesAgo(1), until: minutesAgo(-4), freeze: false };
    const lead = session("lead", { holds: [tree] });
    const scout = session("lead-a1", { parent: "lead", kind: "subagent", name: "scout", holds: [tree, budget], held_since: minutesAgo(2) });
    const live = state([lead, scout, session("idle")]);
    expect(heldWord(lead)).toBe("frozen");
    expect(heldWord(session("idle", { holds: [budget] }))).toBe("held");
    expect(heldCount(live)).toBe(2);
    expect(heldCount(state([session("gone", { running: false, holds: [tree] })]))).toBe(0);
    expect(attention(live, [root], [], scout, now).map((flag) => flag.text)).toEqual(["⏸ paused by the operator for 3m · frozen · 1 more hold"]);
    expect(holdReach(live, scout, tree)).toBe("lead and everything it spawned");
    expect(holdReach(live, lead, tree)).toBe("it and everything it spawned");
    expect(holdOwner(budget)).toBe("the budget's hold (over-rate)");
    expect(holdTitle(live, scout, now).split("\n")[1]).toContain("the budget's hold (over-rate) of it alone");
    expect(heldCall(scout, now)).toContain("its hook holds the call it made");
    expect(heldCall(lead, now)).toContain("it is idle");
    expect(ownPause(lead)).toEqual({ kind: "agent", repository: "r1", member: "lead", tree: true });
    expect(ownPause(scout)).toEqual({ kind: "agent", repository: "r1", member: "lead-a1", tree: false });
  });

  test("a resume's bare prompt comes from `prompt`, not from the operator", () => {
    const live = state([session("lead")]);
    expect(mailHeads(live, message("m1", { sender: "user", recipient: "lead", prompt: true }))).toEqual({ from: "prompt", to: "lead" });
    expect(mailHeads(live, message("m2", { sender: "user", recipient: "lead" }))).toEqual({ from: "you", to: "lead" });
  });

  test("a repository's page lists every message between its members, and the operator's row what they sent", () => {
    const live = state([session("lead"), session("scout")], [message("m1", { sender: "lead", recipient: "scout" }), message("m2", { sender: "user", recipient: "lead" })]);
    const mail = (rows: Row[]) => rows.flatMap((row) => row.t === "mail" ? [row.m.id] : []).sort();
    expect(mail(repoBuffer(live, repository, 0, []).rows)).toEqual(["m1", "m2"]);
    expect(mail(youBuffer(live, repository).rows)).toEqual(["m2"]);
  });

  test("a path reads relative to its repository's checkouts", () => {
    expect(inRepository("/src/lup.git/tree/dev/a.py", repository)).toBe("tree/dev/a.py");
    expect(inRepository("/elsewhere/a.py", repository)).toBe("/elsewhere/a.py");
  });
});

describe("the budget's words", () => {
  const meter = (fields: Partial<AgentMeter> = {}): AgentMeter => ({
    session: "r1/lead", account: "claude:work", hour: { usd: 0.42, tokens: 9000 }, total: { usd: 3.1, tokens: 80000 },
    priority: "normal", caps: NO_CAPS, exempt: false, held: null, ...fields,
  });

  test("caps read as the operator types them, and say why where they do not", () => {
    expect(parseCaps("$2/h $10")).toEqual({ ...NO_CAPS, rate_usd: 2, total_usd: 10 });
    expect(parseCaps("500k/h, 2M")).toEqual({ ...NO_CAPS, rate_tokens: 500_000, total_tokens: 2_000_000 });
    expect(parseCaps("")).toEqual(NO_CAPS);
    expect(parseCaps("ten dollars")).toContain("neither a dollar amount");
    expect(parseCaps("$2k")).toContain("mixes dollars");
    expect(capsText({ ...NO_CAPS, rate_usd: 2, total_tokens: 2_000_000 })).toBe("$2.00/h · 2.0M");
  });

  test("an agent's spend says its rate and total, in tokens where nothing priced it", () => {
    expect(spendLine(meter())).toBe("$0.42/h · $3.10");
    expect(spendLine(meter({ hour: { usd: 0, tokens: 12_000 }, total: { usd: 0, tokens: 1_400_000 } }))).toBe("12k tok/h · 1.4M tok");
    expect(spendLine(meter({ hour: { usd: 0, tokens: 0 }, total: { usd: 0, tokens: 0 } }))).toBe("");
    expect(money(250)).toBe("$250");
    expect(tokenCount(950)).toBe("950");
  });

  test("a verdict says what would hold the agent only where this dashboard places no holds", () => {
    const verdict = { key: "r1/lead", cause: "slot" as const, said: "waiting for a slot", until: null };
    expect(wouldHold({ ...NO_BUDGET, holds: true }, verdict)).toBe("");
    expect(wouldHold(NO_BUDGET, verdict)).toBe("would hold: waiting for a slot");
    expect(wouldHold(NO_BUDGET, null)).toBe("");
  });

  test("a window says where even pace stands, and the meter shows the accounts in use first", () => {
    const now = Date.parse("2026-10-05T12:00:00Z");
    const window = { window: { label: "5-hour", utilization_pct: 70, resets_at: "2026-10-05T14:00:00Z", window_hours: 5 }, per_hour: null };
    const at = windowAt(window, now);
    expect(at.even).toBeCloseTo(60);
    expect(at.ahead).toBe(true);
    const account = (key: string, agents: number, windows = [window]): AccountMeter => ({
      account: { runtime: "claude", profile: key }, key, home: "", signed_in: true, windows, read_at: null, error: "",
      limits: { window_ceiling: null, pace: null, ceilings: null, tolerance: null, reserve: null, max_active: null }, said: [], agents, held: 0, exhausted: "",
    });
    const budget: BudgetView = { accounts: [account("b", 0), account("a", 2), account("idle", 0, [])], agents: [], turtle: false, telemetry: true, refused: "", holds: true };
    expect(metered(budget).map((each) => each.key)).toEqual(["a", "b"]);
    expect(fullest(account("x", 0))?.window.utilization_pct).toBe(70);
  });
});
