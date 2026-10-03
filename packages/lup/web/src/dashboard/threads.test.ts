import { describe, expect, test } from "bun:test";
import type { LiveMessage, LiveSession } from "../generated/views";
import { applied, NO_BUDGET, NO_KEYS, UNSAID } from "./live";
import { discussions, reaches, threadBuffer } from "./threads";

const repository = { key: "r1", name: "lup", repository: "/src/lup.git", checkout: "/src/lup.git/tree/dev" };

function session(id: string, name = id): LiveSession {
  return {
    key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name, doing: "", task: "", running: true, worktree: "", holding: [], contested: [],
    delivery: "hook", wake: "claude", arrived: null, heard: "2026-09-29T12:00:00Z", summary: "", error: "", waiting: 0, runtime: "claude", spawned_by: "", process: null,
    holds: [], held_since: null, activity: { said: "", calling: "", arguments: {}, at: null, transcript: "", recent: [] },
  };
}

let byte = 0;
function message(id: string, sender: string, recipient: string, text: string, at: string, fields: Partial<LiveMessage> = {}): LiveMessage {
  byte += 100;
  return { key: `r1/${id}`, repository: "r1", id, at: byte, sender, recipient, recipient_kind: recipient === "user" ? "user" : "session", text, door: "agent", redirect: false, in_reply_to: "", sent_at: at, waiting: false, post: id, thread: id, prompt: false, ...fields };
}

const live = (messages: LiveMessage[]) => {
  const state = applied(null, {
    cursor: "c",
    event: { type: "snapshot", repositories: [repository], sessions: [session("res", "research"), session("sum", "summarize_sources")], messages, extents: [], reviews: { roots: [], reviews: [], errors: [], history: 0 }, code: UNSAID, keys: NO_KEYS, users: [], served: [], budget: NO_BUDGET },
  });
  if (state === null) throw new Error("a snapshot always applies");
  return state;
};

describe("discussions: the mail read as threads", () => {
  test("posts replying to each other, transitively, are one thread titled by its first line; the rest are conversations by member set", () => {
    const state = live([
      message("m1", "res", "sum", "Which sources disagree?\nlist them", "2026-09-29T10:00:00Z"),
      message("m2", "sum", "res", "Three of them.", "2026-09-29T10:01:00Z", { in_reply_to: "m1" }),
      message("m3", "res", "user", "Sum found three; I go with the newest.", "2026-09-29T10:02:00Z", { in_reply_to: "m2", waiting: true }),
      message("m4", "res", "sum", "status?", "2026-09-29T09:00:00Z"),
      message("m5", "sum", "res", "working", "2026-09-29T09:01:00Z"),
    ]);
    const found = discussions(state);
    expect(found.map((each) => `${each.kind}:${each.title}:${each.posts.length}`)).toEqual([
      "thread:Which sources disagree?:3",
      "conversation:research, summarize_sources:2",
    ]);
    const [thread] = found;
    expect(thread?.participants).toEqual(["res", "sum", "user"]);
    expect(thread?.unread).toBe(1);
    expect(thread === undefined ? [] : reaches(state, thread)).toEqual(["research", "summarize_sources"]);
  });

  test("one send to several members is one post with every recipient, and the buffer shows it once with what it answers", () => {
    const at = "2026-09-29T11:00:00Z";
    const state = live([
      message("m1", "res", "sum", "Plan: split the corpus.", "2026-09-29T10:59:00Z"),
      message("m2", "user", "res", "Go ahead, both of you.", at, { in_reply_to: "m1", door: "page", post: "p2", thread: "m1" }),
      message("m3", "user", "sum", "Go ahead, both of you.", at, { in_reply_to: "m1", door: "page", waiting: true, post: "p2", thread: "m1" }),
    ]);
    const [thread] = discussions(state);
    expect(thread?.posts.map((post) => post.copies.map((copy) => copy.recipient))).toEqual([["sum"], ["res", "sum"]]);
    const rows = thread === undefined ? [] : threadBuffer(state, thread).rows;
    expect(rows.map((row) => row.t)).toEqual(["sec", "post", "post"]);
    const second = rows[2];
    expect(second?.t === "post" && second.answered?.text).toBe("Plan: split the corpus.");
  });

  test("posts sharing a thread are one thread, which a post into it names, even where none replies to another here", () => {
    const state = live([
      message("m1", "res", "sum", "Which print do we trust?", "2026-09-29T10:00:00Z", { post: "p1", thread: "p1" }),
      message("m2", "sum", "res", "The revision.", "2026-09-29T10:01:00Z", { post: "p2", thread: "p1" }),
      message("m3", "sum", "user", "And you?", "2026-09-29T10:02:00Z", { post: "p3", thread: "p1", waiting: true }),
    ]);
    const [thread, ...rest] = discussions(state);
    expect(rest).toEqual([]);
    expect(thread?.kind).toBe("thread");
    expect(thread?.posts.map((post) => post.id)).toEqual(["p1", "p2", "p3"]);
    expect(thread?.thread).toBe("p1");
  });

  test("a message the record kept from before posts had ids is one post with the copies sharing its sender, text and time", () => {
    const at = "2026-09-29T11:00:00Z";
    const [only] = discussions(live([
      message("m1", "user", "res", "Both of you, stop.", at, { post: "", thread: "" }),
      message("m2", "user", "sum", "Both of you, stop.", at, { post: "", thread: "" }),
    ]));
    expect(only?.posts.map((post) => [post.id, post.copies.map((copy) => copy.recipient)])).toEqual([["m1", ["res", "sum"]]]);
    expect(only?.thread).toBe("m1");
  });

  test("a conversation including the operator is titled with the others and you", () => {
    const [only] = discussions(live([message("m1", "res", "user", "done", "2026-09-29T10:00:00Z")]));
    expect(only?.title).toBe("research and you");
  });
});
