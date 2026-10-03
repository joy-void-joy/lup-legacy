// The dashboard mounted against a fake dashboard server: the stream, the
// review routes, the message routes and the key routes. What these tests read
// is what the page draws and what it posts; how it is laid out, and how real
// keys and touches move it, the real-browser checks read (decision 65).
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act } from "react";
import type { KeyBindings, LineComment, ReviewFile, ReviewNotification, ThreadEntry } from "../generated/views";
import { App } from "./App";
import { CATALOG } from "./keys";
import { click, mount, one, until, type Mounted } from "../testing";

const originalFetch = globalThis.fetch;
const root = { id: "tree", path: "/project/tree/feature", repository: "/project/lup.git", repository_name: "lup" };
const repository = { key: "r1", name: "lup", repository: "/project/lup.git", checkout: "/project/lup.git/tree/feature" };
const NONE: KeyBindings = { source: "", unread: "", changed: [], report: { applied: [], refused: [], waits: [] } };
const DELIVERED: ReviewNotification = { queued: true, woken: false, waited: false, copied: false, detail: "No `review wait` holds it: in lead's mailbox, not woken: asleep." };

let frameSeq = 0;
function sent(event: object): string {
  frameSeq += 1;
  const frame = { cursor: JSON.stringify({ epoch: "fixture", seq: frameSeq }), event };
  return `id: ${frame.cursor}\ndata: ${JSON.stringify(frame)}\n\n`;
}

const summary = {
  key: "tree-q1", root_id: root.id, id: "q1", state: "pending", requester: "codex-session",
  reason: "Review the complete replacement", operation: "apply_patch in /project", rule: "whole-file",
  title: "Update project/file.py", paths: ["project/file.py"], total_files: 1,
  created: "2026-09-24T12:00:00Z", answerable: true, unanswerable: "", said: 0, target: "/project", session: "lead",
  stale: [] as { path: string; cause: "changed" | "created" | "deleted" | "directory"; read: boolean }[], settled: null as string | null, archived: false,
};

function review(key = "tree-q1", created = "2026-09-24T12:00:00Z") {
  return {
    summary: { ...summary, key, id: key === "tree-q1" ? "q1" : key, created },
    question: {
      fingerprint: `bound-${key}`, account: [{ source: "description", text: "Replace the file whole, so the old helper goes." }] as { source: "description"; text: string }[],
      answer: null as null | { approved: boolean; principal: string; note: string; comments?: LineComment[]; at?: string },
      operation: { tool: "apply_patch", cwd: "/project", payload: { patch: "Complete requested patch" } as Record<string, string> },
      unpreviewed: null, segments: null, member: "lead", agent: "",
    },
    files: [{ path: "/project/file.py", operation: "modify", before: "before\n", after: "after\n",
      review_effect: "ask" as ReviewFile["review_effect"], review_reason: "This file requires approval.", review_label: { kind: "protected", words: "protected" },
      unified: "--- before\n+++ after\n@@ -1 +1 @@\n-before\n+after\n", unchanged: false, additions: 1, deletions: 1, suppressions: [], markers: [], about: "",
      hunks: [{ header: "@@ -1 +1 @@", old_start: 1, old_end: 1, new_start: 1, new_end: 1, lines: [
        { kind: "remove", text: "before\n", old_line: 1 as number | null, new_line: null as number | null, suppression: false },
        { kind: "add", text: "after\n", old_line: null as number | null, new_line: 1 as number | null, suppression: false },
      ] }],
    }],
    command: null as string | null,
    thread: [] as ThreadEntry[],
    preview_unavailable: "",
    preview_notice: "",
    notification: null as ReviewNotification | null,
  };
}

function session(id: string, fields: object = {}) {
  return {
    key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name: id, doing: `${id} is doing its part`, task: "", running: true,
    worktree: "/project/lup.git/tree/feature", holding: [], contested: [], delivery: "hook", wake: "claude", arrived: null, heard: new Date().toISOString(),
    summary: "", error: "", waiting: 0, activity: { said: `${id} said something`, calling: "", arguments: {}, at: new Date().toISOString(), transcript: "" },
    ...fields,
  };
}

describe("dashboard page", () => {
  let shown: Mounted | null = null;
  let details = new Map<string, ReturnType<typeof review>>();
  let rows: (typeof summary)[] = [];
  let older: (typeof summary)[] = [];
  let answerStatus = 200;
  let streamStatus = 200;
  let stream: ReadableStreamDefaultController<Uint8Array> | null = null;
  let requests: { path: string; method: string; body: unknown; authorization: string | null }[] = [];
  let sessions: object[] = [];
  let messages: object[] = [];
  let extents: object[] = [];
  let olderMail: { before: number; page: { messages: object[]; earlier: number } }[] = [];
  let panes: object[] = [];
  let keys: KeyBindings = NONE;
  let tried: KeyBindings = NONE;
  let code = { source: "fixture", root: "/project/packages/lup/src/lup", since: null as string | null, older: false, failing: "", restarted: "" };
  const settled = () => [...rows.filter((row) => row.state !== "pending"), ...older];
  const queue = () => ({ roots: [root], reviews: rows, errors: [], history: settled().length });
  const snapshot = () => sent({ type: "snapshot", repositories: [repository], sessions, messages, extents, reviews: queue(), code, keys });
  const deliver = async (text: string) => { await act(async () => stream?.enqueue(new TextEncoder().encode(text))); };
  const posted = () => requests.filter((request) => request.method === "POST");

  beforeEach(() => {
    const first = review();
    details = new Map([[first.summary.key, first]]);
    rows = [{ ...first.summary }];
    older = [];
    answerStatus = 200;
    streamStatus = 200;
    stream = null;
    requests = [];
    sessions = [session("lead"), session("lead-a1", { parent: "lead", kind: "subagent", name: "scout" }), session("old", { running: false, summary: "Handed back." })];
    messages = [];
    extents = [{ repository: "r1", earlier: 0 }];
    olderMail = [];
    panes = [];
    keys = NONE;
    tried = NONE;
    code = { source: "fixture", root: "/project/packages/lup/src/lup", since: null, older: false, failing: "", restarted: "" };
    Object.defineProperty(window, "innerWidth", { value: 1920, configurable: true });
    localStorage.clear();
    window.history.replaceState(null, "", "/#token=browser-secret");
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input);
      const body: unknown = typeof options?.body === "string" ? JSON.parse(options.body) : null;
      requests.push({ path, method: options?.method ?? "GET", body, authorization: new Headers(options?.headers).get("Authorization") });
      if (path === "api/stream") {
        if (streamStatus !== 200) return Response.json({ detail: "Authentication required" }, { status: streamStatus });
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            stream = controller;
            controller.enqueue(new TextEncoder().encode(snapshot()));
            options?.signal?.addEventListener("abort", () => controller.error(new DOMException("Stopped", "AbortError")), { once: true });
          },
        }));
      }
      if (path.startsWith("api/reviews/history?")) {
        const query = new URLSearchParams(path.slice(path.indexOf("?") + 1));
        const linked = query.get("review");
        const chosen = linked === null ? settled() : settled().filter((row) => row.id === linked);
        const offset = Number(query.get("offset"));
        return Response.json({ reviews: chosen.slice(offset, offset + Number(query.get("limit"))), total: settled().length });
      }
      if (path === "api/setup") return Response.json(panes);
      if (path === "api/keys/try") return Response.json(tried);
      if (path === "api/keys") return Response.json(tried);
      for (const [key, captured] of details) {
        if (path === `api/reviews/${key}`) return Response.json(captured);
        if (path === `api/reviews/${key}/remark`) {
          const note = (body as { note: string; comments: LineComment[] });
          const remarked = { ...captured, thread: [...captured.thread, { kind: "remark" as const, author: "operator", text: note.note, comments: note.comments, at: "2026-09-24T12:04:00Z", approved: null }] };
          details.set(key, remarked);
          return Response.json({ review: remarked, notification: DELIVERED });
        }
        if (path !== `api/reviews/${key}/answer`) continue;
        if (answerStatus !== 200) return Response.json({ detail: "The file changed; refresh the request." }, { status: answerStatus });
        const answer = body as { approved: boolean; note: string; comments: LineComment[] };
        const decided = { ...captured, summary: { ...captured.summary, state: answer.approved ? "approved" : "rejected", answerable: false, settled: "2026-09-24T12:05:00Z" },
          question: { ...captured.question, answer: { ...answer, principal: "operator", at: "2026-09-24T12:05:00Z" } },
          thread: [...captured.thread, { kind: "answer" as const, author: "operator", text: answer.note, comments: answer.comments, at: "2026-09-24T12:05:00Z", approved: answer.approved }] };
        details.set(key, decided);
        rows = rows.map((row) => row.key === key ? decided.summary : row);
        return Response.json({ review: decided, notification: DELIVERED });
      }
      if (path.startsWith("api/repositories/r1/messages?")) {
        const before = Number(new URLSearchParams(path.slice(path.indexOf("?") + 1)).get("before"));
        const found = olderMail.find((each) => each.before === before);
        return found === undefined ? Response.json({ detail: `No page before ${before}` }, { status: 404 }) : Response.json(found.page);
      }
      if (path.startsWith("api/repositories/") && path.endsWith("/messages")) {
        return Response.json({ session: "r1/lead", queued: true, woken: true, detail: "Queued in its mailbox, and its runtime accepted the wake." });
      }
      return Response.json({ detail: `Unknown fixture route ${path}` }, { status: 404 });
    }, { preconnect() {} });
  });

  afterEach(() => {
    shown?.unmount();
    shown = null;
    globalThis.fetch = originalFetch;
    localStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  function add(key: string, created: string) {
    const item = review(key, created);
    item.summary.reason = `Request ${key}`;
    details.set(key, item);
    rows.push(item.summary);
    return item;
  }

  const status = () => shown?.root.querySelector("#statusline")?.textContent ?? "";
  const open = (state: string) => (status().match(new RegExp(`${state} ([\\w-]{1,8})`)) ?? [])[1] ?? "";

  async function landed() {
    shown = mount(<App />);
    await until(() => shown?.root.querySelector("#note") !== null && shown?.root.querySelector(".pane .r") !== null, "the review and its note box");
    return shown;
  }

  async function write(field: HTMLTextAreaElement | HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")?.set;
    if (setter === undefined) throw new Error("the field has no value setter");
    await act(async () => {
      field.focus();
      setter.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function key(key: string, extra: KeyboardEventInit = {}, target: EventTarget = document.activeElement ?? window) {
    await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key, code: extra.code ?? key, bubbles: true, cancelable: true, ...extra })); });
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keyup", { key, code: extra.code ?? key, bubbles: true })); });
  }


  async function command(text: string) {
    await key(":");
    await until(() => document.getElementById("cmd-input") !== null, "the command line");
    await write(one<HTMLInputElement>(document, "#cmd-input"), text);
    await key("Enter", {}, one(document, "#cmd-input"));
  }

  const note = () => one<HTMLTextAreaElement>(document, "#note");

  test("the page lands on the newest review waiting on the operator, in its note box, which navigates while empty", async () => {
    add("tree-q2", "2026-09-24T12:10:00Z");
    const page = await landed();
    await until(() => open("pending") === "tree-q2", "the newest review open");
    await until(() => status().includes("j/k move on"), "the armed box");
    expect(document.activeElement?.id).toBe("note");
    expect(status()).toContain("INSERT");
    await key("j", {}, note());
    await until(() => open("pending") === "q1", "the next review");
    expect(document.activeElement?.id).toBe("note");
    expect(page.root.querySelector(".float")).toBeNull();
  });

  test("the head says what the call is, the extract what the policy asks, and the asker's words are prose", async () => {
    const page = await landed();
    expect(one(page.root, "#ebar").textContent).toContain("Patch · 1 file");
    expect(one(page.root, "#ebar").textContent).toContain("? whole-file — file.py: This file requires approval.");
    expect(one(page.root, "#context").textContent).toContain("agent's note");
    expect(one(page.root, "#context").textContent).toContain("Replace the file whole, so the old helper goes.");
    expect([...page.root.querySelectorAll(".pane .r.del .tx, .pane .r.add .tx")].map((line) => line.textContent?.slice(0, 5))).toEqual(["befor", "after"]);
  });

  test("a resolution of a conflicted file colours each side as its own, bars it, and names each marker's side and branch", async () => {
    const [opening, split, closing] = ["<", "=", ">"].map((char) => char.repeat(7));
    const lines = ["export function greet(name: string): string {", `${opening} HEAD`, "  /* kept short", split, "  const message = `Hey ${name}`;", `${closing} feat-x`, "     closed here */", "  return name;", "}"];
    const kept = [0, 2, 6, 7, 8];
    const resolution = review();
    const [base] = resolution.files;
    if (base === undefined) throw new Error("the fixture review has a file");
    resolution.files = [{
      ...base, path: "/project/greet.ts", before: `${lines.join("\n")}\n`, after: `${kept.map((at) => lines[at]).join("\n")}\n`, additions: 0, deletions: 4,
      hunks: [{ header: "@@ -1,9 +1,5 @@", old_start: 1, old_end: 9, new_start: 1, new_end: 5, lines: lines.map((text, at) => ({
        kind: kept.includes(at) ? "context" : "remove", text: `${text}\n`, old_line: at + 1 as number | null, new_line: kept.includes(at) ? kept.indexOf(at) + 1 as number | null : null, suppression: false,
      })) }],
    }];
    details.set("tree-q1", resolution);
    const page = await landed();
    const markers = [...page.root.querySelectorAll(".pane .r.cf-at")];
    expect(markers.map((row) => row.querySelector(".vt[class*='cfs-']")?.textContent)).toEqual(["◂ ours · HEAD", "◂ theirs · feat-x", "◂ end of theirs · feat-x"]);
    expect(markers.every((row) => row.classList.contains("del"))).toBe(true);
    const theirs = one(page.root, ".pane .r.cf-theirs:not(.cf-at)");
    expect(theirs.querySelector(".hljs-keyword")?.textContent).toBe("const");
    const shared = [...page.root.querySelectorAll(".pane .r.ctx")].find((row) => row.textContent?.includes("kept short"));
    expect(shared?.classList.contains("cf")).toBe(false);
    expect(shared?.querySelector(".hljs-comment")?.textContent).toContain("kept short");
  });

  test("Ctrl+Enter approves exactly what the page showed, comments included, and the next review opens in its box", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    const page = await landed();
    await click(one(page.root, ".pane .r.add .ln[data-side=after]"));
    await until(() => page.root.querySelector(".cmedit") !== null, "the comment box");
    await write(one<HTMLTextAreaElement>(page.root, ".cmedit"), "Keep the public signature.");
    await write(note(), "Fine once the comment is answered.");
    await key("Enter", { ctrlKey: true }, note());
    await until(() => posted().length === 1, "the approval");
    expect(posted()[0]?.path).toBe("api/reviews/tree-q1/answer");
    expect(posted()[0]?.body).toEqual({ approved: true, note: "Fine once the comment is answered.", fingerprint: "bound-tree-q1",
      comments: [{ path: "/project/file.py", side: "after", start: 1, end: 1, note: "Keep the public signature." }] });
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    expect(window.location.hash).not.toContain("token");
    await until(() => open("pending") === "tree-q2", "the next review");
    await until(() => (page.root.querySelector("#notify")?.textContent ?? "").includes("Approved"), "the notice");
    expect(one(page.root, "#notify").textContent).toContain(DELIVERED.detail);
  });

  test("a refused answer puts everything back where it was, drafts included, and its notice opens it again", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    const page = await landed();
    answerStatus = 409;
    await write(note(), "Use a scoped patch.");
    await key("Enter", { ctrlKey: true }, note());
    await until(() => (page.root.querySelector("#notify")?.textContent ?? "").includes("Approval not recorded"), "the refusal");
    expect(one(page.root, "#notify").textContent).toContain("The file changed; refresh the request.");
    await click([...page.root.querySelectorAll<HTMLElement>("#notify button")].find((button) => button.textContent === "Open it") ?? one(page.root, "#notify"));
    await until(() => open("pending") === "q1", "the refused review open again");
    expect(note().value).toBe("Use a scoped patch.");
    expect(one(page.root, "#composer").textContent).toContain("The file changed; refresh the request.");
    answerStatus = 200;
    await key("Delete", { altKey: true }, note());
    await until(() => posted().length === 2, "the decline");
    expect(posted()[1]?.body).toEqual({ approved: false, note: "Use a scoped patch.", comments: [], fingerprint: "bound-tree-q1" });
  });

  test("a held answer key cannot answer the next review", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    await landed();
    await act(async () => { note().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", ctrlKey: true, bubbles: true, cancelable: true })); });
    await until(() => open("pending") === "tree-q2", "the next review");
    await act(async () => { note().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", ctrlKey: true, repeat: true, bubbles: true, cancelable: true })); });
    await act(async () => { note().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", ctrlKey: true, bubbles: true, cancelable: true })); });
    await until(() => posted().length === 1, "the one answer");
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(posted()).toHaveLength(1);
  });

  test("Alt+Enter sends the note without deciding; the review keeps waiting and its thread shows the remark", async () => {
    const page = await landed();
    await write(note(), "Why replace it whole?");
    await key("Enter", { altKey: true }, note());
    await until(() => posted().length === 1, "the remark");
    expect(posted()[0]?.path).toBe("api/reviews/tree-q1/remark");
    expect(posted()[0]?.body).toEqual({ note: "Why replace it whole?", comments: [], fingerprint: "bound-tree-q1" });
    await until(() => (page.root.querySelector("#context")?.textContent ?? "").includes("operator commented"), "the remark in its thread");
    expect(open("pending")).toBe("q1");
  });

  test("an older snapshot cannot undo an answer given here before the stream catches up", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    const page = await landed();
    const stale = snapshot();
    await key("Enter", { ctrlKey: true }, note());
    await until(() => open("pending") === "tree-q2", "the next review");
    await deliver(stale);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(page.root.querySelectorAll("#queue .tr.review")).toHaveLength(1);
  });

  test("focus decides: a letter typed disarms the box; Esc reads in the buffer, where j moves a line; Alt+↓ walks the reviews; Tab goes round", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    const page = await landed();
    await write(note(), "J");
    await key("j", {}, note());
    expect(open("pending")).toBe("q1");
    expect(status()).not.toContain("j/k move on");
    await key("Escape", {}, note());
    await until(() => status().includes("NORMAL") && status().includes("in the buffer"), "the buffer");
    const line = () => page.root.querySelector(".pane .r.cur")?.getAttribute("data-i");
    const before = line();
    await key("j", {}, document.body);
    await until(() => line() !== before, "the next line");
    expect(open("pending")).toBe("q1");
    await key("ArrowDown", { altKey: true }, document.body);
    await until(() => open("pending") === "tree-q2", "the next review");
    expect(status()).toContain("in the buffer");
    await key("Tab", {}, document.body);
    await until(() => status().includes("in the box"), "the box, by Tab");
    await key("Tab", { shiftKey: true }, document.activeElement ?? document.body);
    await until(() => status().includes("in the buffer"), "back in the buffer, by Shift+Tab");
    await key("c", {}, document.body);
    await until(() => document.activeElement?.id === "note", "the box again");
    expect(status()).not.toContain("j/k move on");
  });

  test("help, which-key and the key finder are drawn from the one catalog", async () => {
    const page = await landed();
    await key("Escape", {}, note());
    await key("?", {}, document.body);
    await until(() => page.root.querySelector("[aria-label=Keys]") !== null, "the help");
    const rows = page.root.querySelectorAll("[aria-label=Keys] .cols section .kr");
    const listed = CATALOG.actions.filter((action) => !action.hidden).length;
    expect(rows.length).toBeGreaterThanOrEqual(listed);
    for (const action of CATALOG.actions.filter((each) => !each.hidden)) expect(one(page.root, "[aria-label=Keys]").textContent).toContain(action.name);
    await key("q", {}, document.body);
    await until(() => page.root.querySelector("[aria-label=Keys]") === null, "the help closed");
    await key(" ", {}, document.body);
    await until(() => page.root.querySelector("#whichkey") !== null, "which-key");
    expect(one(page.root, "#whichkey").textContent).toContain("+find");
    await key("f", {}, document.body);
    await key("k", {}, document.body);
    await until(() => page.root.querySelector("#finder") !== null, "the key finder");
    expect(one(page.root, "#finder").textContent).toContain("answer.approve");
  });

  test("the command line runs commands, completes their names, and refuses a save", async () => {
    const page = await landed();
    await key("Escape", {}, note());
    await command("wq");
    expect(one(page.root, "#cmd-msg").textContent).toContain("writes nothing here");
    await command("frobnicate");
    expect(one(page.root, "#cmd-msg").textContent).toContain("E492: Not an editor command: frobnicate");
    await key(":", {}, document.body);
    await write(one<HTMLInputElement>(page.root, "#cmd-input"), "hel");
    await key("Tab", {}, one(page.root, "#cmd-input"));
    expect(one<HTMLInputElement>(page.root, "#cmd-input").value).toBe("help");
    await key("Enter", {}, one(page.root, "#cmd-input"));
    await until(() => page.root.querySelector("[aria-label=Keys]") !== null, "the help, from :help");
  });

  test("a link to a review opens it; one to a review nobody holds keeps watching for it", async () => {
    add("tree-q2", "2026-09-24T11:00:00Z");
    window.history.replaceState(null, "", "/#review=q1&root=tree&token=browser-secret");
    shown = mount(<App />);
    await until(() => open("pending") === "q1", "the linked review");
    shown.unmount();
    window.history.replaceState(null, "", "/#review=missing");
    shown = mount(<App />);
    await until(() => (shown?.root.textContent ?? "").includes("Request not found"), "the missing review");
    expect(shown.root.textContent).toContain("missing");
    expect(open("pending")).toBe("");
  });

  test("access refused gives the launch instructions, and checking again reads this origin's storage", async () => {
    streamStatus = 401;
    window.history.replaceState(null, "", "/#review=q1");
    localStorage.clear();
    shown = mount(<App />);
    await until(() => (shown?.root.textContent ?? "").includes("access denied"), "the refusal");
    expect(shown.root.textContent).toContain("dashboard open");
    expect(shown.root.textContent).toContain("Requested review: q1");
    streamStatus = 200;
    localStorage.setItem("lup-dashboard-token", "another-tab");
    await click(one(shown.root, ".denied button"));
    await until(() => open("pending") === "q1", "the review once access is back");
    expect(requests[requests.length - 1]?.authorization).toBe("Bearer another-tab");
  });

  test("History past what the stream carries is read a page at a time", async () => {
    older = [{ ...summary, key: "tree-old", id: "old", state: "approved", answerable: false, settled: "2026-09-20T10:00:00Z" }];
    const page = await landed();
    await key("Escape", {}, note());
    await command("older");
    await until(() => (page.root.querySelector("#queue")?.textContent ?? "").includes("approved"), "the older review in History");
    expect(requests.some((request) => request.path.startsWith("api/reviews/history?offset=0"))).toBe(true);
  });

  test("the tree lists every agent with its subagents; one opens beside it and its box writes to it", async () => {
    const page = await landed();
    expect([...page.root.querySelectorAll("#queue .tr.member")].map((row) => row.querySelector("b")?.textContent)).toEqual(["lead", "scout"]);
    expect(one(page.root, "#queue").textContent).toContain("1 stopped agent folded");
    await click([...page.root.querySelectorAll<HTMLElement>("#queue .tr.member")][1] ?? one(page.root, "#queue"));
    await until(() => (page.root.querySelector("#ebar")?.textContent ?? "").includes("scout"), "the subagent");
    expect(one(page.root, ".pane").textContent).toContain("lead-a1 said something");
    await key("c", {}, document.body);
    await until(() => document.activeElement?.id === "reply", "its message box");
    await write(one<HTMLTextAreaElement>(page.root, "#reply"), "How far along is it?");
    await key("Enter", { altKey: true }, one(page.root, "#reply"));
    await until(() => (page.root.querySelector("#composer")?.textContent ?? "").includes("accepted the wake"), "what came of it");
    expect(posted()[0]?.path).toBe("api/repositories/r1/sessions/lead-a1/messages");
    expect(posted()[0]?.body).toEqual({ text: "How far along is it?" });
  });

  test("messages older than the stream carries are read back a page at a time", async () => {
    extents = [{ repository: "r1", earlier: 900 }];
    olderMail = [{ before: 900, page: { messages: [{ key: "r1/m0", repository: "r1", id: "m0", at: 10, sender: "lead", recipient: "lead-a1", recipient_kind: "subagent", text: "The first word.", door: "agent", redirect: false, in_reply_to: "", sent_at: "2026-09-24T09:00:00Z", waiting: false }], earlier: 0 } }];
    const page = await landed();
    await key("Escape", {}, note());
    await command("agent lead");
    await until(() => (page.root.querySelector(".pane")?.textContent ?? "").includes("Load earlier messages"), "the earlier row");
    await key("E", {}, document.body);
    await until(() => (page.root.querySelector(".pane")?.textContent ?? "").includes("The first word."), "the earlier message");
    expect(one(page.root, ".pane").textContent).not.toContain("Load earlier messages");
  });

  test("Threads reads the mail as discussions; r answers a post, and a post names the route it waits on", async () => {
    const mail = (id: string, sender: string, recipient: string, text: string, sentAt: string, fields: object = {}) =>
      ({ key: `r1/${id}`, repository: "r1", id, at: Number(id.slice(1)) * 100, sender, recipient, recipient_kind: recipient === "user" ? "user" : "session", text, door: "agent", redirect: false, in_reply_to: "", sent_at: sentAt, waiting: false, ...fields });
    messages = [
      mail("m1", "lead", "lead-a1", "Which sources disagree?", "2026-09-24T10:00:00Z"),
      mail("m2", "lead-a1", "lead", "Three of them.", "2026-09-24T10:01:00Z", { in_reply_to: "m1" }),
      mail("m3", "lead", "user", "Scout found three.", "2026-09-24T10:02:00Z", { in_reply_to: "m2", waiting: true }),
    ];
    const page = await landed();
    await key("Escape", {}, note());
    await key("4", {}, document.body);
    await key("g", {}, document.body);
    await key("t", {}, document.body);
    await until(() => status().includes("Threads"), "the Threads view");
    expect(one(page.root, "#tabline").textContent).toContain("4 Threads");
    expect(one(page.root, "#w-queue").textContent).toContain("Which sources disagree?");
    expect(one(page.root, ".pane").textContent).toContain("Three of them.");
    expect(one(page.root, ".pane").textContent).toContain("↩ lead: Which sources disagree?");
    await key("j", {}, document.body);
    await key("j", {}, document.body);
    await key("r", {}, document.body);
    await until(() => (page.root.querySelector("#composer")?.textContent ?? "").includes("answering scout: Three of them."), "the post r chose");
    await write(one<HTMLTextAreaElement>(page.root, "#reply"), "Take the newest.");
    await key("Enter", { altKey: true }, one(page.root, "#reply"));
    await until(() => (page.root.querySelector("#cmdline")?.textContent ?? "").includes("threads/<thread>/posts"), "the refusal naming the route");
    expect(posted().filter((request) => request.path.includes("/messages"))).toEqual([]);
  });

  test("the setup view lists each repository's pane and shows the one chosen", async () => {
    panes = [{ key: "a", repository: "/a", name: "alpha", path: "/setup/a/x/" }, { key: "b", repository: "/b", name: "beta", path: "/setup/b/y/" }];
    const page = await landed();
    await key("Escape", {}, note());
    await command("setup");
    await until(() => page.root.querySelector(".setup-frame") !== null, "the setup frame");
    expect(one<HTMLIFrameElement>(page.root, ".setup-frame").getAttribute("src")).toBe("/setup/a/x/");
    await click([...page.root.querySelectorAll<HTMLElement>("#w-setup-list button")][1] ?? one(page.root, "#w-setup-list"));
    expect(one<HTMLIFrameElement>(page.root, ".setup-frame").getAttribute("src")).toBe("/setup/b/y/");
  });

  test("a dashboard running older code says so, and the notice stands until the server stops saying it", async () => {
    const page = await landed();
    code = { ...code, older: true };
    await deliver(sent({ type: "service", code }));
    await until(() => (page.root.querySelector("#notify")?.textContent ?? "").includes("older code"), "the notice");
    expect(status()).toContain("dashboard runs older code; restarting");
    code = { ...code, older: false };
    await deliver(sent({ type: "service", code }));
    await until(() => !(page.root.querySelector("#notify")?.textContent ?? "").includes("older code"), "the notice gone");
  });

  test("the person's keys arrive on the stream and rebind actions; :map tries one through the server", async () => {
    keys = { ...NONE, source: "/home/me/.config/lup/config.toml", changed: [{ action: "help", keys: ["<F1>"], origin: "config", what: "", why: "", way: "" }],
      report: { applied: [{ action: "help", keys: ["<F1>"], origin: "config", what: "", why: "", way: "" }], refused: [{ action: "marker.nxt", keys: ["m"], origin: "config", what: "marker.nxt", why: "no action has that name", way: "did you mean marker.next?" }], waits: [] } };
    const page = await landed();
    await until(() => (page.root.querySelector("#notify")?.textContent ?? "").includes("Your keys"), "the keys notice");
    expect(one(page.root, "#notify").textContent).toContain("1 binding from /home/me/.config/lup/config.toml, 1 refused");
    await key("Escape", {}, note());
    await key("?", {}, document.body);
    expect(page.root.querySelector("[aria-label=Keys]")).toBeNull();
    await key("F1", {}, document.body);
    await until(() => page.root.querySelector("[aria-label=Keys]") !== null, "the help on F1");
    expect(one(page.root, "[aria-label=Keys]").textContent).toContain("yours; lup's is ?, Space ?");
    await key("Escape", {}, document.body);
    await command("map");
    await until(() => page.root.querySelector("[aria-label='Your keys']") !== null, "the :map float");
    expect(one(page.root, "[aria-label='Your keys']").textContent).toContain("did you mean marker.next?");
    await key("Escape", {}, document.body);
    tried = { ...keys, changed: [...keys.changed, { action: "search.next", keys: ["ü"], origin: "tab", what: "", why: "", way: "" }] };
    await command("map search.next ü");
    await until(() => posted().some((request) => request.path === "api/keys/try"), "the tried line");
    expect(posted().find((request) => request.path === "api/keys/try")?.body).toEqual({ lines: [{ action: "search.next", keys: ["ü"] }] });
    await until(() => (page.root.querySelector("#cmd-msg")?.textContent ?? "").includes("in this tab"), "what :map says");
  });
});
