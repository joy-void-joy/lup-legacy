import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act } from "react";
import type { ReviewFile, ReviewSuppression, ReviewNotification } from "../generated/views";
import { App } from "./App";
import { click, labelled, mount, one, until, type Mounted } from "../testing";

const originalFetch = globalThis.fetch;
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const root = { id: "tree", path: "/project/tree/feature" };
const summary = {
  key: "tree-q1", root_id: root.id, id: "q1", state: "pending", requester: "codex-session",
  reason: "Review the complete replacement", operation: "apply_patch in /project", rule: "whole-file",
  title: "Update project/file.py", paths: ["project/file.py"], total_files: 1,
  created: "2026-09-24T12:00:00Z", answerable: true,
};

function review(key = "tree-q1") {
  return {
    summary: { ...summary, key, id: key === "tree-q1" ? "q1" : key },
    question: {
      fingerprint: `bound-${key}`, resumption: "native_retry", answer: null as null | { approved: boolean; principal: string; note: string },
      operation: { tool: "apply_patch", payload: { patch: "Complete requested patch" } },
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

describe("review inbox page", () => {
  let shown: Mounted | null = null;
  let detail = review();
  let details = new Map<string, ReturnType<typeof review>>();
  let rows = [{ ...summary }];
  let answerStatus = 200;
  let answerWait: Promise<void> | null = null;
  let detailWait = new Map<string, Promise<void>>();
  let refreshStatus = 200;
  let stream: ReadableStreamDefaultController<Uint8Array> | null = null;
  let streamingAborted = false;
  let requests: { path: string; method: string; body: unknown; authorization: string | null }[] = [];
  const inbox = () => ({ roots: [root], reviews: rows, errors: [] });

  beforeEach(() => {
    detail = review();
    details = new Map([[detail.summary.key, detail]]);
    rows = [{ ...summary }];
    answerStatus = 200;
    answerWait = null;
    detailWait = new Map();
    refreshStatus = 200;
    stream = null;
    streamingAborted = false;
    requests = [];
    sessionStorage.clear();
    localStorage.clear();
    window.history.replaceState(null, "", "/#token=browser-secret");
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      const path = String(input);
      requests.push({ path, method: options?.method ?? "GET", body: typeof options?.body === "string" ? JSON.parse(options.body) : null,
        authorization: new Headers(options?.headers).get("Authorization") });
      if (path === "api/events" && refreshStatus !== 200) return Response.json({ detail: "Refresh unavailable" }, { status: refreshStatus });
      if (path === "api/events") return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          stream = controller;
          controller.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`));
          options?.signal?.addEventListener("abort", () => {
            streamingAborted = true;
            controller.error(new DOMException("Stopped", "AbortError"));
          }, { once: true });
        },
      }));
      if (path === "api/reviews") return refreshStatus === 200 ? Response.json(inbox()) : Response.json({ detail: "Refresh unavailable" }, { status: refreshStatus });
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
    expect(requests.map((request) => request.path)).toEqual(["api/events", "api/reviews/tree-q1"]);
    expect(page.root.textContent).toContain("Live");
  });

  test("unchanged queue heartbeats still refresh selected file safety checks", async () => {
    const page = await open();
    await comment("Keep the draft during live safety checks.");
    detail.stale_reason = "The captured file changed on disk.";
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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

  test("a refused submission keeps the selected request and comment for rejection", async () => {
    addRequest();
    const page = await open();
    answerStatus = 409;
    await comment("Use a scoped patch.");
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.textContent?.includes("The file changed") ?? false, "the refusal");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Use a scoped patch.");
    expect(one(page.root, ".queue-row.selected").textContent).toContain(summary.title);
    answerStatus = 200;
    await click(labelled(page.root, "button", "Reject"));
    await until(() => page.root.textContent?.includes("Rejection recorded.") ?? false, "the rejected record");
    expect(requests.filter((request) => request.method === "POST")[1]?.body).toEqual({ approved: false, note: "Use a scoped patch.", fingerprint: "bound-tree-q1" });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request");
  });

  test("live snapshots add requests and stale evidence prevents approval", async () => {
    detail.stale_reason = "The proposed file has changed.";
    const page = await open();
    expect(labelled<HTMLButtonElement>(page.root, "button", "Approve").disabled).toBe(true);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Reject").disabled).toBe(false);
    await keydown("A", "KeyA", { shiftKey: true });
    await keyup("A", "KeyA");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
    addRequest();
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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
    await click(labelled(page.root, "button", "Reconnect"));
    await until(() => requests.filter((request) => request.path === "api/events").length === 2, "a fresh stream");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Preserve this draft.");
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
    expect(one(page.root, ".file-heading .file-prefix code").textContent).toBe(other.path);
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
    expect(one(page.root, ".file-heading .file-prefix code").textContent).toBe(other.path);
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
    await keydown("R", "KeyR", { shiftKey: true });
    await keyup("R", "KeyR");
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
    await keydown("R", "KeyR", { shiftKey: true }, editable);
    await keyup("R", "KeyR");
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
    await keydown("R", "KeyR", { shiftKey: true });
    await keyup("R", "KeyR");
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
    expect(one(page.root, ".request .reason").textContent).toBe(summary.reason);
    expect(labelled<HTMLButtonElement>(page.root, "button", "Reject").disabled).toBe(true);
    await act(async () => { finish?.(); });
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the unlocked next request");
    await act(async () => labelled(page.root, "button", "Approve").dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 2 })));
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
  });

  test("recorded decisions advance without waiting for another inbox read", async () => {
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
    const previous = inbox();
    const page = await open();
    await click(labelled(page.root, "button", "Approve"));
    await until(() => page.root.querySelector(".request .reason")?.textContent === "Request tree-q2", "the next request");
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(previous)}\n`)));
    expect(labelled(page.root, "button", "Pending (1)")).toBeTruthy();
    expect(page.root.querySelectorAll(".queue-row")).toHaveLength(1);
    const completed = rows.find((row) => row.key === "tree-q1");
    if (completed === undefined) throw new Error("fixture lacks the completed request");
    completed.title = "Server-confirmed history title";
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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
      await click(one<HTMLElement>(page.root, ".file-heading .file-prefix summary"));
      expect(one(page.root, ".file-review p").textContent).toBe(file.review_reason);
      expect(one(page.root, ".file-heading .file-prefix code").textContent).toBe(file.path);
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

  test("command-level requests show the exact command when every file passes automatically", async () => {
    const file = detail.files[0];
    if (file === undefined) throw new Error("fixture lacks a file");
    file.review_effect = "allow";
    file.review_reason = "The file edit passes; the command itself requires approval.";
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
    detail.preview_notice = "Preview computed in inbox environment from captured input. The execution environment was not captured.";
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

  test("a token-free link opens the exact request in another tab using origin storage", async () => {
    const second = addRequest();
    localStorage.setItem("lup-review-token", "browser-secret");
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
    await until(() => shown?.root.textContent?.includes("Rejected by operator") ?? false, "the directly linked history request");
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
      localStorage.setItem("lup-review-token", "renewed-access");
      window.dispatchEvent(new StorageEvent("storage", { key: "lup-review-token", newValue: "renewed-access", storageArea: localStorage }));
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
    expect(localStorage.getItem("lup-review-token")).toBe("restarted-server");
    expect(one<HTMLTextAreaElement>(page.root, "textarea").value).toBe("Keep this second request's draft.");
    expect(one(page.root, ".request .reason").textContent).toBe("Request tree-q2");
    expect(requests.slice(before).every((request) => request.authorization === "Bearer restarted-server")).toBe(true);
  });

  test("checking access rereads origin storage when no storage event was delivered", async () => {
    window.history.replaceState(null, "", "/#review=q1");
    refreshStatus = 401;
    shown = mount(<App />);
    await until(() => shown?.root.textContent?.includes("not authorized") ?? false, "missing access");
    localStorage.setItem("lup-review-token", "restored-launch");
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
    await act(async () => stream?.enqueue(new TextEncoder().encode(`${JSON.stringify(inbox())}\n`)));
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
