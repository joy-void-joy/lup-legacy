import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act } from "react";
import type { ReviewFile, ReviewSuppression, ReviewNotification, UnpreviewedStep } from "../generated/views";
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
const summary = {
  key: "tree-q1", root_id: root.id, id: "q1", state: "pending", requester: "codex-session",
  reason: "Review the complete replacement", operation: "apply_patch in /project", rule: "whole-file",
  title: "Update project/file.py", paths: ["project/file.py"], total_files: 1,
  created: "2026-09-24T12:00:00Z", answerable: true, session: "",
};

function review(key = "tree-q1") {
  return {
    summary: { ...summary, key, id: key === "tree-q1" ? "q1" : key },
    question: {
      fingerprint: `bound-${key}`, resumption: "native_retry", answer: null as null | { approved: boolean; principal: string; note: string },
      operation: { tool: "apply_patch", cwd: "/project", payload: { patch: "Complete requested patch" } as Record<string, string> },
      unpreviewed: null as UnpreviewedStep[] | null,
    },
    files: [{ path: "/project/file.py", operation: "modify", before: "before\n", after: "after\n",
      review_effect: "ask" as ReviewFile["review_effect"], review_reason: "This file requires approval.",
      unified: "--- before\n+++ after\n@@ -1 +1 @@\n-before\n+after\n", unchanged: false, additions: 1, deletions: 1, suppressions: [] as ReviewSuppression[],
      hunks: [{ header: "@@ -1 +1 @@", lines: [
        { kind: "remove", text: "before\n", old_line: 1 as number | null, new_line: null as number | null, suppression: false },
        { kind: "add", text: "after\n", old_line: null as number | null, new_line: 1 as number | null, suppression: false },
      ] }],
    }],
    stale_reason: "",
    command: null as string | null,
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
  const queue = () => ({ roots, reviews: rows, errors: issues });

  beforeEach(() => {
    detail = review();
    details = new Map([[detail.summary.key, detail]]);
    rows = [{ ...summary }];
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
      if (path === "api/setup") return Response.json(panes);
      for (const [key, captured] of details) {
        if (path === `api/reviews/${key}`) {
          await detailWait.get(key);
          return Response.json(captured);
        }
        if (path !== `api/reviews/${key}/answer`) continue;
        if (answerWait !== null) await answerWait;
        if (answerStatus !== 200) return Response.json({ detail: "The file changed; refresh the request." }, { status: answerStatus });
        const body = JSON.parse(String(options?.body)) as { approved: boolean; note: string };
        const settled = { ...captured, summary: { ...captured.summary, state: body.approved ? "approved" : "rejected", answerable: false },
          question: { ...captured.question, answer: { ...body, principal: "operator" } } };
        details.set(key, settled);
        rows = rows.map((row) => row.key === key ? settled.summary : row);
        return Response.json({ review: settled, notification: { queued: true, woken: false, detail: "Decision queued for the requesting session." } });
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
    expect([...page.root.querySelectorAll(".request-location dt")].map((node) => node.textContent)).toEqual(["Queue checkout", "Operation directory"]);
    expect([...page.root.querySelectorAll(".request-location code")].map((node) => node.textContent)).toEqual([checkout, detail.question.operation.cwd]);
    expect(one(page.root, ".file-target").textContent).toBe(`Target file${file.path}`);
    expect(one(page.root, ".queue-row .root-path").textContent).toBe(`Queue: ${checkout}`);
    expect(labelled(page.root, "button", "Approve").closest(".request-inspection")).toBeNull();
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
    expect(one(page.root, ".request-location code").textContent).toBe(root.path);
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".request-location code")?.textContent === secondRoot.path, "the next request's queue checkout");
    expect([...page.root.querySelectorAll(".request-location code")].map((node) => node.textContent)).toEqual([secondRoot.path, next.question.operation.cwd]);
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
    await until(() => page.root.querySelector(".decision") !== null, "the review again");
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
      expect(one(page.root, ".file-target code").textContent).toBe(file.path);
      for (const node of page.root.querySelectorAll<HTMLElement>(".watched-checkout code, .request-location code, .file-target code")) {
        expect(getComputedStyle(node).whiteSpace).toBe("pre");
        expect(getComputedStyle(node).overflow).toBe("auto");
        expect(node.tabIndex).toBe(0);
      }
    } finally {
      style.remove();
    }
  });

  test("an answered request retains notification failure diagnostics when reopened", async () => {
    detail.summary.state = "approved";
    detail.summary.answerable = false;
    detail.notification = { queued: false, woken: false, detail: "Decision recorded; no unique live requester is registered." };
    rows = [detail.summary];
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1&root=tree");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("no unique live requester") === true, "persisted notification diagnostics");
    expect(shown.root.textContent).toContain("Decision recorded");
    expect(one<HTMLDetailsElement>(shown.root, ".notification-status").open).toBe(false);
    expect(one<HTMLElement>(shown.root, ".notification-status summary").textContent).toContain("unconfirmed");
    expect(shown.root.querySelector(".decision")).toBeNull();
  });

  async function open() {
    shown = mount(<App />);
    await until(() => shown?.root.querySelector(".decision") !== null && shown?.root.querySelector(".decision") !== undefined, "the decision form");
    return shown;
  }

  async function comment(value: string) {
    if (shown === null) throw new Error("page is not open");
    const textarea = one<HTMLTextAreaElement>(shown.root, "textarea");
    const disclosure = textarea.closest("details");
    if (disclosure instanceof HTMLDetailsElement && !disclosure.open) await click(one<HTMLElement>(disclosure, "summary"));
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (setter === undefined) throw new Error("textarea has no value setter");
    await act(async () => {
      setter.call(textarea, value);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
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

  test("reconnecting an empty queue does not present its previous zero count as current", async () => {
    rows = [];
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the confirmed empty queue");
    streamImmediately = false;
    await click(labelled(shown.root, "button", "Reconnect"));
    await until(() => requests.filter((request) => request.path === "api/stream").length === 2, "the replacement stream");
    expect(shown.root.textContent).toContain("Pending (?)");
    expect(shown.root.textContent).toContain("Refreshing review queue…");
    expect(shown.root.textContent).not.toContain("Queue complete");
    expect(shown.root.textContent).not.toContain("No requests waiting");
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the refreshed empty queue");
  });

  test("failed checkout reads leave counts unknown while retaining known requests", async () => {
    issues = [{ root: "/project/tree/other", message: "Queue could not be read" }];
    const page = await open();
    expect(page.root.textContent).toContain("Pending (?)");
    expect(page.root.textContent).toContain("Some checkout queues are unavailable");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
    expect(page.root.textContent).toContain("Queue could not be read");
    expect(page.root.textContent).not.toContain("Queue complete");
    rows = [];
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    expect(page.root.textContent).not.toContain("No requests waiting");
    expect(page.root.textContent).not.toContain("Request not found");
    expect(page.root.textContent).toContain("Requested review unavailable");
  });

  test("a disconnected empty queue stays unknown during the automatic reconnect delay", async () => {
    rows = [];
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Queue complete") ?? false, "the empty snapshot");
    await act(async () => stream?.error(new Error("Connection lost")));
    await until(() => shown?.root.textContent?.includes("Reconnecting") ?? false, "the retry status");
    expect(shown.root.textContent).toContain("Pending (?)");
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
    detail.stale_reason = "The file changed while the queue was open.";
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => shown?.root.textContent?.includes(detail.stale_reason) ?? false, "a completed request's next safety refresh");
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
    await until(() => shown?.root.querySelector(".request h2")?.textContent === fresh.summary.title, "a fresh detail fetch after reconnect", 1000);
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(2);
    await act(async () => finish());
    expect(shown.root.querySelector(".request h2")?.textContent).toBe(fresh.summary.title);
  });

  test("unchanged queue heartbeats still refresh selected file safety checks", async () => {
    const page = await open();
    await comment("Keep the draft during live safety checks.");
    detail.stale_reason = "The captured file changed on disk.";
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.textContent?.includes(detail.stale_reason) ?? false, "fresh stale-preimage status");
    expect(requests.filter((request) => request.path === "api/reviews/tree-q1")).toHaveLength(2);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(true);
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Keep the draft during live safety checks.");
  });

  test("large complete records mount only when requested and preserve the whole payload", async () => {
    detail.question.operation.payload.patch = "Complete evidence ".repeat(50000);
    const page = await open();
    expect(page.root.querySelector(".record pre")).toBeNull();
    expect(page.root.querySelector(".tool-input pre")).toBeNull();
    await click(one<HTMLElement>(page.root, ".request-evidence > summary"));
    await until(() => page.root.querySelector(".tool-input pre") !== null, "the requested payload");
    expect(JSON.parse(one(page.root, ".tool-input pre").textContent ?? "")).toEqual(detail.question.operation.payload);
    await comment("Typing should retain the already rendered evidence.");
    expect(JSON.parse(one(page.root, ".tool-input pre").textContent ?? "")).toEqual(detail.question.operation.payload);
    await click(one<HTMLElement>(page.root, ".request-evidence > summary"));
    await until(() => page.root.querySelector(".tool-input pre") === null, "the closed payload disclosure");
    await click(one<HTMLElement>(page.root, ".request-heading > .request-context > summary"));
    await click(one<HTMLElement>(page.root, ".record summary"));
    await until(() => page.root.querySelector(".record pre") !== null, "the requested full record");
    expect(JSON.parse(one(page.root, ".record pre").textContent ?? "")).toEqual(detail.question);
  });

  test("shows highlighted complete evidence and submits an exact approval with the comment", async () => {
    const page = await open();
    expect(one(page.root, ".diff-line.remove .line-content").textContent).toBe("before\n");
    expect(one(page.root, ".diff-line.add .line-content").textContent).toBe("after\n");
    expect([...page.root.querySelectorAll(".diff-line .line-number")].map((cell) => cell.textContent)).toEqual(["1", "", "", "1"]);
    expect(one(page.root, ".file-overview").textContent).toContain("+1 −1");
    await click(one<HTMLElement>(page.root, ".request-heading > .request-context > summary"));
    await click(one<HTMLElement>(page.root, ".record summary"));
    await until(() => page.root.querySelector(".record pre") !== null, "the requested complete record");
    expect(one(page.root, ".record pre").textContent).toContain("Complete requested patch");
    await click(labelled(page.root, "button", "Raw diff"));
    expect(one(page.root, ".file-evidence pre").textContent).toBe(detail.files[0]?.unified ?? "");
    await comment("Keep the public signature.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.textContent?.includes("Approval recorded.") ?? false, "the approval receipt");
    expect(requests.find((request) => request.method === "POST")?.body).toEqual({ approved: true, note: "Keep the public signature.", fingerprint: "bound-tree-q1" });
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    expect(window.location.hash).not.toContain("token");
    expect(page.root.textContent).toContain("Decision queued for the requesting session.");
    expect(page.root.textContent).toContain("Queue complete");
    expect(page.root.querySelector(".decision")).toBeNull();
    await click(labelled(page.root, "button", "History (1)"));
    await until(() => page.root.textContent?.includes("Approved by operator") ?? false, "the history detail");
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
  });

  test("a refused submission keeps the selected request and comment for declining", async () => {
    addRequest();
    const page = await open();
    answerStatus = 409;
    await comment("Use a scoped patch.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.textContent?.includes("The file changed") ?? false, "the refusal");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Use a scoped patch.");
    expect(one(page.root, ".queue-row.selected").textContent).toContain(summary.title);
    answerStatus = 200;
    await click(labelled(page.root, "button", "Decline"));
    await until(() => page.root.textContent?.includes("Decline recorded.") ?? false, "the declined record");
    expect(requests.filter((request) => request.method === "POST")[1]?.body).toEqual({ approved: false, note: "Use a scoped patch.", fingerprint: "bound-tree-q1" });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request");
  });

  test("live snapshots add requests and stale evidence prevents approval", async () => {
    detail.stale_reason = "The proposed file has changed.";
    const page = await open();
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(true);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Decline").disabled).toBe(false);
    await keydown("A", "KeyA", { shiftKey: true });
    await keyup("A", "KeyA");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
    addRequest();
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.querySelectorAll(".queue-row").length === 2, "the new queued request");
    page.unmount();
    shown = null;
    expect(streamingAborted).toBe(true);
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
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("This comment belongs to the first request.");
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
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Preserve this draft.");
    expect(page.root.textContent).toContain("Pending (?)");
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("other.py");
    expect(page.root.querySelectorAll(".file")).toHaveLength(1);
    await click(labelled(page.root, "button", "After"));
    expect(one(page.root, ".source-text").textContent).toBe(other.after);
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-heading > code").textContent).toBe("other.py");
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    await keydown("]", "BracketRight", {}, search);
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    await click(one<HTMLElement>(page.root, ".file-list button"));
    expect(one(page.root, ".file-heading > code").textContent).toBe("nested/percent%#name.py");
    expect(one(page.root, ".file-target code").textContent).toBe(other.path);
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("second%20.py");
    expect(one(page.root, ".file-target code").textContent).toBe(other.path);
  });

  test("expanded inspection disclosures stay inside their scroll region and outside the decision footer", async () => {
    detail.command = "sed -i 's/before/after/' file.py";
    detail.preview_notice = "The execution environment was not captured.\n".repeat(100);
    detail.summary.reason = "Review the captured changes.\n".repeat(100);
    detail.question.operation.payload.patch = "Complete requested patch\n".repeat(100);
    const page = await open();
    for (const selector of [".request-context", ".record", ".request-evidence", ".preview-note"]) {
      await click(one<HTMLElement>(one(page.root, selector), "summary"));
      expect(one<HTMLDetailsElement>(page.root, selector).open).toBe(true);
      expect(one(page.root, selector).closest(".request-inspection")).toBe(one(page.root, ".request-inspection"));
    }
    await comment("Keep this expanded draft available.");
    const inspection = one(page.root, ".request-inspection");
    const decision = one(page.root, ".decision");
    expect(inspection.nextElementSibling).toBe(decision);
    expect(decision.parentElement).toBe(one(page.root, ".request"));
    expect(one(page.root, ".files").parentElement).toBe(inspection);
    expect(one(page.root, ".comment-editor").closest(".request-inspection")).toBeNull();
    expect(labelled(page.root, "button", "Approve").closest(".request-inspection")).toBeNull();
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(false);
    expect(one(page.root, ".preview-note p").textContent).toBe(detail.preview_notice);
    expect(one<HTMLTextAreaElement>(decision, "textarea").value).toBe("Keep this expanded draft available.");
  });

  test("decision controls stay outside the evidence scroll region and comment opens on demand", async () => {
    const page = await open();
    expect(one<HTMLDetailsElement>(page.root, ".request-context").open).toBe(false);
    expect(one<HTMLDetailsElement>(page.root, ".comment-editor").open).toBe(false);
    expect(labelled(page.root, "button", "Approve").closest(".file-evidence")).toBeNull();
    expect(one(page.root, ".decision").parentElement).toBe(one(page.root, ".request"));
    await keydown("c", "KeyC");
    await keyup("c", "KeyC");
    expect(one<HTMLDetailsElement>(page.root, ".comment-editor").open).toBe(true);
    expect(document.activeElement).toBe(one(page.root, "textarea"));
    await click(labelled(page.root, "button", "Queue (1)"));
    expect(one(page.root, ".workspace").getAttribute("data-mobile-panel")).toBe("queue");
    await click(one<HTMLElement>(page.root, ".queue-row"));
    expect(one(page.root, ".workspace").getAttribute("data-mobile-panel")).toBe("review");
    await click(labelled(page.root, "button", "Browse files / exceptions"));
    expect(one(page.root, ".files").getAttribute("data-mobile-panel")).toBe("navigator");
  });

  test("successful decisions advance through the queue without a held key answering the next target", async () => {
    addRequest();
    const page = await open();
    await keydown("A", "KeyA", { shiftKey: true });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "automatic queue advance");
    expect(document.activeElement).toBe(one(page.root, ".request h2"));
    await keydown("A", "KeyA", { shiftKey: true, repeat: true });
    await keydown("A", "KeyA", { shiftKey: true });
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
    await keyup("A", "KeyA");
    await keydown("D", "KeyD", { shiftKey: true });
    await keyup("D", "KeyD");
    await until(() => page.root.textContent?.includes("Queue complete") ?? false, "queue completion");
    expect(requests.filter((request) => request.method === "POST").map((request) => ({ path: request.path, body: request.body }))).toEqual([
      { path: "api/reviews/tree-q1/answer", body: { approved: true, note: "", fingerprint: "bound-tree-q1" } },
      { path: "api/reviews/tree-q2/answer", body: { approved: false, note: "", fingerprint: "bound-tree-q2" } },
    ]);
  });

  test("shortcuts ignore typing, modifiers and composition while navigation preserves drafts", async () => {
    addRequest();
    const page = await open();
    await comment("First draft");
    const textarea = one<HTMLTextAreaElement>(page.root, "textarea");
    for (const extra of [{ shiftKey: true }, { shiftKey: true, ctrlKey: true }, { shiftKey: true, isComposing: true }]) {
      await keydown("A", "KeyA", extra, textarea);
      await keyup("A", "KeyA");
    }
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    page.root.append(editable);
    await keydown("D", "KeyD", { shiftKey: true }, editable);
    await keyup("D", "KeyD");
    await keydown("A", "KeyA", { shiftKey: true, metaKey: true });
    await keyup("A", "KeyA");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "keyboard navigation");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("");
    await keydown("k", "KeyK");
    await keyup("k", "KeyK");
    await until(() => page.root.querySelector(".request .reason")?.textContent === summary.reason, "previous request");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("First draft");
    await keydown("c", "KeyC");
    await keyup("c", "KeyC");
    expect(document.activeElement).toBe(one(page.root, "textarea"));
    await click(labelled(page.root, "button", "Keyboard shortcuts"));
    expect(one(page.root, "#shortcut-help").textContent).toContain("Release the keys");
  });

  test("an in-flight decision locks queue changes and prevents duplicate requests", async () => {
    addRequest();
    const page = await open();
    let finish: (() => void) | undefined;
    answerWait = new Promise<void>((resolve) => { finish = resolve; });
    await click(labelled(page.root, "button", "Approve"));
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await keydown("D", "KeyD", { shiftKey: true });
    await keyup("D", "KeyD");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Decline").disabled).toBe(true);
    await act(async () => { finish?.(); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the unlocked next request");
    await act(async () => labelled(page.root, "button", "Approve").dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 2 })));
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
  });

  test("recorded decisions advance without waiting for another queue read", async () => {
    addRequest();
    const page = await open();
    refreshStatus = 503;
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "next request from the live snapshot");
    expect(requests.some((request) => request.path === "api/reviews")).toBe(false);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(false);
    expect(page.root.textContent).toContain("Approval recorded.");
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

  test("answered request heartbeats replace pending notification status with the recorded transport outcome", async () => {
    const page = await open();
    await click(one<HTMLElement>(page.root, ".queue-setting input"));
    detail.notification = { queued: false, woken: false, detail: "Decision recorded; notification pending." };
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".notification-status summary")?.textContent?.includes("unconfirmed") ?? false, "the pending notification outcome");
    expect(page.root.querySelector(".decision")).toBeNull();
    const settled = details.get("tree-q1");
    if (settled === undefined) throw new Error("fixture lacks the settled request");
    settled.notification = { queued: true, woken: true, detail: "The native runtime accepted the notification." };
    await act(async () => stream?.enqueue(new TextEncoder().encode(framed(queue()))));
    await until(() => page.root.querySelector(".notification-status summary")?.textContent?.includes("accepted by runtime") ?? false, "the refreshed transport outcome");
    expect(one(page.root, ".notification-status p").textContent).toBe(settled.notification.detail);
    expect(page.root.textContent).not.toContain("agent read");
  });

  test("automatic queue advancement can be paused", async () => {
    addRequest();
    const page = await open();
    await click(one<HTMLElement>(page.root, ".queue-setting input"));
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.textContent?.includes("Approved by operator") ?? false, "the retained decision");
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(page.root.querySelector(".decision")).toBeNull();
    await click(one<HTMLElement>(page.root, ".queue-row"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "manual advance");
  });

  test("a slow next-request load cannot submit the previously displayed fingerprint", async () => {
    addRequest();
    const page = await open();
    let finish: (() => void) | undefined;
    detailWait.set("tree-q2", new Promise<void>((resolve) => { finish = resolve; }));
    await keydown("j", "KeyJ");
    await keyup("j", "KeyJ");
    await until(() => page.root.textContent?.includes("Loading request…") ?? false, "the pending detail read");
    await keydown("A", "KeyA", { shiftKey: true });
    await keyup("A", "KeyA");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
    await act(async () => { finish?.(); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the loaded next request");
    await keydown("A", "KeyA", { shiftKey: true });
    await keyup("A", "KeyA");
    expect(requests.find((request) => request.method === "POST")?.body).toEqual({ approved: true, note: "", fingerprint: "bound-tree-q2" });
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    expect(one(page.root, ".file-overview-heading .change-counts").textContent).toBe("+1 −1");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("1 / 1");
    await click(labelled(page.root, "button", "Full operation (2)"));
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(2);
    expect(one(page.root, ".file-list li:first-child .file-review-state").textContent).toBe("Automatic");
    await click(one<HTMLElement>(page.root, ".file-list li:first-child button"));
    expect(one(page.root, ".file-heading > code").textContent).toBe("tests/test_auto.py");
    await click(labelled(page.root, "button", "Needs review (1)"));
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    await click(labelled(page.root, "button", "Approve"));
    await until(() => requests.some((request) => request.method === "POST"), "the complete operation approval");
    expect(requests.find((request) => request.method === "POST")?.body).toEqual({ approved: true, note: "", fingerprint: "bound-tree-q1" });
  });

  test("queue and request headings use the backend's review subset with a secondary full-operation count", async () => {
    detail.summary.title = "Change 2 files requiring review";
    detail.summary.paths = ["src/first.py", "src/second.py"];
    detail.summary.total_files = 5;
    rows = [detail.summary];
    const page = await open();
    expect(one(page.root, ".queue-row strong").textContent).toBe(detail.summary.title);
    expect(one(page.root, ".request h2").textContent).toBe(detail.summary.title);
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("asked.py");
    expect([...page.root.querySelectorAll(".file-list code")].map((node) => node.textContent)).toEqual(["asked.py", "unknown.py"]);
    expect(one(page.root, ".file-overview-heading .change-counts").textContent).toBe("+2 −2");
    await keydown("]", "BracketRight");
    await keyup("]", "BracketRight");
    expect(one(page.root, ".file-heading > code").textContent).toBe("unknown.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("2 / 2");
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-heading > code").textContent).toBe("asked.py");
    await click(labelled(page.root, "button", "Full operation (4)"));
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(4);
    const native = one<HTMLElement>(page.root, ".file-list li:nth-child(3) button");
    expect(one(native, ".file-review-state").textContent).toBe("Native decision");
    await click(native);
    await click(one<HTMLElement>(page.root, ".file-review summary"));
    expect(one(page.root, ".file-review p").textContent).toBe(`No Lup approval requested; the native provider decides. ${deferred.review_reason}`);
    await click(labelled(page.root, "button", "Needs review (2)"));
    expect(one(page.root, ".file-heading > code").textContent).toBe("asked.py");
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(2);
    await click(labelled(page.root, "button", "Approve"));
    await until(() => requests.some((request) => request.method === "POST"), "the complete operation approval");
    expect(requests.find((request) => request.method === "POST")?.body).toEqual({ approved: true, note: "", fingerprint: "bound-tree-q1" });
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
    expect(one(page.root, ".source-text").textContent).toBe(file.after);
    expect(page.root.querySelectorAll(".source-line.suppression")).toHaveLength(1);
    await click(labelled(page.root, "button", "Full operation (1)"));
    expect([...page.root.querySelectorAll(".suppression-group summary code")].map((node) => node.textContent)).toEqual(["asked-rule", "native-rule"]);
    expect(page.root.querySelectorAll(".diff-line.suppression")).toHaveLength(2);
    expect(one(page.root, ".suppression-group:last-child .file-review-state").textContent).toBe("Native decision");
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(document.activeElement).toBe(one(page.root, ".diff-line:last-child"));
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
    expect(one(page.root, ".source-text").textContent).toBe(file.after);
    expect(page.root.querySelectorAll(".source-line.suppression")).toHaveLength(1);
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("match-last.py");
    expect(one(page.root, ".file-paging > span").textContent).toBe("2 / 2");
    await keydown("[", "BracketLeft");
    await keyup("[", "BracketLeft");
    expect(one(page.root, ".file-heading > code").textContent).toBe("match-first.py");
  });

  test("unknown file attribution stays visible even when the path looks like a test", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.path = "/project/tests/test_uncaptured.py";
    file.review_effect = "unknown";
    file.review_reason = "This historical request did not capture per-file policy decisions.";
    const page = await open();
    expect(page.root.querySelectorAll(".file-list li")).toHaveLength(1);
    expect(one(page.root, ".file-review summary").textContent).toBe("Unclassified · Why");
    await click(one<HTMLElement>(page.root, ".file-review summary"));
    expect(one(page.root, ".file-review p").textContent).toBe(file.review_reason);
  });

  test("expanded file reasons and paths remain complete inside a bounded scrolling header", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.review_reason = "A captured directive site requires review.\n".repeat(150);
    file.path = `/project/${"nested/".repeat(50)}source.py`;
    const style = document.createElement("style");
    style.textContent = await Bun.file(new URL("./styles.css", import.meta.url)).text();
    document.head.append(style);
    try {
      const page = await open();
      await click(one<HTMLElement>(page.root, ".file-review summary"));
      expect(one(page.root, ".file-review p").textContent).toBe(file.review_reason);
      expect(one(page.root, ".file-target code").textContent).toBe(file.path);
      const layout = getComputedStyle(one(page.root, ".file-heading"));
      expect(layout.overflow).toBe("auto");
      expect(layout.maxHeight).not.toBe("none");
      expect(layout.maxHeight).not.toBe("");
      expect(one(page.root, ".file-evidence").parentElement).toBe(one(page.root, ".file"));
      expect(labelled(page.root, "button", "Approve").closest(".request-inspection")).toBeNull();
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
    expect(one(page.root, ".diff-line.suppression code").textContent).toBe(line.text);
    expect(overview.querySelectorAll("details[open]")).toHaveLength(0);
    await click(one<HTMLElement>(overview, "details:first-of-type summary"));
    await click(one<HTMLElement>(overview, "details:first-of-type li button"));
    expect(document.activeElement).toBe(one(page.root, ".diff-line.suppression"));
    await keydown("n", "KeyN");
    await keyup("n", "KeyN");
    expect(one(page.root, ".source-text").textContent).toBe(file.after);
    expect(document.activeElement).toBe(one(page.root, ".source-line[data-line='6']"));
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("second.py");
    await keydown("p", "KeyP");
    await keyup("p", "KeyP");
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
  });

  test("shell file previews keep the exact command and unavailable-preview explanation visible", async () => {
    detail.command = "sed -i 's/before/after/' /project/file.py";
    detail.preview_unavailable = "One command target cannot be safely previewed.";
    detail.preview_notice = "Preview computed where the dashboard runs, from captured input. The execution environment was not captured.";
    const page = await open();
    await click(one<HTMLElement>(page.root, ".request-evidence > summary"));
    await until(() => page.root.querySelector(".tool-input pre") !== null, "the exact command and tool input");
    expect(one(page.root, ".command pre").textContent).toBe(detail.command);
    expect(page.root.textContent).toContain(detail.preview_unavailable);
    expect(one(page.root, ".tool-input pre").textContent).toContain("Complete requested patch");
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
    expect(one(page.root, ".file-heading > code").textContent).toBe("file.py");
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

  test("a token-free link opens the exact request in another tab using origin storage", async () => {
    const second = addRequest();
    localStorage.setItem("lup-dashboard-token", "browser-secret");
    window.history.replaceState(null, "", "/#review=tree-q2");
    const page = await open();
    expect(one(page.root, ".request .reason").textContent).toBe(second.summary.reason);
    expect(one(page.root, ".request h2").textContent).toBe(second.summary.title);
    expect(requests.every((request) => request.authorization === "Bearer browser-secret")).toBe(true);
    expect(requests.some((request) => request.path === "api/reviews/tree-q1")).toBe(false);
  });

  test("a direct history link selects history and keeps the exact operation visible", async () => {
    detail.summary.state = "rejected";
    detail.summary.answerable = false;
    detail.question.answer = { approved: false, principal: "operator", note: "Choose another design." };
    rows = [detail.summary];
    window.history.replaceState(null, "", "/#token=browser-secret&review=q1");
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("Declined by operator") ?? false, "the directly linked history request");
    expect(labelled(shown.root, "button", "History (1)").getAttribute("aria-pressed")).toBe("true");
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
    await until(() => page.root.querySelector("textarea") !== null && requests.some((request) => request.authorization === "Bearer renewed-access"), "access from the other tab");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Keep this draft across authentication.");
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
    await until(() => page.root.querySelector("textarea") !== null && requests.some((request) => request.authorization === "Bearer restarted-server"), "same-tab reauthentication");
    expect(window.location.href).toBe(address);
    expect(window.location.hash).not.toContain("token");
    expect(localStorage.getItem("lup-dashboard-token")).toBe("restarted-server");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Keep this second request's draft.");
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
    await until(() => shown?.root.querySelector("textarea") !== null, "rechecked access");
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
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Keep in-memory access.");
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
});
