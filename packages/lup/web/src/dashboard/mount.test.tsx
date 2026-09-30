import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act } from "react";
import type { CommandSegment, LineComment, ReviewFile, ReviewMarker, ReviewSuppression, ReviewNotification, ThreadEntry, UnpreviewedStep } from "../generated/views";
import { App } from "./App";
import { click, labelled, mount, one, until, type Mounted } from "../testing";

const originalFetch = globalThis.fetch;
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const root = { id: "tree", path: "/project/tree/feature" };

let frameSeq = 0;
let liveRepositories: object[] = [];
let liveSessions: object[] = [];
let liveMessages: object[] = [];

/** One frame as the dashboard's stream sends it: its cursor as the event id, the frame as data. */
function sent(event: object): string {
  frameSeq += 1;
  const frame = { cursor: JSON.stringify({ epoch: "fixture", seq: frameSeq }), event };
  return `id: ${frame.cursor}\ndata: ${JSON.stringify(frame)}\n\n`;
}

/** The whole state as one frame, over the fixture's reviews and whatever sessions and messages it holds. */
function framed(reviews: unknown): string {
  const code = { source: "fixture", root: "/project/packages/lup/src/lup", since: null, older: false, failing: "" };
  return sent({ type: "snapshot", repositories: liveRepositories, sessions: liveSessions, messages: liveMessages, reviews, code });
}
/** How the fake server says the requester heard of an answer. */
const DELIVERED = { queued: true, woken: false, waited: false, copied: false, detail: "No `review wait` holds it: in lead's mailbox, not woken: asleep." };

const summary = {
  key: "tree-q1", root_id: root.id, id: "q1", state: "pending", requester: "codex-session",
  reason: "Review the complete replacement", operation: "apply_patch in /project", rule: "whole-file",
  title: "Update project/file.py", paths: ["project/file.py"], total_files: 1,
  created: "2026-09-24T12:00:00Z", answerable: true, unanswerable: "", said: 0, target: "", session: "",
  stale: [] as { path: string; cause: "changed" | "created" | "deleted" | "directory" }[],
};

function review(key = "tree-q1") {
  return {
    summary: { ...summary, key, id: key === "tree-q1" ? "q1" : key },
    question: {
      fingerprint: `bound-${key}`, resumption: "native_retry", account: [] as { source: string; text: string }[],
      answer: null as null | { approved: boolean; principal: string; note: string; comments?: LineComment[]; at?: string },
      operation: { tool: "apply_patch", cwd: "/project", payload: { patch: "Complete requested patch" } as Record<string, string> },
      unpreviewed: null as UnpreviewedStep[] | null,
      segments: null as CommandSegment[] | null,
    },
    files: [{ path: "/project/file.py", operation: "modify", before: "before\n", after: "after\n",
      review_effect: "ask" as ReviewFile["review_effect"], review_reason: "This file requires approval.",
      unified: "--- before\n+++ after\n@@ -1 +1 @@\n-before\n+after\n", unchanged: false, additions: 1, deletions: 1, suppressions: [] as ReviewSuppression[],
      markers: [] as ReviewMarker[], about: "",
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

function exception(fields: Pick<ReviewSuppression, "line" | "rule_ids" | "reason" | "introduced">): ReviewSuppression {
  return { ...fields, review_effect: fields.introduced ? "ask" : "allow", review_reason: fields.introduced ? "New exception requires approval." : "Existing exception.", review_rule_ids: fields.introduced ? fields.rule_ids : [] };
}

describe("dashboard page", () => {
  let shown: Mounted | null = null;
  let detail = review();
  let details = new Map<string, ReturnType<typeof review>>();
  let rows = [{ ...summary }];
  let roots = [root];
  let answerStatus = 200;
  let answerWait: Promise<void> | null = null;
  let detailWait = new Map<string, Promise<void>>();
  let refreshStatus = 200;
  let streamImmediately = true;
  let issues: { root: string; message: string }[] = [];
  let stream: ReadableStreamDefaultController<Uint8Array> | null = null;
  let streamingAborted = false;
  let requests: { path: string; method: string; body: unknown; authorization: string | null }[] = [];
  let panes: { key: string; repository: string; name: string; path: string }[] = [];
  let replyStatus = 200;
  // History past what the stream carries: the server pages it, most recently settled first.
  let older: (typeof summary & { settled?: string })[] = [];
  const settledRows = () => [...rows.filter((row) => row.state !== "pending"), ...older];
  const queue = () => ({ roots, reviews: rows, errors: issues, history: settledRows().length });

  beforeEach(() => {
    detail = review();
    details = new Map([[detail.summary.key, detail]]);
    rows = [{ ...summary }];
    older = [];
    roots = [root];
    answerStatus = 200;
    answerWait = null;
    detailWait = new Map();
    refreshStatus = 200;
    streamImmediately = true;
    issues = [];
    stream = null;
    streamingAborted = false;
    requests = [];
    panes = [];
    replyStatus = 200;
    liveRepositories = [];
    liveSessions = [];
    liveMessages = [];
    sessionStorage.clear();
    localStorage.clear();
    window.history.replaceState(null, "", "/#token=browser-secret");
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input);
      requests.push({ path, method: options?.method ?? "GET", body: typeof options?.body === "string" ? JSON.parse(options.body) : null,
        authorization: new Headers(options?.headers).get("Authorization") });
      if (path === "api/stream" && refreshStatus !== 200) return Response.json({ detail: "Refresh unavailable" }, { status: refreshStatus });
      if (path === "api/stream") return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          stream = controller;
          if (streamImmediately) controller.enqueue(new TextEncoder().encode(framed(queue())));
          options?.signal?.addEventListener("abort", () => {
            streamingAborted = true;
            controller.error(new DOMException("Stopped", "AbortError"));
          }, { once: true });
        },
      }));
      if (path === "api/reviews") return refreshStatus === 200 ? Response.json(queue()) : Response.json({ detail: "Refresh unavailable" }, { status: refreshStatus });
      if (path.startsWith("api/reviews/history?")) {
        const query = new URLSearchParams(path.slice(path.indexOf("?") + 1));
        const linked = query.get("review");
        const offset = Number(query.get("offset")), limit = Number(query.get("limit"));
        const chosen = linked === null ? settledRows() : settledRows().filter((row) => row.id === linked);
        return Response.json({ reviews: chosen.slice(offset, offset + limit), total: settledRows().length });
      }
      if (path === "api/setup") return Response.json(panes);
      for (const [key, captured] of details) {
        if (path === `api/reviews/${key}`) {
          await detailWait.get(key);
          return Response.json(captured);
        }
        if (path === `api/reviews/${key}/remark`) {
          if (answerWait !== null) await answerWait;
          if (answerStatus !== 200) return Response.json({ detail: "The review changed; read it again." }, { status: answerStatus });
          const body = JSON.parse(String(options?.body)) as { note: string; comments: LineComment[] };
          const remarked = { ...captured, summary: { ...captured.summary, said: captured.summary.said + 1 },
            thread: [...captured.thread, { kind: "remark" as const, author: "operator", text: body.note, comments: body.comments, at: "2026-09-24T12:04:00Z", approved: null }] };
          details.set(key, remarked);
          return Response.json({ review: remarked, notification: { ...DELIVERED, detail: "Its `review wait` holds it and tells the session now.", waited: true } });
        }
        if (path !== `api/reviews/${key}/answer`) continue;
        if (answerWait !== null) await answerWait;
        if (answerStatus !== 200) return Response.json({ detail: "The file changed; refresh the request." }, { status: answerStatus });
        const body = JSON.parse(String(options?.body)) as { approved: boolean; note: string; comments: LineComment[] };
        const settled = { ...captured, summary: { ...captured.summary, state: body.approved ? "approved" : "rejected", answerable: false },
          question: { ...captured.question, answer: { ...body, principal: "operator", at: "2026-09-24T12:05:00Z" } },
          thread: [...captured.thread, { kind: "answer" as const, author: "operator", text: body.note, comments: body.comments, at: "2026-09-24T12:05:00Z", approved: body.approved }] };
        details.set(key, settled);
        rows = rows.map((row) => row.key === key ? settled.summary : row);
        return Response.json({ review: settled, notification: DELIVERED });
      }
      if (path.startsWith("api/repositories/") && path.endsWith("/messages")) {
        if (replyStatus !== 200) return Response.json({ detail: "lead left at noon" }, { status: replyStatus });
        return Response.json({ session: "r1/lead", queued: true, woken: true, detail: "Queued in its mailbox, and its runtime accepted the wake." });
      }
      return Response.json({ detail: `Unknown fixture route ${path}` }, { status: 404 });
    }, { preconnect() {} });
  });

  afterEach(() => {
    if (originalStorage !== undefined) Object.defineProperty(globalThis, "localStorage", originalStorage);
    shown?.unmount();
    shown = null;
    globalThis.fetch = originalFetch;
    sessionStorage.clear();
    localStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  function addRequest(key = "tree-q2") {
    const item = review(key);
    item.summary.reason = `Request ${key}`;
    details.set(key, item);
    rows.push(item.summary);
    return item;
  }

  test("a dashboard running older code than its checkout says so until it has restarted", async () => {
    const page = await open();
    expect(page.root.querySelector(".running-code")).toBeNull();

    const older = { source: "abc", root: "/project/packages/lup/src/lup", since: "2026-09-30T00:00:00Z", older: true, failing: "" };
    await act(async () => stream?.enqueue(new TextEncoder().encode(sent({ type: "service", code: older }))));
    await until(() => page.root.querySelector(".running-code") !== null, "the older-code notice");
    expect(one(page.root, ".running-code").textContent).toContain("runs older code than its checkout and is restarting onto it");

    await act(async () => stream?.enqueue(new TextEncoder().encode(sent({ type: "service", code: { ...older, failing: "SyntaxError: invalid syntax" } }))));
    await until(() => (page.root.querySelector(".running-code")?.textContent ?? "").includes("does not start"), "the failing notice");
    expect(one(page.root, ".running-code").textContent).toContain("SyntaxError: invalid syntax");

    await act(async () => stream?.enqueue(new TextEncoder().encode(sent({ type: "service", code: { ...older, older: false } }))));
    await until(() => page.root.querySelector(".running-code") === null, "the notice taken down");
  });

  test("watched queue, operation directory and foreign target paths are labelled separately", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    const checkout = "/projects/live-translator/tree/setup";
    roots = [{ id: root.id, path: checkout }];
    detail.question.operation.cwd = "/projects/live-translator/tree/setup";
    file.path = "/projects/lup/tree/review-queue/packages/lup/review.py";
    const page = await open();
    expect(one(page.root, ".masthead .watched-checkout").textContent).toBe(`Watching queue ${checkout}`);
    expect([...page.root.querySelectorAll(".request-meta code")].map((node) => node.getAttribute("title"))).toEqual([checkout, detail.question.operation.cwd]);
    expect(one(page.root, ".file-name").getAttribute("title")).toBe(file.path);
    expect(one(page.root, ".queue-row .root-path").textContent).toBe(`Queue: ${checkout}`);
    await click(labelled(page.root, "button", "Details"));
    expect([...page.root.querySelectorAll(".request-record dd code")].slice(0, 2).map((node) => node.textContent)).toEqual([checkout, detail.question.operation.cwd]);
    expect(labelled(page.root, "button", "Approve").closest(".request")).toBeNull();
  });

  test("multiple watched checkouts remain visible while selected request identity follows navigation", async () => {
    const secondRoot = { id: "library", path: "/projects/lup/tree/feature" };
    roots = [root, secondRoot];
    const next = addRequest();
    next.summary.root_id = secondRoot.id;
    next.question.operation.cwd = "/projects/lup/tree/feature/packages/lup";
    const page = await open();
    expect(one(page.root, ".masthead .roots summary").textContent).toBe("Watching 2 checkout queues");
    expect([...page.root.querySelectorAll(".masthead .roots code")].map((node) => node.textContent)).toEqual([root.path, secondRoot.path]);
    expect(one(page.root, ".request-meta code").getAttribute("title")).toBe(root.path);
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".request-meta code")?.getAttribute("title") === secondRoot.path, "the next request's queue checkout");
    expect([...page.root.querySelectorAll(".request-meta code")].map((node) => node.getAttribute("title"))).toEqual([secondRoot.path, next.question.operation.cwd]);
    expect(one(page.root, ".masthead .roots summary").textContent).toBe("Watching 2 checkout queues");
  });

  test("the queue groups reviews by repository, then by the session that asked, and moves in that order", async () => {
    const other = { id: "other", path: "/projects/other/main", repository: "/projects/other/.git", repository_name: "other" };
    roots = [{ ...root, repository: "/project/.git", repository_name: "project" }, other];
    rows = [{ ...summary, session: "builder" }];
    const second = addRequest("tree-q2");
    second.summary.root_id = other.id;
    second.summary.session = "reviewer";
    const third = addRequest("tree-q3");
    third.summary.session = "builder";
    const page = await open();
    expect([...page.root.querySelectorAll(".repository-name")].map((node) => node.textContent)).toEqual(["project (2)", "other (1)"]);
    expect([...page.root.querySelectorAll(".session-name")].map((node) => node.textContent)).toEqual(["Asked by builder (2)", "Asked by reviewer (1)"]);
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".queue-row.selected strong")?.closest(".session-group")?.querySelector(".session-name")?.textContent === "Asked by builder (2)"
      && [...page.root.querySelectorAll(".queue-row")].indexOf(one(page.root, ".queue-row.selected")) === 1, "the second request of the first session");
  });

  test("the setup view lists each repository's pane and shows the one chosen", async () => {
    panes = [
      { key: "first", repository: "/project/.git", name: "project", path: "/setup/first/capability-one/" },
      { key: "second", repository: "/projects/other/.git", name: "other", path: "/setup/second/capability-two/" },
    ];
    const page = await open();
    await click(labelled(page.root, ".views button", "Setup"));
    await until(() => page.root.querySelector(".setup-frame") !== null, "the setup pane");
    expect(one<HTMLIFrameElement>(page.root, ".setup-frame").getAttribute("src")).toBe("/setup/first/capability-one/");
    expect(requests.find((request) => request.path === "api/setup")?.authorization).toBe("Bearer browser-secret");
    await click(labelled(page.root, ".setup-list button", "other"));
    await until(() => page.root.querySelector(".setup-frame")?.getAttribute("src") === "/setup/second/capability-two/", "the second pane");
    expect(one<HTMLIFrameElement>(page.root, ".setup-frame").title).toBe("Setup · other");
    await click(labelled(page.root, ".views button", "Reviews"));
    await until(() => page.root.querySelector(".composer") !== null, "the review again");
  });

  const lupRepository = { key: "r1", name: "lup", repository: "/src/lup.git", checkout: "/src/lup.git/tree/dev" };

  function liveRow(id: string, fields: object = {}) {
    return {
      key: `r1/${id}`, repository: "r1", id, parent: "", kind: "session", name: id, doing: "", task: "", running: true,
      worktree: `/src/lup.git/tree/${id}`, holding: [], contested: [], delivery: "hook", wake: "claude",
      arrived: "2026-09-29T08:00:00Z", heard: "2026-09-29T10:00:00Z", summary: "", error: "", waiting: 0,
      activity: { said: "", calling: "", arguments: {}, at: null, transcript: "" },
      ...fields,
    };
  }

  function liveMessage(id: string, fields: object = {}) {
    return {
      key: `r1/${id}`, repository: "r1", id, seq: 0, sender: "", recipient: "lead", recipient_kind: "session",
      text: id, door: "agent", redirect: false, in_reply_to: "", sent_at: "2026-09-29T10:00:00Z", waiting: false, ...fields,
    };
  }

  function sessionsFixture() {
    liveRepositories = [lupRepository];
    liveSessions = [
      liveRow("lead", {
        name: "dev", doing: "rebuilding the dashboard", holding: ["at /src/lup.git/tree/dev/live.py"], waiting: 1,
        activity: { said: "Reading the roster.", calling: "Read", arguments: { file_path: "roster.py" }, at: "2026-09-29T10:00:00Z", transcript: "/t/lead.jsonl" },
      }),
      liveRow("lead-a1", { parent: "lead", kind: "subagent", name: "scout", doing: "searching the store", delivery: "hook", wake: "" }),
      liveRow("other", { name: "reviewer", running: false, summary: "done" }),
    ];
    liveMessages = [
      liveMessage("m1", { sender: "other", text: "rebase onto staging first", waiting: true }),
      liveMessage("m2", { sender: "lead", recipient: "other", text: "on it", sent_at: "2026-09-29T10:01:00Z" }),
    ];
  }

  async function sessionsView(): Promise<Mounted> {
    sessionsFixture();
    const page = await open();
    const sessions = [...page.root.querySelectorAll<HTMLElement>(".views button")].find((button) => button.textContent?.startsWith("Sessions"));
    if (sessions === undefined) throw new Error("no Sessions view button");
    await click(sessions);
    await until(() => page.root.querySelector(".sessions") !== null, "the sessions view");
    return page;
  }

  test("the sessions view lists each repository's sessions with their subagents and what each is doing", async () => {
    const page = await sessionsView();
    expect(labelled(page.root, ".views button", "Sessions (1)")).toBeDefined();
    const group = one(page.root, ".session-tree");
    expect(one(group, ".tree-repository").textContent).toContain("lup");
    const lead = one(group, "[data-session='r1/lead']");
    expect(lead.textContent).toContain("dev");
    expect(lead.textContent).toContain("rebuilding the dashboard");
    expect(lead.textContent).toContain("Read");
    expect(lead.textContent).toContain("1 waiting");
    const scout = one(group, "[data-session='r1/lead-a1']");
    expect(scout.closest(".subagents")).not.toBeNull();
    expect(scout.textContent).toContain("scout");
    expect(one(group, "[data-session='r1/other']").textContent).toContain("stopped");
  });

  test("a session shows what it is doing, what it holds and what was said, and a reply goes to it", async () => {
    const page = await sessionsView();
    await click(one(page.root, "[data-session='r1/lead'] button"));
    await until(() => page.root.querySelector(".session-detail") !== null, "the session's detail");
    const detailed = one(page.root, ".session-detail");
    expect(detailed.textContent).toContain("Reading the roster.");
    expect(detailed.textContent).toContain("roster.py");
    expect(detailed.textContent).toContain("at /src/lup.git/tree/dev/live.py");
    const said = [...detailed.querySelectorAll(".message")].map((node) => node.textContent ?? "");
    expect(said[0]).toContain("reviewer → dev");
    expect(said[0]).toContain("rebase onto staging first");
    expect(said[0]).toContain("waiting");
    expect(said[1]).toContain("dev → reviewer");
    const box = one<HTMLTextAreaElement>(detailed, "textarea");
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(box, "stop and look at the stream");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(labelled(detailed, "button", "Send"));
    await until(() => detailed.querySelector(".reply-outcome") !== null, "the reply's outcome");
    const posted = requests.find((request) => request.path === "api/repositories/r1/sessions/lead/messages");
    expect(posted?.method).toBe("POST");
    expect(posted?.body).toEqual({ text: "stop and look at the stream" });
    expect(posted?.authorization).toBe("Bearer browser-secret");
    expect(one(detailed, ".reply-outcome").textContent).toContain("runtime accepted the wake");
    expect(one<HTMLTextAreaElement>(detailed, "textarea").value).toBe("");
  });

  test("a frame moves a session's activity and conversation in place", async () => {
    const page = await sessionsView();
    await click(one(page.root, "[data-session='r1/lead'] button"));
    await until(() => page.root.querySelector(".session-detail") !== null, "the session's detail");
    const before = requests.length;
    await act(async () => {
      stream?.enqueue(new TextEncoder().encode(sent({ type: "session", session: liveRow("lead", { name: "dev", activity: { said: "Now testing the stream.", calling: "", arguments: {}, at: null, transcript: "/t/lead.jsonl" } }) })));
      stream?.enqueue(new TextEncoder().encode(sent({ type: "message", message: liveMessage("m3", { sender: "user", text: "keep going", sent_at: "2026-09-29T10:02:00Z", waiting: true }) })));
    });
    await until(() => page.root.querySelector(".session-detail")?.textContent?.includes("Now testing the stream.") === true, "the new activity");
    await until(() => page.root.querySelector(".session-detail")?.textContent?.includes("keep going") === true, "the operator's message");
    expect([...page.root.querySelectorAll(".session-detail .message")].at(-1)?.textContent).toContain("you → dev");
    expect(requests.length).toBe(before);
  });

  test("a stopped session is shown as stopped, with nothing to reply through", async () => {
    const page = await sessionsView();
    await click(one(page.root, "[data-session='r1/other'] button"));
    await until(() => page.root.querySelector(".session-detail") !== null, "the session's detail");
    const detailed = one(page.root, ".session-detail");
    expect(detailed.textContent).toContain("stopped");
    expect(detailed.textContent).toContain("done");
    expect(detailed.querySelector("textarea")).toBeNull();
  });

  test("a refused reply keeps what was written and says why", async () => {
    replyStatus = 409;
    const page = await sessionsView();
    await click(one(page.root, "[data-session='r1/lead'] button"));
    await until(() => page.root.querySelector(".session-detail textarea") !== null, "the reply box");
    const box = one<HTMLTextAreaElement>(page.root, ".session-detail textarea");
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(box, "are you there");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(labelled(page.root, ".session-detail button", "Send"));
    await until(() => page.root.querySelector(".session-detail .error") !== null, "the refusal");
    expect(one(page.root, ".session-detail .error").textContent).toContain("lead left at noon");
    expect(one<HTMLTextAreaElement>(page.root, ".session-detail textarea").value).toBe("are you there");
  });

  test("a repository's messages read as one conversation between its sessions", async () => {
    const page = await sessionsView();
    await click(labelled(page.root, ".tree-repository button", "lup"));
    await until(() => page.root.querySelector(".repository-messages") !== null, "the repository's messages");
    const said = [...page.root.querySelectorAll(".repository-messages .message")].map((node) => node.textContent ?? "");
    expect(said).toHaveLength(2);
    expect(said[0]).toContain("reviewer → dev");
    expect(said[1]).toContain("dev → reviewer");
  });

  test("checkout and target identity preserve full literal paths in compact scrollable lines", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    const path = `/projects/100%done/日本語 #?%2F/${"nested/".repeat(50)}feature`;
    roots = [{ id: root.id, path }];
    detail.question.operation.cwd = path;
    file.path = `${path}/source.py`;
    const style = document.createElement("style");
    style.textContent = await Bun.file(new URL("./styles.css", import.meta.url)).text();
    document.head.append(style);
    try {
      const page = await open();
      expect(one(page.root, ".watched-checkout code").textContent).toBe(path);
      expect(one(page.root, ".file-name").getAttribute("title")).toBe(file.path);
      for (const node of page.root.querySelectorAll<HTMLElement>(".watched-checkout code, .request-location code, .file-target code")) {
        expect(getComputedStyle(node).whiteSpace).toBe("pre");
        expect(getComputedStyle(node).overflow).toBe("auto");
        expect(node.tabIndex).toBe(0);
      }
    } finally {
      style.remove();
    }
  });

  test("an answered request says how its requester heard of it when reopened", async () => {
    detail.summary.state = "approved";
    detail.summary.answerable = false;
    detail.notification = { queued: false, woken: false, waited: false, copied: false, detail: "No `review wait` holds it and no running session matches the one that asked." };
    rows = [detail.summary];
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1&root=tree");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("no running session matches") === true, "the persisted delivery outcome");
    expect(one(shown.root, ".delivery").textContent).toBe(`How the requester heard: ${detail.notification.detail}`);
    expect(shown.root.querySelector(".composer")).toBeNull();
  });

  async function open() {
    shown = mount(<App />);
    await until(() => shown?.root.querySelector(".composer") !== null && shown?.root.querySelector(".composer") !== undefined, "the composer");
    return shown;
  }

  async function type(field: HTMLTextAreaElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (setter === undefined) throw new Error("textarea has no value setter");
    await act(async () => {
      setter.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function comment(value: string) {
    if (shown === null) throw new Error("page is not open");
    await type(one<HTMLTextAreaElement>(shown.root, "#review-comment"), value);
  }

  function box(root: HTMLElement): HTMLTextAreaElement {
    return one<HTMLTextAreaElement>(root, "#review-comment");
  }

  function sourceText(root: HTMLElement): string {
    return [...root.querySelectorAll(".diff.source .line-content code")].map((line) => line.textContent).join("\n");
  }

  async function approve(target: EventTarget = window) {
    await keydown("Enter", "Enter", { ctrlKey: true }, target);
    await keyup("Enter", "Enter");
  }

  async function decline(target: EventTarget = window) {
    await keydown("Delete", "Delete", { altKey: true }, target);
    await keyup("Delete", "Delete");
  }

  function posted() {
    return requests.filter((request) => request.method === "POST");
  }

  async function keydown(key: string, code: string, extra: KeyboardEventInit = {}, target: EventTarget = window) {
    await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...extra })); });
  }

  async function keyup(key: string, code: string) {
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keyup", { key, code, bubbles: true })); });
  }

  test("the first stream snapshot loads the selected request without duplicate reads", async () => {
    const page = await open();
    expect(requests.map((request) => request.path)).toEqual(["api/stream", "api/reviews/tree-q1"]);
    expect(page.root.textContent).toContain("Live");
  });

  test("a slow first snapshot shows unknown counts instead of claiming the queue is empty", async () => {
    streamImmediately = false;
    shown = mount(<App />);
    await until(() => requests.some((request) => request.path === "api/stream"), "the connecting stream");
    expect(shown.root.textContent).toContain("Pending (?)");
    expect(shown.root.textContent).toContain("Loading review queue…");
    expect(shown.root.textContent).not.toContain("Pending (0)");
    expect(shown.root.textContent).not.toContain("Queue complete");
    expect(shown.root.textContent).not.toContain("No requests waiting");
    expect(shown.root.querySelector(".queue")?.getAttribute("aria-busy")).toBe("true");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.querySelector(".request h2") !== null, "the loaded request");
    expect(shown.root.textContent).toContain("Pending (1)");
    expect(shown.root.querySelector(".queue")?.getAttribute("aria-busy")).toBe("false");
  });

  test("a linked request is loading until its first snapshot resolves the identity", async () => {
    streamImmediately = false;
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1");
    shown = mount(<App />);
    await until(() => requests.length > 0, "the linked stream");
    expect(shown.root.textContent).toContain("Loading requested review…");
    expect(shown.root.textContent).not.toContain("Request not found");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.querySelector(".request h2") !== null, "the exact linked request");
  });

  test("reconnecting an empty queue keeps its last count, marked as refreshing, never as current", async () => {
    rows = [];
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the confirmed empty queue");
    streamImmediately = false;
    await click(labelled(shown.root, "button", "Reconnect"));
    await until(() => requests.filter((request) => request.path === "api/stream").length === 2, "the replacement stream");
    expect(shown.root.textContent).toContain("Pending (0 · refreshing)");
    expect(one(shown.root, ".queue-state").textContent).toBe("Reconnecting…");
    expect(shown.root.textContent).not.toContain("Queue complete");
    expect(shown.root.textContent).not.toContain("No requests waiting");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the refreshed empty queue");
  });

  test("failed checkout reads keep the last counts, marked, and name the queue that failed", async () => {
    issues = [{ root: "/project/tree/other", message: "Queue could not be read" }];
    const page = await open();
    expect(page.root.textContent).toContain("Pending (1 · refreshing)");
    expect(one(page.root, ".queue-state").textContent).toBe("other unavailable: Queue could not be read");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
    expect(page.root.textContent).toContain("Queue could not be read");
    expect(page.root.textContent).not.toContain("Queue complete");
    rows = [];
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    expect(page.root.textContent).not.toContain("No requests waiting");
    expect(page.root.textContent).not.toContain("Request not found");
    expect(page.root.textContent).toContain("Requested review unavailable");
  });

  test("a disconnected empty queue keeps its count marked as refreshing during the reconnect delay", async () => {
    rows = [];
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the empty snapshot");
    await act(async () => stream?.error(new Error("Connection lost")));
    await until(() => shown?.root.textContent?.includes("Reconnecting") ?? false, "the retry status");
    expect(shown.root.textContent).toContain("Pending (0 · refreshing)");
    expect(one(shown.root, ".queue-state").textContent).toContain("Reconnecting — ");
    expect(shown.root.textContent).not.toContain("Queue complete");
    expect(shown.root.textContent).not.toContain("No requests waiting");
  });

  test("identical heartbeats do not cancel a request detail that is still loading", async () => {
    let finish = () => {};
    detailWait.set("tree-q1", new Promise<void>((resolve) => { finish = resolve; }));
    shown = mount(<App />);
    await until(() => requests.some((request) => request.path === "api/reviews/tree-q1"), "the detail fetch");
    for (const _heartbeat of [1, 2]) {
      await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    }
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(1);
    await act(async () => finish());
    await until(() => shown?.root.querySelector(".request h2") !== null, "the uninterrupted detail result");
    detail.preview_unavailable = "The file changed while the queue was open.";
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.textContent?.includes(detail.preview_unavailable) ?? false, "a completed request's next refresh");
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(2);
  });

  test("opening a new deep link refreshes the queue instead of trusting an old empty snapshot", async () => {
    rows = [];
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the first empty snapshot");
    const later = addRequest("tree-later");
    streamImmediately = false;
    await act(async () => { window.location.hash = "review=tree-later"; });
    await until(() => requests.filter((request) => request.path === "api/stream").length === 2, "a fresh stream for the direct link");
    expect(shown.root.textContent).toContain("Loading requested review…");
    expect(shown.root.textContent).not.toContain("Request not found");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.querySelector(".request .reason")?.textContent === later.summary.reason, "the freshly linked request");
  });

  test.each(["explicit", "automatic"])("%s reconnect releases a held detail fetch and ignores its late response", async (mode) => {
    let finish = () => {};
    detailWait.set("tree-q1", new Promise<void>((resolve) => { finish = resolve; }));
    shown = mount(<App />);
    await until(() => requests.some((request) => request.path === "api/reviews/tree-q1"), "the held detail request");
    const fresh = review();
    fresh.summary.title = "Fresh request after reconnect";
    details.set("tree-q1", fresh);
    detailWait.delete("tree-q1");
    if (mode === "explicit") await click(labelled(shown.root, "button", "Reconnect"));
    else await act(async () => stream?.error(new Error("Connection lost")));
    await until(() => shown?.root.querySelector(".request h2")?.textContent?.endsWith(fresh.summary.title) ?? false, "a fresh detail fetch after reconnect", 1000);
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(2);
    await act(async () => finish());
    expect(shown.root.querySelector(".request h2")?.textContent?.endsWith(fresh.summary.title)).toBe(true);
  });

  test("unchanged queue heartbeats still refresh the selected request, keeping the draft", async () => {
    const page = await open();
    await comment("Keep the draft during live refreshes.");
    detail.preview_unavailable = "The captured file changed on disk.";
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.textContent?.includes(detail.preview_unavailable) ?? false, "the refreshed detail");
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(2);
    rows = [{ ...summary, answerable: false, unanswerable: "Only operator-two may answer it." }];
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.textContent?.includes("Only operator-two may answer it.") ?? false, "why it cannot be answered here");
    expect(page.root.querySelectorAll(".actions button.approve, .actions button.decline")).toHaveLength(0);
    expect(one(page.root, ".actions .unanswerable").textContent).toBe("Only operator-two may answer it.");
    expect(one(page.root, ".queue-row .state").textContent).toBe("can't answer here");
    expect(one(page.root, ".queue-row .row-unanswerable").textContent).toBe("Only operator-two may answer it.");
    expect(box(page.root).value).toBe("Keep the draft during live refreshes.");
  });

  test("large complete records mount only when requested and preserve the whole payload", async () => {
    detail.question.operation.payload.patch = "Complete evidence ".repeat(50000);
    const page = await open();
    expect(page.root.querySelector(".request-record")).toBeNull();
    await click(labelled(page.root, "button", "Details"));
    expect(JSON.parse(one(page.root, ".request-record > pre").textContent ?? "")).toEqual(detail.question.operation.payload);
    expect(page.root.querySelector(".record pre")).toBeNull();
    await comment("Typing should retain the already rendered evidence.");
    expect(JSON.parse(one(page.root, ".request-record > pre").textContent ?? "")).toEqual(detail.question.operation.payload);
    await click(one<HTMLElement>(page.root, ".record summary"));
    await until(() => page.root.querySelector(".record pre") !== null, "the requested full record");
    expect(JSON.parse(one(page.root, ".record pre").textContent ?? "")).toEqual(detail.question);
    await click(labelled(page.root, "button", "Details"));
    expect(page.root.querySelector(".request-record")).toBeNull();
  });

  test("shows highlighted complete evidence and submits an exact approval with the comment", async () => {
    const page = await open();
    expect(one(page.root, ".diff-line.remove .line-content").textContent).toBe("before");
    expect(one(page.root, ".diff-line.add .line-content").textContent).toBe("after");
    expect([...page.root.querySelectorAll(".diff-line .line-number")].map((cell) => cell.textContent)).toEqual(["1", "", "", "1"]);
    expect(one(page.root, ".file-bar .change-counts").textContent).toBe("+1 −1");
    await click(labelled(page.root, "button", "Details"));
    await click(one<HTMLElement>(page.root, ".record summary"));
    expect(one(page.root, ".record pre").textContent).toContain("Complete requested patch");
    await click(labelled(page.root, "button", "Raw"));
    expect(one(page.root, ".file-evidence pre").textContent).toBe(detail.files[0]?.unified ?? "");
    await comment("Keep the public signature.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".toast.done") !== null, "the approval toast");
    expect(posted()[0]?.body).toEqual({ approved: true, note: "Keep the public signature.", comments: [], fingerprint: "bound-tree-q1" });
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    expect(window.location.hash).not.toContain("token");
    expect(one(page.root, ".toast.done").textContent).toContain(DELIVERED.detail);
    expect(page.root.textContent).toContain("Queue complete");
    expect(page.root.querySelector(".composer")).toBeNull();
    await click(labelled(page.root, "button", "History (1)"));
    await until(() => page.root.textContent?.includes("Approved by operator") ?? false, "the history detail");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
  });

  test("a refused approval rolls back where the operator sees it, its drafts restored", async () => {
    addRequest();
    const page = await open();
    answerStatus = 409;
    await comment("Use a scoped patch.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".toast.failed") !== null, "the refusal toast");
    expect(one(page.root, ".toast.failed").textContent).toContain("The file changed; refresh the request.");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(2);
    expect(one(page.root, ".row-failed").textContent).toBe("The file changed; refresh the request.");
    await click(labelled(one(page.root, ".toast.failed"), "button", "Open it"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === summary.reason, "the refused request again");
    expect(box(page.root).value).toBe("Use a scoped patch.");
    expect(one(page.root, ".composer .error").textContent).toBe("The file changed; refresh the request.");
    answerStatus = 200;
    await click(labelled(page.root, "button", "Decline"));
    await until(() => posted().length === 2, "the decline");
    expect(posted()[1]?.body).toEqual({ approved: false, note: "Use a scoped patch.", comments: [], fingerprint: "bound-tree-q1" });
  });

  test("live snapshots add requests, and a stale review leaves the queue for History saying what moved", async () => {
    const page = await open();
    const stale = addRequest();
    stale.summary.state = "stale";
    stale.summary.answerable = false;
    stale.summary.stale = [{ path: "/project/vocabulary.py", cause: "changed" }];
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.textContent?.includes("History (1)") ?? false, "the stale review in History");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
    await click(labelled(page.root, "button", "History (1)"));
    expect(one(page.root, ".row-stale").textContent).toBe("Stale: vocabulary.py changed since this was recorded");
    await click(one<HTMLElement>(page.root, ".queue-row"));
    await until(() => page.root.querySelector(".stale-reason") !== null, "the stale reason on the request");
    expect(one(page.root, ".stale-reason").textContent).toContain("vocabulary.py changed since this was recorded");
    expect(page.root.querySelector(".composer")).toBeNull();
    page.unmount();
    shown = null;
    expect(streamingAborted).toBe(true);
  });

  test("History past what the stream carries is read a page at a time", async () => {
    older = [{ ...summary, key: "tree-old", id: "old", state: "completed", title: "An older request", settled: "2026-09-20T12:00:00Z" }];
    const page = await open();
    await click(labelled(page.root, "button", "History (1)"));
    expect(page.root.textContent).not.toContain("An older request");
    await click(labelled(page.root, "button", "Load older requests (1 more)"));
    await until(() => page.root.textContent?.includes("An older request") ?? false, "the older page");
    expect(requests.map((request) => request.path)).toContain("api/reviews/history?offset=0&limit=50");
    expect(page.root.textContent).not.toContain("Load older requests");
  });

  test("the next request is read ahead, and opening it reads nothing more", async () => {
    addRequest();
    const page = await open();
    await until(() => requests.some((request) => request.path === "api/reviews/tree-q2"), "the next request read ahead");
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request at once");
    expect(requests.filter((request) => request.path === "api/reviews/tree-q2")).toHaveLength(1);
  });

  test("a link to a request past what the stream carries is found in History", async () => {
    older = [{ ...summary, key: "tree-old", id: "old", state: "completed", title: "An older request", settled: "2026-09-20T12:00:00Z" }];
    const kept = review("tree-old");
    kept.summary = { ...kept.summary, ...older[0], key: "tree-old" };
    details.set("tree-old", kept);
    window.history.replaceState(null, "", "/#token=browser-secret&review=old&root=tree");
    shown = mount(<App />);
    await until(() => shown?.root.querySelector(".request h2")?.textContent?.endsWith("An older request") ?? false, "the linked older request");
    expect(requests.some((request) => request.path.startsWith("api/reviews/history?") && request.path.includes("review=old"))).toBe(true);
    expect(labelled(shown.root, "button", "History (1)").getAttribute("aria-pressed")).toBe("true");
  });

  test("missing or expired browser authorization gives launch instructions without selecting another review", async () => {
    window.history.replaceState(null, "", "/#review=missing");
    refreshStatus = 401;
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("not authorized") ?? false, "browser authorization instructions");
    expect(shown.root.textContent).toContain("missing");
    expect(shown.root.textContent).toContain("launch link");
    expect(requests).toHaveLength(1);
    expect(requests[0]?.authorization).toBeNull();
  });

  test("a newer request never replaces the request or comment being reviewed", async () => {
    const page = await open();
    await comment("This comment belongs to the first request.");
    const newer = addRequest();
    rows = [newer.summary, { ...summary }];
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.querySelectorAll(".queue-row").length === 2, "the newer request");
    expect(one(page.root, ".queue-row.selected").textContent).toContain(summary.title);
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(box(page.root).value).toBe("This comment belongs to the first request.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => requests.some((request) => request.method === "POST"), "the original request approval");
    expect(requests.find((request) => request.method === "POST")?.path).toBe("api/reviews/tree-q1/answer");
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the remaining newer request");
  });

  test("a disconnected stream reports failure and reconnects without losing the comment", async () => {
    const page = await open();
    await comment("Preserve this draft.");
    await act(async () => stream?.error(new Error("Connection lost")));
    await until(() => page.root.textContent?.includes("Reconnecting") ?? false, "the reconnect status");
    streamImmediately = false;
    await click(labelled(page.root, "button", "Reconnect"));
    await until(() => requests.filter((request) => request.path === "api/stream").length === 2, "a fresh stream");
    expect(box(page.root).value).toBe("Preserve this draft.");
    expect(page.root.textContent).toContain("Pending (1 · refreshing)");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    expect(page.root.textContent).toContain("Live");
  });

  test("the file navigator switches one diff at a time and keeps complete long evidence accessible", async () => {
    const other = review().files[0];
    if (other === undefined) throw new Error("fixture lacks a file");
    other.path = "/project/other.py";
    other.after = `after\n${"long content ".repeat(1000)}`;
    detail.files.push(other);
    const page = await open();
    expect(page.root.querySelectorAll(".file-overview li")).toHaveLength(2);
    expect(page.root.querySelectorAll(".file")).toHaveLength(1);
    expect(one(page.root, ".file-overview .file-prefix code").textContent).toBe("/project/");
    expect([...page.root.querySelectorAll(".file-list code")].map((item) => item.textContent)).toEqual(["file.py", "other.py"]);
    expect(page.root.textContent).not.toContain(other.after);
    await click(one<HTMLElement>(page.root, ".file-list li:nth-child(2) button"));
    expect(one(page.root, ".file-name").textContent).toBe("other.py");
    expect(page.root.querySelectorAll(".file")).toHaveLength(1);
    await click(labelled(page.root, "button", "After"));
    expect(sourceText(page.root)).toBe(other.after);
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-name").textContent).toBe("other.py");
  });

  test("file search keeps the selected evidence stable until a matching file is chosen", async () => {
    const other = review().files[0];
    if (other === undefined) throw new Error("fixture lacks a file");
    other.path = "/project/nested/percent%#name.py";
    detail.files.push(other);
    const page = await open();
    const search = one<HTMLInputElement>(page.root, ".file-search input");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter === undefined) throw new Error("input has no value setter");
    await act(async () => { setter.call(search, "percent%"); search.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    await keydown("]", "BracketRight", {}, search);
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    await click(one<HTMLElement>(page.root, ".file-list button"));
    expect(one(page.root, ".file-name").textContent).toBe("nested/percent%#name.py");
    expect(one(page.root, ".file-name").getAttribute("title")).toBe(other.path);
  });

  test.each(["/project/100%done/", "/project/literal%20/日本語 #?%2F/"])("file navigation preserves literal directory characters in %s", async (directory) => {
    const first = detail.files[0];
    const other = review().files[0];
    if (first === undefined || other === undefined) throw new Error("fixture lacks a file");
    first.path = `${directory}first.py`;
    other.path = `${directory}second%20.py`;
    detail.files.push(other);
    const page = await open();
    expect(one(page.root, ".file-overview .file-prefix code").textContent).toBe(directory);
    expect([...page.root.querySelectorAll(".file-list code")].map((item) => item.textContent)).toEqual(["first.py", "second%20.py"]);
    expect(one(page.root, ".file-list li:nth-child(2) code").getAttribute("title")).toBe(other.path);
    await click(one<HTMLElement>(page.root, ".file-list li:nth-child(2) button"));
    expect(one(page.root, ".file-name").textContent).toBe("second%20.py");
    expect(one(page.root, ".file-name").getAttribute("title")).toBe(other.path);
  });

  test("the editor is the centre: context above it in single lines, the composer pinned beneath", async () => {
    detail.command = "sed -i 's/before/after/' file.py";
    detail.preview_notice = "The execution environment was not captured.\n".repeat(100);
    detail.summary.reason = "Review the captured changes.\n".repeat(100);
    const page = await open();
    const request = one(page.root, ".request");
    expect([...request.children].map((child) => child.className.split(" ")[0])).toEqual(
      ["request-bar", "request-meta", "why", "command", "preview-note", "files"]);
    expect(request.lastElementChild).toBe(one(page.root, ".files"));
    expect(request.nextElementSibling).toBe(one(page.root, ".composer"));
    expect(one(page.root, ".composer").parentElement).toBe(one(page.root, ".stage"));
    expect(one(page.root, ".why .reason").textContent).toBe(detail.summary.reason);
    expect(one(page.root, ".why h3").textContent).toBe(`Why approval is needed · ${summary.rule}`);
    await comment("Keep this draft.");
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(false);
    expect(box(page.root).value).toBe("Keep this draft.");
    const style = document.createElement("style");
    style.textContent = await Bun.file(new URL("./styles.css", import.meta.url)).text();
    document.head.append(style);
    try {
      expect(getComputedStyle(one(page.root, ".files")).flexGrow).toBe("1");
      expect(getComputedStyle(one(page.root, ".composer")).flexShrink).toBe("0");
      expect(getComputedStyle(one(page.root, ".file-evidence")).overflow).toBe("auto");
      expect(getComputedStyle(one(page.root, ".request-bar h2")).whiteSpace).toBe("nowrap");
      expect(getComputedStyle(one(page.root, ".toasts")).position).toBe("fixed");
    } finally {
      style.remove();
    }
  });

  test("the comment box is open and focused on a waiting review, Esc leaves it and c comes back", async () => {
    const page = await open();
    await until(() => document.activeElement === box(page.root), "the focused comment box");
    await keydown("Escape", "Escape", {}, box(page.root));
    expect(document.activeElement).not.toBe(box(page.root));
    await keydown("c", "KeyC");
    await keyup("c", "KeyC");
    expect(document.activeElement).toBe(box(page.root));
    await click(labelled(page.root, "button", "Queue (1)"));
    expect(one(page.root, ".workspace").getAttribute("data-mobile-panel")).toBe("queue");
    await click(one<HTMLElement>(page.root, ".queue-row"));
    expect(one(page.root, ".workspace").getAttribute("data-mobile-panel")).toBe("review");
  });

  test("Ctrl+Enter approves and Alt+Delete declines, and a held key cannot answer the next request", async () => {
    addRequest();
    const page = await open();
    await keydown("Enter", "Enter", { ctrlKey: true }, box(page.root));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "automatic queue advance");
    await keydown("Enter", "Enter", { ctrlKey: true, repeat: true });
    await keydown("Enter", "Enter", { ctrlKey: true });
    expect(posted()).toHaveLength(1);
    await keyup("Enter", "Enter");
    await keydown("Backspace", "Backspace", { altKey: true }, box(page.root));
    await keyup("Backspace", "Backspace");
    expect(posted()).toHaveLength(1);
    await decline(box(page.root));
    await until(() => page.root.textContent?.includes("Queue complete") ?? false, "queue completion");
    expect(posted().map((request) => ({ path: request.path, body: request.body }))).toEqual([
      { path: "api/reviews/tree-q1/answer", body: { approved: true, note: "", comments: [], fingerprint: "bound-tree-q1" } },
      { path: "api/reviews/tree-q2/answer", body: { approved: false, note: "", comments: [], fingerprint: "bound-tree-q2" } },
    ]);
  });

  test("one-letter keys wait until Esc leaves the box, Alt+arrows move from inside it, drafts stay with their review", async () => {
    addRequest();
    const page = await open();
    await comment("First draft");
    await keydown("j", "KeyJ", {}, box(page.root));
    await keyup("j", "KeyJ");
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    await keydown("Enter", "Enter", { ctrlKey: true, isComposing: true }, box(page.root));
    expect(posted()).toHaveLength(0);
    await keydown("ArrowDown", "ArrowDown", { altKey: true }, box(page.root));
    await keyup("ArrowDown", "ArrowDown");
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "Alt+Down from inside the box");
    expect(box(page.root).value).toBe("");
    await keydown("Escape", "Escape", {}, box(page.root));
    await keydown("k", "KeyK");
    await keyup("k", "KeyK");
    await until(() => page.root.querySelector(".request .reason")?.textContent === summary.reason, "k once the box is left");
    expect(box(page.root).value).toBe("First draft");
    await click(labelled(page.root, "button", "Keyboard shortcuts"));
    const help = one(page.root, "#shortcut-help").textContent ?? "";
    for (const meaning of ["Approve", "Decline", "without deciding", "Leave the comment box", "marker", "Whole file"]) expect(help).toContain(meaning);
    expect(help).not.toContain("Shift + A");
  });

  test("an answer on its way never holds the page, and is never sent twice", async () => {
    addRequest();
    const page = await open();
    let finish: (() => void) | undefined;
    answerWait = new Promise<void>((resolve) => { finish = resolve; });
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request at once");
    expect(one(page.root, ".toast.sending").textContent).toContain("Approving…");
    await click(one<HTMLElement>(page.root, ".queue-row"));
    expect(posted()).toHaveLength(1);
    await act(async () => { finish?.(); });
    await until(() => page.root.querySelector(".toast.done") !== null, "the reconciled toast");
    expect(one(page.root, ".toast.done").textContent).toContain(DELIVERED.detail);
    expect(posted()).toHaveLength(1);
  });

  test("recorded decisions advance without waiting for another queue read", async () => {
    addRequest();
    const page = await open();
    refreshStatus = 503;
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "next request from the live snapshot");
    expect(requests.some((request) => request.path === "api/reviews")).toBe(false);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(false);
    await until(() => page.root.querySelector(".toast.done") !== null, "the approval toast");
  });

  test("an older pending snapshot cannot undo a recorded decision before the stream catches up", async () => {
    addRequest();
    const previous = queue();
    const page = await open();
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(previous))));
    expect(labelled(page.root, "button", "Pending (1)")).toBeTruthy();
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
    const completed = rows.find((row) => row.key === "tree-q1");
    if (completed === undefined) throw new Error("fixture lacks the completed request");
    completed.title = "Server-confirmed history title";
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await click(labelled(page.root, "button", "History (1)"));
    expect(one(page.root, ".queue-row").textContent).toContain("Server-confirmed history title");
  });

  test("sending comments without deciding keeps the review waiting and shows them in its thread", async () => {
    const page = await open();
    await comment("Why not a flag?");
    await keydown("Enter", "Enter", { altKey: true }, box(page.root));
    await keyup("Enter", "Enter");
    await until(() => posted().length === 1, "the remark");
    expect(posted()[0]?.path).toBe("api/reviews/tree-q1/remark");
    expect(posted()[0]?.body).toEqual({ note: "Why not a flag?", comments: [], fingerprint: "bound-tree-q1" });
    await until(() => page.root.querySelector(".toast.done") !== null, "the remark toast");
    expect(one(page.root, ".toast.done").textContent).toContain("Its `review wait` holds it");
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(page.root.querySelector(".composer")).not.toBeNull();
    expect(box(page.root).value).toBe("");
    await until(() => page.root.querySelector(".thread .said.remark") !== null, "the remark in the thread");
    expect(one(page.root, ".thread .said.remark").textContent).toContain("Why not a flag?");
  });

  test("automatic queue advancement can be paused", async () => {
    addRequest();
    const page = await open();
    await click(one<HTMLElement>(page.root, ".queue-setting input"));
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.textContent?.includes("Approved by operator") ?? false, "the retained decision");
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(page.root.querySelector(".composer")).toBeNull();
    await click(one<HTMLElement>(page.root, ".queue-row"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "manual advance");
  });

  test("a slow next-request load cannot submit the previously displayed fingerprint", async () => {
    addRequest();
    let finish: (() => void) | undefined;
    // Held from the start, so reading it ahead of the operator is as slow as opening it.
    detailWait.set("tree-q2", new Promise<void>((resolve) => { finish = resolve; }));
    const page = await open();
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.textContent?.includes("Loading request…") ?? false, "the pending detail read");
    await approve();
    expect(posted()).toHaveLength(0);
    await act(async () => { finish?.(); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the loaded next request");
    await approve();
    expect(posted()[0]?.body).toEqual({ approved: true, note: "", comments: [], fingerprint: "bound-tree-q2" });
  });

  test("default review skips automatic files and full operation restores their evidence without changing approval", async () => {
    const automatic = review().files[0];
    if (automatic === undefined) throw new Error("fixture lacks a file");
    automatic.path = "/project/tests/test_auto.py";
    automatic.review_effect = "allow";
    automatic.review_reason = "This edit passes automatically.";
    automatic.additions = 50;
    detail.files.unshift(automatic);
    const page = await open();
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    expect(one(page.root, ".file-overview-heading .change-counts").textContent).toBe("+1 −1");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("1 / 1");
    await click(labelled(page.root, "button", "Full operation (2)"));
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(2);
    expect(one(page.root, ".file-list li:first-child .file-review-state").textContent).toBe("Automatic");
    await click(one<HTMLElement>(page.root, ".file-list li:first-child button"));
    expect(one(page.root, ".file-name").textContent).toBe("tests/test_auto.py");
    await click(labelled(page.root, "button", "Needs review (1)"));
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    await click(labelled(page.root, "button", "Approve"));
    await until(() => requests.some((request) => request.method === "POST"), "the complete operation approval");
    expect(posted()[0]?.body).toEqual({ approved: true, note: "", comments: [], fingerprint: "bound-tree-q1" });
  });

  test("queue and request headings use the backend's review subset with a secondary full-operation count", async () => {
    detail.summary.title = "Change 2 files requiring review";
    detail.summary.paths = ["src/first.py", "src/second.py"];
    detail.summary.total_files = 5;
    rows = [detail.summary];
    const page = await open();
    expect(one(page.root, ".queue-row strong").textContent).toBe(detail.summary.title);
    expect(one(page.root, ".request h2").textContent).toContain(detail.summary.title);
    expect(one(page.root, ".review-file-count").textContent).toBe("2 files to review · 5 submitted");
    expect(one(page.root, ".queue-files summary").textContent).toBe("2 files to review");
    await click(one<HTMLElement>(page.root, ".queue-files summary"));
    expect([...page.root.querySelectorAll(".queue-files code")].map((node) => node.textContent)).toEqual(detail.summary.paths);
  });

  test("captured native deferrals stay outside review counts and navigation while unknown files stay visible", async () => {
    const asked = detail.files[0];
    const automatic = review().files[0];
    const deferred = review().files[0];
    const unknown = review().files[0];
    if (asked === undefined || automatic === undefined || deferred === undefined || unknown === undefined) throw new Error("fixture lacks files");
    asked.path = "/project/asked.py";
    automatic.path = "/project/automatic.py";
    automatic.review_effect = "allow";
    automatic.additions = 50;
    deferred.path = "/project/native.py";
    deferred.review_effect = "defer";
    deferred.review_reason = "The native provider applies its own permission policy.";
    deferred.additions = 100;
    unknown.path = "/project/unknown.py";
    unknown.review_effect = "unknown";
    detail.files = [automatic, asked, deferred, unknown];
    const page = await open();
    expect(one(page.root, ".file-name").textContent).toBe("asked.py");
    expect([...page.root.querySelectorAll(".file-list code")].map((node) => node.textContent)).toEqual(["asked.py", "unknown.py"]);
    expect(one(page.root, ".file-overview-heading .change-counts").textContent).toBe("+2 −2");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-name").textContent).toBe("unknown.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("2 / 2");
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-name").textContent).toBe("asked.py");
    await click(labelled(page.root, "button", "Full operation (4)"));
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(4);
    const native = one<HTMLElement>(page.root, ".file-list li:nth-child(3) button");
    expect(one(native, ".file-review-state").textContent).toBe("Native decision");
    await click(native);
    await click(one<HTMLElement>(page.root, ".file-bar .file-review-state"));
    expect(one(page.root, ".file-why").textContent).toContain(`No Lup approval requested; the native provider decides. ${deferred.review_reason}`);
    await click(labelled(page.root, "button", "Needs review (2)"));
    expect(one(page.root, ".file-name").textContent).toBe("asked.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(2);
    await click(labelled(page.root, "button", "Approve"));
    await until(() => requests.some((request) => request.method === "POST"), "the complete operation approval");
    expect(posted()[0]?.body).toEqual({ approved: true, note: "", comments: [], fingerprint: "bound-tree-q1" });
  });

  test("native-deferred exceptions stay out of review highlights and exception shortcuts", async () => {
    const file = detail.files[0];
    const added = file?.hunks[0]?.lines[1];
    if (file === undefined || added === undefined) throw new Error("fixture lacks its change");
    const asked = exception({ line: 1, rule_ids: ["asked-rule"], reason: "Needs review", introduced: true });
    const deferred = exception({ line: 2, rule_ids: ["native-rule"], reason: "Native decision", introduced: true });
    deferred.review_effect = "defer";
    deferred.review_reason = "No Lup approval requested.";
    file.suppressions = [asked, deferred];
    added.text = "# lup: ignore[asked-rule]\n";
    added.suppression = true;
    file.hunks[0]?.lines.push({ kind: "add", text: "# lup: ignore[native-rule]\n", old_line: null, new_line: 2, suppression: true });
    if (file.hunks[0] !== undefined) file.hunks[0].new_end = 2;
    file.after = `${added.text}# lup: ignore[native-rule]\n`;
    const page = await open();
    expect(page.root.querySelectorAll(".diff-line.suppression")).toHaveLength(1);
    await click(labelled(page.root, "button", "Exceptions (1)"));
    expect([...page.root.querySelectorAll(".suppression-group summary code")].map((node) => node.textContent)).toEqual(["asked-rule"]);
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    const first = one(page.root, ".diff-line.suppression");
    expect(document.activeElement).toBe(first);
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(document.activeElement).toBe(first);
    await click(labelled(page.root, "button", "After"));
    expect(sourceText(page.root)).toBe(file.after.replace(/\n$/, ""));
    expect(page.root.querySelectorAll(".diff.source .diff-line.suppression")).toHaveLength(1);
    await click(labelled(page.root, "button", "Full operation (1)"));
    expect([...page.root.querySelectorAll(".suppression-group summary code")].map((node) => node.textContent)).toEqual(["asked-rule", "native-rule"]);
    expect(page.root.querySelectorAll(".diff-line.suppression")).toHaveLength(2);
    expect(one(page.root, ".suppression-group:last-child .file-review-state").textContent).toBe("Native decision");
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(document.activeElement).toBe([...page.root.querySelectorAll(".diff-line")].at(-1) ?? null);
  });

  test("default exceptions show only the asked rules and leave existing directives unhighlighted", async () => {
    const file = detail.files[0];
    const added = file?.hunks[0]?.lines[1];
    if (file === undefined || added === undefined) throw new Error("fixture lacks its change");
    const existing = exception({ line: 2, rule_ids: ["existing-rule"], reason: "Established exception", introduced: false });
    const changed = exception({ line: 1, rule_ids: ["existing-rule", "new-rule"], reason: "One added exemption", introduced: true });
    changed.review_rule_ids = ["new-rule"];
    file.suppressions = [changed, existing];
    added.text = "# lup: ignore[existing-rule, new-rule]\n";
    added.suppression = true;
    file.hunks[0]?.lines.push({ kind: "context", text: "# lup: ignore[existing-rule]\n", old_line: 2, new_line: 2, suppression: true });
    if (file.hunks[0] !== undefined) Object.assign(file.hunks[0], { old_end: 2, new_end: 2 });
    file.after = `${added.text}# lup: ignore[existing-rule]\n`;
    const page = await open();
    expect(page.root.querySelectorAll(".diff-line.suppression")).toHaveLength(1);
    expect(one(page.root, ".diff-line.context").classList.contains("suppression")).toBe(false);
    await click(labelled(page.root, "button", "Exceptions (1)"));
    expect([...page.root.querySelectorAll(".suppression-group summary code")].map((node) => node.textContent)).toEqual(["new-rule"]);
    expect(one(page.root, ".exception-counts").textContent).toContain("1 added · 0 existing");
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(document.activeElement).toBe(one(page.root, ".diff-line.add"));
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(document.activeElement).toBe(one(page.root, ".diff-line.add"));
    await click(labelled(page.root, "button", "After"));
    expect(sourceText(page.root)).toBe(file.after.replace(/\n$/, ""));
    expect(page.root.querySelectorAll(".diff.source .diff-line.suppression")).toHaveLength(1);
    await click(labelled(page.root, "button", "Full operation (1)"));
    expect([...page.root.querySelectorAll(".suppression-group summary code")].map((node) => node.textContent)).toEqual(["existing-rule", "new-rule"]);
    expect(one(page.root, ".exception-counts").textContent).toContain("2 occurrences");
    expect(page.root.querySelectorAll(".diff-line.suppression")).toHaveLength(2);
  });

  test("file search counts and shortcuts stay within the displayed review set", async () => {
    const first = detail.files[0];
    const hidden = review().files[0];
    const last = review().files[0];
    if (first === undefined || hidden === undefined || last === undefined) throw new Error("fixture lacks files");
    first.path = "/project/match-first.py";
    hidden.path = "/project/other.py";
    last.path = "/project/match-last.py";
    detail.files.push(hidden, last);
    const page = await open();
    const search = one<HTMLInputElement>(page.root, ".file-search input");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter === undefined) throw new Error("input has no value setter");
    await act(async () => { setter.call(search, "match-"); search.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(one(page.root, ".file-overview-heading h3").textContent).toBe("2 files matching");
    expect(one(page.root, ".file-overview-heading .change-counts").textContent).toBe("+2 −2");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-name").textContent).toBe("match-last.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("2 / 2");
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-name").textContent).toBe("match-first.py");
  });

  test("unknown file attribution stays visible even when the path looks like a test", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.path = "/project/tests/test_uncaptured.py";
    file.review_effect = "unknown";
    file.review_reason = "This historical request did not capture per-file policy decisions.";
    const page = await open();
    expect(one(page.root, ".file-bar .file-review-state").textContent).toBe("Unclassified");
    await click(one<HTMLElement>(page.root, ".file-bar .file-review-state"));
    expect(one(page.root, ".file-why").textContent).toContain(file.review_reason);
  });

  test("expanded file reasons and paths remain complete inside a bounded scrolling strip", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.review_reason = "A captured directive site requires review.\n".repeat(150);
    file.path = `/project/${"nested/".repeat(50)}source.py`;
    const style = document.createElement("style");
    style.textContent = await Bun.file(new URL("./styles.css", import.meta.url)).text();
    document.head.append(style);
    try {
      const page = await open();
      await click(one<HTMLElement>(page.root, ".file-bar .file-review-state"));
      expect(one(page.root, ".file-why").textContent).toContain(file.review_reason);
      expect(one(page.root, ".file-why code").textContent).toBe(file.path);
      const layout = getComputedStyle(one(page.root, ".file-why"));
      expect(layout.overflow).toBe("auto");
      expect(layout.maxHeight).not.toBe("none");
      expect(one(page.root, ".file-evidence").parentElement).toBe(one(page.root, ".file"));
    } finally {
      style.remove();
    }
  });

  test.each(["allow", "defer"] as const)("command-level requests retain the exact command when every file decision is %s", async (effect) => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.review_effect = effect;
    file.review_reason = "No Lup file approval is requested; the command itself requires approval.";
    detail.command = "sed -i 's/before/after/' /project/file.py";
    const page = await open();
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(0);
    expect(page.root.querySelectorAll(".diff")).toHaveLength(0);
    expect(one(page.root, ".no-file-review pre").textContent).toBe(detail.command);
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(page.root.querySelectorAll(".diff")).toHaveLength(0);
    await click(labelled(page.root, "button", "Full operation (1)"));
    expect(page.root.querySelectorAll(".diff")).toHaveLength(1);
    await click(labelled(page.root, "button", "Needs review (0)"));
    expect(one(page.root, ".no-file-review pre").textContent).toBe(detail.command);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(false);
  });

  test("rule exceptions are highlighted and jump to added or unchanged source lines", async () => {
    const file = detail.files[0];
    const line = file?.hunks[0]?.lines[1];
    if (file === undefined || line === undefined) throw new Error("fixture lacks its change");
    line.text = "# lup: ignore[constant-declaration] - External protocol value\n";
    line.suppression = true;
    file.after = `${line.text}second\nthird\nfourth\nfifth\n# lup: ignore\n`;
    file.suppressions = [
      exception({ line: 1, rule_ids: ["constant-declaration"], reason: "External protocol value", introduced: true }),
      exception({ line: 6, rule_ids: null, reason: "", introduced: false }),
    ];
    const page = await open();
    await click(labelled(page.root, "button", "Full operation (1)"));
    await click(labelled(page.root, "button", "Exceptions (2)"));
    const overview = one(page.root, ".suppression-overview");
    expect(overview.textContent).toContain("2 occurrences");
    expect(overview.textContent).toContain("1 added · 1 existing");
    expect(overview.textContent).toContain("constant-declaration");
    expect(overview.textContent).toContain("External protocol value");
    expect(overview.textContent).toContain("Unscoped directive");
    expect(overview.textContent).toContain("No reason supplied");
    expect(one(page.root, ".diff-line.suppression code").textContent).toBe(line.text.replace(/\n$/, ""));
    expect(overview.querySelectorAll("details[open]")).toHaveLength(0);
    await click(one<HTMLElement>(overview, "details:first-of-type summary"));
    await click(one<HTMLElement>(overview, "details:first-of-type li button"));
    expect(document.activeElement).toBe(one(page.root, ".diff-line.suppression"));
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(labelled(page.root, "button", "Whole file").getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement?.querySelectorAll(".line-number")[1]?.textContent).toBe("6");
    expect(document.activeElement?.classList.contains("suppression")).toBe(true);
    await keydown("p", "KeyP");
    await keyup("p", "KeyP");
    expect(document.activeElement).toBe(one(page.root, ".diff-line.suppression"));
    expect(page.root.querySelector(".line-content .suppression-badge")).toBeNull();
  });

  test("exceptions shared across files group by rule and navigate to the matching source", async () => {
    const first = detail.files[0];
    const second = review().files[0];
    if (first === undefined || second === undefined) throw new Error("fixture lacks files");
    first.suppressions = [exception({ line: 1, rule_ids: ["constant-declaration"], reason: "First reason", introduced: true })];
    second.path = "/project/second.py";
    second.suppressions = [exception({ line: 1, rule_ids: ["constant-declaration"], reason: "Second reason", introduced: false })];
    detail.files.push(second);
    const page = await open();
    await click(labelled(page.root, "button", "Full operation (2)"));
    await click(labelled(page.root, "button", "Exceptions (2)"));
    expect(page.root.querySelectorAll(".suppression-group")).toHaveLength(1);
    const group = one<HTMLDetailsElement>(page.root, ".suppression-group");
    expect(group.open).toBe(false);
    expect(one(group, "summary").textContent).toContain("1 added · 1 existing");
    await click(one<HTMLElement>(group, "summary"));
    expect(group.textContent).toContain("First reason");
    expect(group.textContent).toContain("Second reason");
    await click(one<HTMLElement>(group, "li:nth-child(2) button"));
    expect(one(page.root, ".file-name").textContent).toBe("second.py");
    await keydown("p", "KeyP");
    await keyup("p", "KeyP");
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
  });

  test("shell file previews keep the exact command and unavailable-preview explanation visible", async () => {
    detail.command = "sed -i 's/before/after/' /project/file.py";
    detail.preview_unavailable = "One command target cannot be safely previewed.";
    detail.preview_notice = "Preview computed where the dashboard runs, from captured input. The execution environment was not captured.";
    const page = await open();
    expect(one(page.root, ".command pre").textContent).toBe(detail.command);
    expect(page.root.textContent).toContain(detail.preview_unavailable);
    await click(labelled(page.root, "button", "Details"));
    expect(one(page.root, ".request-record pre").textContent).toContain("Complete requested patch");
    expect(page.root.querySelectorAll(".diff-line")).toHaveLength(2);
    expect(page.root.querySelectorAll(".preview-note")).toHaveLength(1);
    expect(one<HTMLDetailsElement>(page.root, ".preview-note").open).toBe(false);
    expect(one(page.root, ".preview-note p").textContent).toBe(detail.preview_notice);
  });

  test("a command review shows each recorded file, leaves scratch out by default, and lists what only running shows", async () => {
    const scratch = review().files[0];
    if (scratch === undefined) throw new Error("fixture lacks a file");
    scratch.path = "/project/tmp/scratch.txt";
    scratch.review_effect = "allow";
    scratch.review_reason = "Scratch is allowed however it is written.";
    detail.files.unshift(scratch);
    detail.command = "printf 'x\\n' > tmp/scratch.txt && sed -i 's/before/after/' file.py && sort -o sorted.txt file.py";
    detail.question.operation = { tool: "Bash", cwd: "/project", payload: { command: detail.command } };
    detail.question.unpreviewed = [{ command: "sort -o sorted.txt file.py", paths: ["/project/sorted.txt"], cause: "run" }];
    detail.preview_notice = "Each document is the one the policy worked out when it judged this command, without running it.";
    const page = await open();
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    expect(one(page.root, ".file-name").textContent).toBe("file.py");
    expect(page.root.querySelectorAll(".diff-line")).toHaveLength(2);
    const steps = one(page.root, ".unpreviewed");
    expect(steps.textContent).toContain("1 step no document shows");
    expect(one(steps, ".unpreviewed-cause").textContent).toBe("Result known only after running");
    expect(one(steps, "pre").textContent).toBe("sort -o sorted.txt file.py");
    expect(one(steps, ".unpreviewed-paths code").textContent).toBe("/project/sorted.txt");
    expect(one(page.root, ".preview-note summary").textContent).toBe("How these documents were worked out");
    await click(labelled(page.root, "button", "Full operation (2)"));
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(2);
  });

  test("a command line shows every command that asks with its own reason, and folds the ones allowed alone", async () => {
    detail.files = [];
    detail.command = "ls && git push --delete origin old && git push --force origin feature";
    detail.question.operation = { tool: "Bash", cwd: "/project", payload: { command: detail.command } };
    detail.question.segments = [
      { command: "ls", effect: "allow", reason: "", rule: "shell:ls" },
      { command: "git push --delete origin old", effect: "ask", reason: "deleting a remote branch loses work", rule: "shell:git-push" },
      { command: "git push --force origin feature", effect: "ask", reason: "--force overwrites the remote branch", rule: "shell:git-push" },
    ];
    const page = await open();
    expect(one(page.root, ".command > pre").textContent).toBe(detail.command);
    const asking = [...page.root.querySelectorAll(".command > .segments > li")];
    expect(asking.map((row) => one(row, "pre").textContent)).toEqual(["git push --delete origin old", "git push --force origin feature"]);
    expect(asking.map((row) => one(row, "p").textContent)).toEqual(["deleting a remote branch loses work", "--force overwrites the remote branch"]);
    expect(asking.map((row) => one(row, ".file-review-state").textContent)).toEqual(["Needs approval", "Needs approval"]);
    expect(asking.map((row) => one(row, ".segment-rule").textContent)).toEqual(["shell:git-push", "shell:git-push"]);
    const allowed = one<HTMLDetailsElement>(page.root, ".segments-allowed");
    expect(allowed.open).toBe(false);
    expect(one(allowed, "summary").textContent).toBe("1 command allowed on its own");
    expect(one(allowed, "pre").textContent).toBe("ls");
  });

  test("a line of one command is its reason already, and lists nothing beneath it", async () => {
    detail.files = [];
    detail.command = "git push --delete origin old";
    detail.question.segments = [{ command: detail.command, effect: "ask", reason: "deleting a remote branch loses work", rule: "shell:git-push" }];
    const page = await open();
    expect(one(page.root, ".command > pre").textContent).toBe(detail.command);
    expect(page.root.querySelectorAll(".segments")).toHaveLength(0);
  });

  test("the agent's own account shows beside the reason, labelled by where it was found", async () => {
    detail.question.account = [
      { source: "justification", text: "The marker is outside the writable roots" },
      { source: "preceding", text: "The marker has to exist before the next step reads it.\n".repeat(8) },
    ];
    const page = await open();
    const said = [...page.root.querySelectorAll(".account .accounted")];
    expect(said.map((row) => one(row, ".account-source").textContent)).toEqual(["agent's reason to leave the sandbox", "agent said before this call"]);
    expect(one(said[1] as HTMLElement, "p").className).toBe("folded");
    await click(labelled(said[1] as HTMLElement, "button", "Show all"));
    expect(one(said[1] as HTMLElement, "p").className).toBe("");
    expect(one(said[1] as HTMLElement, "p").textContent).toBe(detail.question.account[1]?.text ?? "");
  });

  test("a token-free link opens the exact request in another tab using origin storage", async () => {
    const second = addRequest();
    localStorage.setItem("lup-dashboard-token", "browser-secret");
    window.history.replaceState(null, "", "/#review=tree-q2");
    const page = await open();
    expect(one(page.root, ".request .reason").textContent).toBe(second.summary.reason);
    expect(one(page.root, ".request h2").textContent).toContain(second.summary.title);
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    expect(requests.some((request) => request.path === "api/reviews/tree-q1")).toBe(false);
  });

  test("a direct history link selects history and keeps the exact operation visible", async () => {
    detail.summary.state = "rejected";
    detail.summary.answerable = false;
    detail.question.answer = { approved: false, principal: "operator", note: "Choose another design.", comments: [], at: "2026-09-24T12:05:00Z" };
    detail.thread = [{ kind: "answer", author: "operator", text: "Choose another design.", comments: [], at: "2026-09-24T12:05:00Z", approved: false }];
    rows = [detail.summary];
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Choose another design.") ?? false, "the directly linked history request");
    expect(labelled(shown.root, "button", "History (1)").getAttribute("aria-pressed")).toBe("true");
    await click(labelled(shown.root, "button", "Details"));
    expect(one(shown.root, ".metadata").textContent).toContain(detail.summary.operation);
    expect(window.location.hash).toBe("#review=q1");
  });

  test("a launch authenticated in another tab restores access without losing the request or comment", async () => {
    const page = await open();
    await comment("Keep this draft across authentication.");
    const address = window.location.href;
    refreshStatus = 401;
    await click(labelled(page.root, "button", "Reconnect"));
    await until(() => page.root.textContent?.includes("not authorized") ?? false, "expired access");
    refreshStatus = 200;
    const before = requests.length;
    await act(async () => {
      localStorage.setItem("lup-dashboard-token", "renewed-access");
      window.dispatchEvent(new StorageEvent("storage", { key: "lup-dashboard-token", newValue: "renewed-access", storageArea: localStorage }));
    });
    await until(() => page.root.querySelector("#review-comment") !== null && requests.some((request) => request.authorization === "Bearer renewed-access"), "access from the other tab");
    expect(box(page.root).value).toBe("Keep this draft across authentication.");
    expect(window.location.href).toBe(address);
    expect(requests.slice(before).every((request) => request.authorization === "Bearer renewed-access")).toBe(true);
    expect(streamingAborted).toBe(true);
  });

  test("opening a fresh token-only launch link in the same tab preserves the active review and draft", async () => {
    addRequest();
    const page = await open();
    await click(labelled(page.root, "button", "Next →"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the second request");
    await comment("Keep this second request's draft.");
    const address = window.location.href;
    refreshStatus = 401;
    await click(labelled(page.root, "button", "Reconnect"));
    await until(() => page.root.textContent?.includes("not authorized") ?? false, "expired access");
    refreshStatus = 200;
    const before = requests.length;
    await act(async () => { window.location.hash = "token=restarted-server"; });
    await until(() => page.root.querySelector("#review-comment") !== null && requests.some((request) => request.authorization === "Bearer restarted-server"), "same-tab reauthentication");
    expect(window.location.href).toBe(address);
    expect(window.location.hash).not.toContain("token");
    expect(localStorage.getItem("lup-dashboard-token")).toBe("restarted-server");
    expect(box(page.root).value).toBe("Keep this second request's draft.");
    expect(one(page.root, ".request .reason").textContent).toBe("Request tree-q2");
    expect(requests.slice(before).every((request) => request.authorization === "Bearer restarted-server")).toBe(true);
  });

  test("checking access rereads origin storage when no storage event was delivered", async () => {
    window.history.replaceState(null, "", "/#review=q1");
    refreshStatus = 401;
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("not authorized") ?? false, "missing access");
    localStorage.setItem("lup-dashboard-token", "restored-launch");
    refreshStatus = 200;
    await click(labelled(shown.root, "button", "Check access again"));
    await until(() => shown?.root.querySelector("#review-comment") !== null, "rechecked access");
    expect(requests.some((request) => request.authorization === "Bearer restored-launch")).toBe(true);
    expect(one(shown.root, ".request .reason").textContent).toBe(summary.reason);
  });

  test("blocked storage leaves the launch usable in this tab with a visible sharing notice", async () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new DOMException("Storage blocked", "SecurityError"); } });
    const page = await open();
    expect(page.root.textContent).toContain("Browser storage is unavailable");
    expect(window.location.hash).not.toContain("token");
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    await comment("Keep in-memory access.");
    await click(labelled(page.root, "button", "Reconnect"));
    expect(box(page.root).value).toBe("Keep in-memory access.");
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
  });

  test("an unknown request stays selected until that exact request arrives", async () => {
    window.history.replaceState(null, "", "/#token=browser-secret&review=tree-later");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Request not found") ?? false, "the missing-link explanation");
    expect(shown.root.querySelector(".request")).toBeNull();
    expect(requests.some((request) => request.path === "api/reviews/tree-q1")).toBe(false);
    const later = addRequest("tree-later");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.querySelector(".request .reason")?.textContent === later.summary.reason, "the linked request arrival");
  });

  test("hash navigation and browser back and forward restore the intended request", async () => {
    addRequest();
    const page = await open();
    const firstUrl = window.location.href;
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "manual link navigation");
    expect(new URL(window.location.href).hash).toContain("review=tree-q2");
    const secondUrl = window.location.href;
    await act(async () => { window.history.replaceState(null, "", firstUrl); window.dispatchEvent(new PopStateEvent("popstate")); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === summary.reason, "back to the first request");
    await act(async () => { window.history.replaceState(null, "", secondUrl); window.dispatchEvent(new PopStateEvent("popstate")); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "forward to the second request");
    await act(async () => { window.location.hash = "review=q1"; });
    await until(() => page.root.querySelector(".request .reason")?.textContent === summary.reason, "a changed hash");
  });

  test("copied links contain review identity and no credential", async () => {
    const page = await open();
    await click(labelled(page.root, "button", "Copy link"));
    await until(() => page.root.textContent?.includes("Link copied.") ?? false, "the copy receipt");
    const copied = new URL(await navigator.clipboard.readText());
    expect(copied.hash).toBe("#review=q1&root=tree");
    expect(copied.href).not.toContain("browser-secret");
    expect(copied.href).not.toContain("token");
  });

  test("duplicate question IDs require an explicit checkout choice", async () => {
    const second = addRequest();
    second.summary.id = "q1";
    second.summary.root_id = "other-tree";
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("multiple checkouts") ?? false, "the ambiguous-link explanation");
    expect(shown.root.querySelector(".request")).toBeNull();
    await click(one<HTMLElement>(shown.root, ".queue-row"));
    await until(() => shown?.root.querySelector(".request .reason")?.textContent === summary.reason, "the explicit checkout selection");
    expect(window.location.hash).toBe("#review=q1&root=tree");
  });

  test("a line and a range are commented on the diff and sent as structured anchors with the decision", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.path = "/project/src/app.py";
    file.before = "one\ntwo\nthree\n";
    file.after = "one\nTWO\nthree\nfour\n";
    file.hunks = [{ header: "@@ -1,3 +1,4 @@", old_start: 1, old_end: 3, new_start: 1, new_end: 4, lines: [
      { kind: "context", text: "one\n", old_line: 1, new_line: 1, suppression: false },
      { kind: "remove", text: "two\n", old_line: 2, new_line: null, suppression: false },
      { kind: "add", text: "TWO\n", old_line: null, new_line: 2, suppression: false },
      { kind: "context", text: "three\n", old_line: 3, new_line: 3, suppression: false },
      { kind: "add", text: "four\n", old_line: null, new_line: 4, suppression: false },
    ] }];
    const page = await open();
    await click(one<HTMLElement>(page.root, 'button[aria-label="Comment on line 2 before the change"]'));
    await type(one<HTMLTextAreaElement>(page.root, ".line-comment-editor"), "keep the lower case");
    await keydown("Escape", "Escape", {}, one(page.root, ".line-comment-editor"));
    expect(one(page.root, ".comment-row .line-comment p").textContent).toBe("keep the lower case");
    await click(one<HTMLElement>(page.root, 'button[aria-label="Comment on line 2 after the change"]'));
    await act(async () => one<HTMLElement>(page.root, 'button[aria-label="Comment on line 4 after the change"]').dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true })));
    await type(one<HTMLTextAreaElement>(page.root, ".line-comment-editor"), "these three belong together");
    expect(page.root.querySelectorAll(".diff-line.commenting")).toHaveLength(3);
    expect(one(page.root, ".draft-count").textContent).toBe("2 line comments drafted");
    await comment("Two notes on the diff.");
    await approve(one(page.root, ".line-comment-editor"));
    await until(() => posted().length === 1, "the approval with its comments");
    expect(posted()[0]?.body).toEqual({ approved: true, note: "Two notes on the diff.", fingerprint: "bound-tree-q1", comments: [
      { path: "/project/src/app.py", start: 2, end: 2, side: "before", note: "keep the lower case" },
      { path: "/project/src/app.py", start: 2, end: 4, side: "after", note: "these three belong together" },
    ] });
  });

  test("every lup marker kind is marked where it stands, and m and Shift+M jump to them", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    const lines = ["# lup: open note", ...Array.from({ length: 10 }, (_, index) => `line ${index + 2}`), "# lup: defer: parked", "# lup: solved: done", "# lup: template: choose", "x = 1  # lup: ignore[rule]"];
    file.path = "/project/marked.py";
    file.before = `${lines.join("\n")}\n`;
    file.after = `${lines.map((line) => line === "line 6" ? "line six" : line).join("\n")}\n`;
    file.hunks = [{ header: "@@ -3,7 +3,7 @@", old_start: 3, old_end: 9, new_start: 3, new_end: 9, lines: [
      ...[3, 4, 5].map((number) => ({ kind: "context" as const, text: `line ${number}\n`, old_line: number, new_line: number, suppression: false })),
      { kind: "remove", text: "line 6\n", old_line: 6, new_line: null, suppression: false },
      { kind: "add", text: "line six\n", old_line: null, new_line: 6, suppression: false },
      ...[7, 8, 9].map((number) => ({ kind: "context" as const, text: `line ${number}\n`, old_line: number, new_line: number, suppression: false })),
    ] }];
    file.markers = [
      { side: "after", line: 1, end_line: 1, kind: "note", condition: null, text: "open note" },
      { side: "after", line: 12, end_line: 12, kind: "defer", condition: null, text: "parked" },
      { side: "after", line: 13, end_line: 13, kind: "solved", condition: null, text: "done" },
      { side: "after", line: 14, end_line: 14, kind: "template", condition: null, text: "choose" },
      { side: "after", line: 15, end_line: 15, kind: "ignore", condition: null, text: "# lup: ignore[rule]" },
    ];
    const page = await open();
    expect(page.root.querySelectorAll(".diff-gap")).toHaveLength(2);
    expect(one(page.root, ".diff-gap button").textContent).toBe("Show 2 unchanged lines");
    await keydown("Escape", "Escape", {}, box(page.root));
    await keydown("m", "KeyM");
    await keyup("m", "KeyM");
    expect(labelled(page.root, "button", "Whole file").getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement?.classList.contains("marker-note")).toBe(true);
    expect(document.activeElement?.querySelector(".marker-badge")?.textContent).toBe("open note");
    await keydown("m", "KeyM");
    await keyup("m", "KeyM");
    expect(document.activeElement?.classList.contains("marker-defer")).toBe(true);
    expect(document.activeElement?.querySelector(".marker-badge")?.textContent).toBe("deferred");
    await keydown("M", "KeyM", { shiftKey: true });
    await keyup("M", "KeyM");
    expect(document.activeElement?.classList.contains("marker-note")).toBe(true);
    for (const kind of ["note", "defer", "solved", "template", "ignore"]) expect(page.root.querySelectorAll(`.diff-line.marker-${kind}`)).toHaveLength(1);
    expect(one(page.root, ".marker-paging span").textContent).toBe("Markers 1/5");
  });

  test("the diff folds what it leaves out, one gap opens on a click, and f shows the whole file", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.before = Array.from({ length: 20 }, (_, index) => `line ${index + 1}\n`).join("");
    file.after = file.before.replace("line 10\n", "line ten\n");
    file.hunks = [{ header: "@@ -7,7 +7,7 @@", old_start: 7, old_end: 13, new_start: 7, new_end: 13, lines: [
      ...[7, 8, 9].map((number) => ({ kind: "context" as const, text: `line ${number}\n`, old_line: number, new_line: number, suppression: false })),
      { kind: "remove", text: "line 10\n", old_line: 10, new_line: null, suppression: false },
      { kind: "add", text: "line ten\n", old_line: null, new_line: 10, suppression: false },
      ...[11, 12, 13].map((number) => ({ kind: "context" as const, text: `line ${number}\n`, old_line: number, new_line: number, suppression: false })),
    ] }];
    const page = await open();
    expect([...page.root.querySelectorAll(".diff-gap button")].map((button) => button.textContent)).toEqual(["Show 6 unchanged lines", "Show 7 unchanged lines"]);
    await click(one<HTMLElement>(page.root, ".diff-gap button"));
    expect(page.root.querySelectorAll(".diff-gap")).toHaveLength(1);
    expect(page.root.querySelectorAll(".diff-line")).toHaveLength(14);
    await keydown("Escape", "Escape", {}, box(page.root));
    await keydown("f", "KeyF");
    await keyup("f", "KeyF");
    expect(page.root.querySelectorAll(".diff-gap")).toHaveLength(0);
    expect(page.root.querySelectorAll(".diff-hunk")).toHaveLength(0);
    expect(page.root.querySelectorAll(".diff-line")).toHaveLength(21);
    expect([...page.root.querySelectorAll(".diff-line")].at(-1)?.querySelectorAll(".line-number")[1]?.textContent).toBe("20");
  });

  test("code is coloured by its file's extension, and a name no grammar claims stays plain", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.before = "def before():\n    return 1\n";
    file.after = "def after():\n    return 2\n";
    file.hunks = [{ header: "@@ -1,2 +1,2 @@", old_start: 1, old_end: 2, new_start: 1, new_end: 2, lines: [
      { kind: "remove", text: "def before():\n", old_line: 1, new_line: null, suppression: false },
      { kind: "remove", text: "    return 1\n", old_line: 2, new_line: null, suppression: false },
      { kind: "add", text: "def after():\n", old_line: null, new_line: 1, suppression: false },
      { kind: "add", text: "    return 2\n", old_line: null, new_line: 2, suppression: false },
    ] }];
    const page = await open();
    expect(one(page.root, ".diff-line.add .hljs-keyword").textContent).toBe("def");
    expect(one(page.root, ".diff-line.add .hljs-title").textContent).toBe("after");
    expect(one(page.root, ".diff-line.remove .hljs-number").textContent).toBe("1");
    page.unmount();
    shown = null;
    file.path = "/project/NOTES";
    const plain = await open();
    expect(plain.root.querySelector(".diff .hljs-keyword")).toBeNull();
    expect(one(plain.root, ".diff-line.add .line-content").textContent).toBe("def after():");
  });

  test("the thread shows what the operator remarked and what the requester replied", async () => {
    detail.thread = [
      { kind: "remark", author: "operator", text: "Why not a flag?", comments: [{ path: "/project/file.py", start: 1, end: 1, side: "after", note: "here" }], at: "2026-09-24T12:01:00Z", approved: null },
      { kind: "reply", author: "lead", text: "The flag is gone upstream.", comments: [], at: "2026-09-24T12:02:00Z", approved: null },
    ];
    detail.question.account = [{ source: "description", text: "Rewrite the file header" }];
    rows = [{ ...summary, said: 2 }];
    const page = await open();
    expect(one(page.root, ".queue-row .row-said").textContent).toBe("2 comments in its thread");
    expect([...page.root.querySelectorAll(".thread .said")].map((entry) => entry.className)).toEqual(["said remark", "said reply"]);
    expect(one(page.root, ".thread .said.reply").textContent).toContain("lead replied");
    expect(one(page.root, ".thread-comments").textContent).toContain("file.py:1");
    expect(one(page.root, ".account").textContent).toBe("agent's noteRewrite the file header");
    expect(one(page.root, ".comment-row.recorded p").textContent).toBe("here");
  });
});
