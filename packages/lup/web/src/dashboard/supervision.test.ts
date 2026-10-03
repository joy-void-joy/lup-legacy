import { describe, expect, test } from "bun:test";
import type { LiveMessage, LiveSession, ReviewSummary } from "../generated/views";
import { applied, NO_KEYS, UNSAID } from "./live";
import { activityBrief, attention, callSummary, inboxOf, inRepository, standing, treeItems, unreadCount } from "./supervision";

const now = Date.parse("2026-09-29T12:00:00Z");
const minutesAgo = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
const repository = { key: "r1", name: "lup", repository: "/src/lup.git", checkout: "/src/lup.git/tree/dev" };
const root = { id: "root", path: "/src/lup.git/tree/dev", repository: "/src/lup.git", repository_name: "lup" };

function session(id: string, fields: Partial<LiveSession> = {}): LiveSession {
  return {
    key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name: id, doing: "", task: "", running: true, worktree: "", holding: [], contested: [],
    delivery: "hook", wake: "claude", arrived: null, heard: minutesAgo(1), summary: "", error: "", waiting: 0,
    activity: { said: "", calling: "", arguments: {}, at: minutesAgo(1), transcript: "" }, ...fields,
  };
}

function review(key: string, asker: string): ReviewSummary {
  return {
    key, root_id: "root", id: key, state: "pending", requester: asker, reason: "", title: key, paths: [], total_files: 0, operation: "Bash", rule: "",
    created: minutesAgo(5), answerable: true, unanswerable: "", stale: [], said: 0, target: "", session: asker, settled: null, archived: false,
  };
}

function message(id: string, fields: Partial<LiveMessage> = {}): LiveMessage {
  return { key: `r1/${id}`, repository: "r1", id, at: 0, sender: "lead", recipient: "user", recipient_kind: "user", text: id, door: "agent", redirect: false, in_reply_to: "", sent_at: minutesAgo(2), waiting: true, ...fields };
}

const state = (sessions: LiveSession[], messages: LiveMessage[] = [], reviews: ReviewSummary[] = []) => applied(null, {
  cursor: "c",
  event: { type: "snapshot", repositories: [repository], sessions, messages, extents: [], reviews: { roots: [root], reviews, errors: [], history: 0 }, code: UNSAID, keys: NO_KEYS },
});

describe("who is here, and what each needs", () => {
  test("an agent is working, idle, quiet after ten minutes on one call, or stopped", () => {
    expect(standing(session("a", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(2), transcript: "" } }), now)).toBe("working");
    expect(standing(session("a", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(12), transcript: "" } }), now)).toBe("quiet");
    expect(standing(session("a"), now)).toBe("idle");
    expect(standing(session("a", { running: false }), now)).toBe("stopped");
  });

  test("what it is doing reads its call in one line, from the call's own words", () => {
    expect(callSummary({ command: "uv run pytest", description: "Run the tests" })).toBe("Run the tests");
    expect(callSummary({ file_path: "/a.py" })).toBe("/a.py");
    expect(activityBrief(session("a", { activity: { said: "", calling: "Read", arguments: { file_path: "a.py" }, at: minutesAgo(1), transcript: "" } }), now)).toBe("calling Read · a.py");
    expect(activityBrief(session("a", { running: false, summary: "Handed back." }), now)).toBe("Handed back.");
  });

  test("an agent needs the operator when quiet, waited on, or written from", () => {
    const quiet = session("lead", { activity: { said: "", calling: "Bash", arguments: {}, at: minutesAgo(20), transcript: "" }, contested: ["at /x"] });
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

  test("a path reads relative to its repository's checkouts", () => {
    expect(inRepository("/src/lup.git/tree/dev/a.py", repository)).toBe("tree/dev/a.py");
    expect(inRepository("/elsewhere/a.py", repository)).toBe("/elsewhere/a.py");
  });
});
