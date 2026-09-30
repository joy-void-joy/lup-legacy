import { describe, expect, test } from "bun:test";
import type { DashboardEvent, LiveMessage, LiveSession, ReviewSummary, StreamFrame } from "../generated/views";
import { applied, called, codeNotice, conversation, paged, repositoryMessages, sessionTree, UNSAID, type LiveState } from "./live";

const repository = { key: "r1", name: "lup", repository: "/src/lup.git", checkout: "/src/lup.git/tree/dev" };

function row(id: string, fields: Partial<LiveSession> = {}): LiveSession {
  return {
    key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name: id, doing: "", task: "", running: true,
    worktree: "", holding: [], contested: [], delivery: "hook", wake: "claude", arrived: null, heard: null,
    summary: "", error: "", waiting: 0,
    activity: { said: "", calling: "", arguments: {}, at: null, transcript: "" },
    ...fields,
  };
}

function message(id: string, fields: Partial<LiveMessage> = {}): LiveMessage {
  return {
    key: `r1/${id}`, repository: "r1", id, at: 0, sender: "", recipient: "lead", recipient_kind: "session",
    text: id, door: "agent", redirect: false, in_reply_to: "", sent_at: "2026-09-29T10:00:00Z", waiting: true,
    ...fields,
  };
}

function review(key: string, created: string): ReviewSummary {
  return {
    key, root_id: "root", id: key, state: "pending", requester: "lead", reason: "", title: key, paths: [],
    total_files: 0, operation: "Bash", rule: "", created, answerable: true, unanswerable: "", stale: [], said: 0, target: "", session: "lead",
    settled: null, archived: false,
  };
}

let seq = 0;
function frame(event: DashboardEvent): StreamFrame {
  seq += 1;
  return { cursor: `{"epoch":"e","seq":${seq}}`, event };
}

function snapshot(): LiveState {
  return applied(null, frame({
    type: "snapshot",
    repositories: [repository],
    sessions: [row("lead"), row("lead-a1", { parent: "lead", kind: "subagent", name: "scout" }), row("other", { running: false })],
    messages: [message("m1", { sender: "other", at: 900 })],
    extents: [{ repository: "r1", earlier: 900 }],
    reviews: { roots: [], errors: [], reviews: [review("q1", "2026-09-29T09:00:00Z")], history: 0 },
    code: UNSAID,
  }));
}

describe("live state", () => {
  test("a snapshot is the whole state, and each frame moves it by one difference", () => {
    const whole = snapshot();
    const described = applied(whole, frame({ type: "session", session: row("lead", { doing: "reviewing" }) }));
    const gone = applied(described, frame({ type: "session_gone", key: "r1/other" }));
    const told = applied(gone, frame({ type: "message", message: message("m1", { sender: "other", waiting: false }) }));

    expect(described.sessions.get("r1/lead")?.doing).toBe("reviewing");
    expect(gone.sessions.has("r1/other")).toBe(false);
    expect(told.messages.get("r1/m1")?.waiting).toBe(false);
    expect(told.cursor).toBe(`{"epoch":"e","seq":${seq}}`);
  });

  test("reviews keep the newest first and keep their identity through frames that are not theirs", () => {
    const whole = snapshot();
    const described = applied(whole, frame({ type: "session", session: row("lead", { doing: "x" }) }));
    const parked = applied(described, frame({ type: "review", review: review("q2", "2026-09-29T11:00:00Z") }));
    const settled = applied(parked, frame({ type: "review", review: { ...review("q1", "2026-09-29T09:00:00Z"), state: "approved" } }));
    const dropped = applied(settled, frame({ type: "review_gone", key: "q2" }));
    const scoped = applied(dropped, frame({ type: "review_scope", roots: [{ id: "root", path: "/tree", repository: "", repository_name: "" }], errors: [], history: 1204 }));

    expect(described.reviews).toBe(whole.reviews);
    expect(parked.reviews.reviews.map((each) => each.key)).toEqual(["q2", "q1"]);
    expect(settled.reviews.reviews.map((each) => each.state)).toEqual(["pending", "approved"]);
    expect(dropped.reviews.reviews.map((each) => each.key)).toEqual(["q1"]);
    expect(scoped.reviews.roots.map((each) => each.path)).toEqual(["/tree"]);
    expect(scoped.reviews.history).toBe(1204);
  });

  test("sessions nest their subagents beneath them, working ones first", () => {
    const [group] = sessionTree(snapshot());
    expect(group?.repository.name).toBe("lup");
    expect(group?.sessions.map((node) => [node.session.id, node.subagents.map((each) => each.name)])).toEqual([
      ["lead", ["scout"]],
      ["other", []],
    ]);
  });

  test("a session's conversation is what it was sent and what it sent, oldest first", () => {
    const state = applied(snapshot(), frame({
      type: "message",
      message: message("m2", { sender: "lead", recipient: "other", sent_at: "2026-09-29T10:05:00Z", at: 950 }),
    }));
    const unrelated = applied(state, frame({ type: "message", message: message("m3", { sender: "user", recipient: "other", at: 1000 }) }));

    expect(conversation(unrelated, "r1", "lead").map((each) => each.id)).toEqual(["m1", "m2"]);
    expect(repositoryMessages(unrelated, "r1").map((each) => each.id)).toEqual(["m1", "m3", "m2"]);
  });

  test("an older page of a repository's messages joins the ones held, back from where they started", () => {
    const whole = snapshot();
    const older = { messages: [message("m0", { at: 0, sent_at: "2026-09-29T09:00:00Z" }), message("m1", { at: 900, waiting: false })], earlier: 0 };
    const read = paged(whole, "r1", 900, older);
    const stale = paged(read, "r1", 900, { messages: [message("stray", { at: 400 })], earlier: 0 });

    expect(whole.earlier.get("r1")).toBe(900);
    expect(repositoryMessages(read, "r1").map((each) => each.id)).toEqual(["m0", "m1"]);
    expect(read.messages.get("r1/m1")?.waiting).toBe(true);
    expect(read.earlier.get("r1")).toBe(0);
    expect(stale).toBe(read);
  });

  test("an id reads as the name its roster row carries, and the person as the operator", () => {
    const state = snapshot();
    expect(called(state, "r1", "lead-a1")).toBe("scout");
    expect(called(state, "r1", "user")).toBe("you");
    expect(called(state, "r1", "unknown-id")).toBe("unknown-id");
  });

  test("the code the dashboard runs is moved by its own frame, and said only while older", () => {
    const whole = snapshot();
    const code = { source: "abc", root: "/src/lup", since: null, older: true, failing: "", restarted: "" };
    const older = applied(whole, frame({ type: "service", code }));

    expect(codeNotice(whole.code)).toBe("");
    expect(older.code.older).toBe(true);
    expect(older.sessions).toBe(whole.sessions);
    expect(codeNotice(older.code)).toContain("is restarting onto it");
    expect(codeNotice({ ...code, failing: "ImportError" })).toContain("does not start (ImportError)");
  });

  test("a dashboard the sessions started again after it stopped says why, beside anything older", () => {
    const code = { source: "abc", root: "/src/lup", since: null, older: false, failing: "", restarted: "it was ended by SIGKILL" };

    expect(codeNotice(code)).toBe("This dashboard was restarted after it stopped: it was ended by SIGKILL.");
    expect(codeNotice({ ...code, older: true })).toContain("is restarting onto it; answers wait until the page reconnects. This dashboard was restarted");
  });
});
