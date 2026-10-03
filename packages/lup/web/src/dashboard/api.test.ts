import { afterEach, describe, expect, test } from "bun:test";
import type { StreamFrame } from "../generated/views";
import { answerReview, remarkReview, followDashboard, readReviews, readReviewLink, reviewLink, sendReply, takeToken, type Followed } from "./api";

const originalFetch = globalThis.fetch;
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const snapshot = { roots: [], reviews: [], errors: [], history: 0 };

afterEach(() => {
  if (originalStorage !== undefined) Object.defineProperty(globalThis, "localStorage", originalStorage);
  globalThis.fetch = originalFetch;
  sessionStorage.clear();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("review capability", () => {
  test("takes the capability from the fragment and shares it only within this origin", () => {
    window.history.replaceState(null, "", "/#token=operator-secret");
    expect(takeToken()).toEqual({ token: "operator-secret", notice: "" });
    expect(window.location.hash).toBe("");
    expect(takeToken()).toEqual({ token: "operator-secret", notice: "" });
    expect(localStorage.getItem("lup-dashboard-token")).toBe("operator-secret");
    expect(sessionStorage.getItem("lup-dashboard-token")).toBeNull();
  });

  test("authenticates reads and answers without putting the capability in the URL or body", async () => {
    const calls: { url: string; options?: RequestInit }[] = [];
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      calls.push({ url: String(input), options });
      return Response.json(snapshot);
    }, { preconnect() {} });
    await readReviews("operator-secret");
    const comments = [{ path: "/project/app.py", start: 3, end: 4, side: "after" as const, note: "Name it." }];
    await answerReview("tree/request", { approved: false, note: "Use a scoped change", comments, fingerprint: "exact" }, "operator-secret");
    await remarkReview("tree/request", { note: "Why?", comments, fingerprint: "exact" }, "operator-secret");
    expect(calls.map((call) => call.url)).toEqual(["api/reviews", "api/reviews/tree%2Frequest/answer", "api/reviews/tree%2Frequest/remark"]);
    for (const call of calls) expect(new Headers(call.options?.headers).get("Authorization")).toBe("Bearer operator-secret");
    expect(JSON.parse(String(calls[1]?.options?.body))).toEqual({ approved: false, note: "Use a scoped change", comments, fingerprint: "exact" });
    expect(JSON.parse(String(calls[2]?.options?.body))).toEqual({ note: "Why?", comments, fingerprint: "exact" });
  });

  test("scrubbing a launch token preserves the selected request", () => {
    window.history.replaceState(null, "", "/#token=operator-secret&review=question%2Fid&root=checkout");
    expect(takeToken().token).toBe("operator-secret");
    expect(readReviewLink()).toEqual({ id: "question/id", root: "checkout" });
    expect(window.location.hash).not.toContain("token");
  });

  test("shareable links drop authority and use the human-facing question ID", () => {
    window.history.replaceState(null, "", "/?token=never-share#token=also-secret");
    const shared = new URL(reviewLink("question/id"));
    expect(shared.search).toBe("");
    expect(shared.hash).toBe("#review=question%2Fid");
    expect(shared.href).not.toContain("secret");
    expect(shared.href).not.toContain("token");
  });

  test("missing access omits Authorization rather than sending an empty bearer", async () => {
    let headers = new Headers();
    globalThis.fetch = Object.assign(async (_input: string | URL | Request, options?: RequestInit) => {
      headers = new Headers(options?.headers);
      return Response.json(snapshot);
    }, { preconnect() {} });
    await readReviews("");
    expect(headers.has("Authorization")).toBe(false);
  });

  test("stale tab storage never overrides the origin's current launch token", () => {
    sessionStorage.setItem("lup-dashboard-token", "previous-launch");
    localStorage.setItem("lup-dashboard-token", "current-launch");
    expect(takeToken().token).toBe("current-launch");
    localStorage.clear();
    expect(takeToken().token).toBe("");
  });

  test("a denied storage write still scrubs the launch link and grants this tab access", () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
      setItem() { throw new DOMException("Storage full", "QuotaExceededError"); },
      getItem() { return null; },
    } });
    window.history.replaceState(null, "", "/#token=private-launch&review=q1");
    const access = takeToken();
    expect(access.token).toBe("private-launch");
    expect(access.notice).toContain("Browser storage is unavailable");
    expect(window.location.hash).toBe("#review=q1");
    expect(localStorage.getItem("lup-dashboard-token")).toBeNull();
  });

  test("a denied storage read reports the problem and preserves access already held in memory", () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new DOMException("Storage blocked", "SecurityError"); } });
    expect(takeToken().token).toBe("");
    expect(takeToken("current-tab")).toEqual({ token: "current-tab", notice: expect.stringContaining("Browser storage is unavailable") });
  });
});


/** One frame as the dashboard sends it: its cursor as the event id, the frame as data. */
function sse(frame: StreamFrame): string {
  return `id: ${frame.cursor}\ndata: ${JSON.stringify(frame)}\n\n`;
}

const whole: StreamFrame = {
  cursor: '{"epoch":"e1","seq":0}',
  event: { type: "snapshot", repositories: [], sessions: [], messages: [], extents: [], reviews: snapshot, code: { source: "", root: "", since: null, older: false, failing: "", restarted: "" }, keys: { source: "", unread: "", changed: [], report: { applied: [], refused: [], waits: [] } } },
};

const gone: StreamFrame = { cursor: '{"epoch":"e1","seq":1}', event: { type: "session_gone", key: "r/é-session" } };

function served(chunks: string[], cancel: () => void = () => {}, close = true): typeof fetch {
  return Object.assign(async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      if (close) controller.close();
    },
    cancel,
  })), { preconnect() {} });
}

describe("dashboard stream", () => {
  test("hands over each frame and says when the tab is current, ignoring retry and keep-alive", async () => {
    globalThis.fetch = served(["retry: 3000\n\n", sse(whole), ": live\n\n", ": keep-alive\n\n", sse(gone)]);
    const received: Followed[] = [];
    for await (const entry of followDashboard("secret", new AbortController().signal)) received.push(entry);
    expect(received).toEqual([{ kind: "frame", frame: whole }, { kind: "live" }, { kind: "frame", frame: gone }]);
  });

  test("decodes frames and UTF-8 across arbitrary byte boundaries", async () => {
    const encoded = new TextEncoder().encode(`${sse(whole)}${sse(gone)}`);
    globalThis.fetch = Object.assign(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of encoded) controller.enqueue(Uint8Array.of(byte));
        controller.close();
      },
    })), { preconnect() {} });
    const received: Followed[] = [];
    for await (const entry of followDashboard("secret", new AbortController().signal)) received.push(entry);
    expect(received).toEqual([{ kind: "frame", frame: whole }, { kind: "frame", frame: gone }]);
  });

  test("a reconnecting tab names the last frame it saw, and a fresh one names none", async () => {
    const sent: Headers[] = [];
    globalThis.fetch = Object.assign(async (_input: string | URL | Request, options?: RequestInit) => {
      sent.push(new Headers(options?.headers));
      return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.close(); } }));
    }, { preconnect() {} });
    for await (const _ of followDashboard("secret", new AbortController().signal, whole.cursor)) break;
    for await (const _ of followDashboard("secret", new AbortController().signal)) break;
    expect(sent[0]?.get("Last-Event-ID")).toBe(whole.cursor);
    expect(sent[0]?.get("Authorization")).toBe("Bearer secret");
    expect(sent[1]?.has("Last-Event-ID")).toBe(false);
  });

  test("a stalled stream expires and releases its network reader", async () => {
    let cancelled = false;
    globalThis.fetch = served([sse(whole)], () => { cancelled = true; }, false);
    const reader = followDashboard("secret", new AbortController().signal, "", 10);
    expect((await reader.next()).value).toEqual({ kind: "frame", frame: whole });
    await expect(reader.next()).rejects.toThrow("The dashboard stream stopped updating.");
    expect(cancelled).toBe(true);
  });

  test("keep-alive comments keep a quiet stream available", async () => {
    let stream: ReadableStreamDefaultController<Uint8Array> | null = null;
    globalThis.fetch = Object.assign(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        stream = controller;
        controller.enqueue(new TextEncoder().encode(sse(whole)));
      },
    })), { preconnect() {} });
    const reader = followDashboard("secret", new AbortController().signal, "", 60);
    await reader.next();
    const next = reader.next();
    for (const _ of [1, 2, 3]) {
      await new Promise<void>((resolve) => setTimeout(resolve, 30));
      const sending = stream as ReadableStreamDefaultController<Uint8Array> | null;
      sending?.enqueue(new TextEncoder().encode(": keep-alive\n\n"));
    }
    const sending = stream as ReadableStreamDefaultController<Uint8Array> | null;
    sending?.enqueue(new TextEncoder().encode(sse(gone)));
    expect((await next).value).toEqual({ kind: "frame", frame: gone });
    await reader.return(undefined);
  });

  test("surfaces refused credentials instead of decoding an error as a frame", async () => {
    globalThis.fetch = Object.assign(async () => Response.json({ detail: "Access denied" }, { status: 403 }), { preconnect() {} });
    await expect(followDashboard("bad", new AbortController().signal).next()).rejects.toThrow("Access denied");
  });
});

describe("reply", () => {
  test("posts the operator's words to the session it names, by repository and member id", async () => {
    const calls: { url: string; options?: RequestInit }[] = [];
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      calls.push({ url: String(input), options });
      return Response.json({ session: "repo/abc", queued: true, woken: true, detail: "Queued." });
    }, { preconnect() {} });
    const outcome = await sendReply("repo key", "abc/1", "rebase first", "secret");
    expect(outcome.woken).toBe(true);
    expect(calls[0]?.url).toBe("api/repositories/repo%20key/sessions/abc%2F1/messages");
    expect(calls[0]?.options?.method).toBe("POST");
    expect(new Headers(calls[0]?.options?.headers).get("Authorization")).toBe("Bearer secret");
    expect(new Headers(calls[0]?.options?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(calls[0]?.options?.body))).toEqual({ text: "rebase first" });
  });

  test("a refused reply says why", async () => {
    globalThis.fetch = Object.assign(async () => Response.json({ detail: "abc left at noon" }, { status: 409 }), { preconnect() {} });
    await expect(sendReply("r", "abc", "hello", "secret")).rejects.toThrow("abc left at noon");
  });
});
