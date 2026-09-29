import { afterEach, describe, expect, test } from "bun:test";
import { answerReview, followInbox, readInbox, readReviewLink, reviewLink, takeToken } from "./api";

const originalFetch = globalThis.fetch;
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const snapshot = { roots: [], reviews: [], errors: [] };

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
    expect(localStorage.getItem("lup-review-token")).toBe("operator-secret");
    expect(sessionStorage.getItem("lup-review-token")).toBeNull();
  });

  test("authenticates reads and answers without putting the capability in the URL or body", async () => {
    const calls: { url: string; options?: RequestInit }[] = [];
    globalThis.fetch = Object.assign(async (input: string | URL | Request, options?: RequestInit) => {
      calls.push({ url: String(input), options });
      return Response.json(snapshot);
    }, { preconnect() {} });
    await readInbox("operator-secret");
    await answerReview("tree/request", { approved: false, note: "Use a scoped change", fingerprint: "exact" }, "operator-secret");
    expect(calls.map((call) => call.url)).toEqual(["api/reviews", "api/reviews/tree%2Frequest/answer"]);
    for (const call of calls) expect(new Headers(call.options?.headers).get("Authorization")).toBe("Bearer operator-secret");
    expect(JSON.parse(String(calls[1]?.options?.body))).toEqual({ approved: false, note: "Use a scoped change", fingerprint: "exact" });
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
    await readInbox("");
    expect(headers.has("Authorization")).toBe(false);
  });

  test("stale tab storage never overrides the origin's current launch token", () => {
    sessionStorage.setItem("lup-review-token", "previous-launch");
    localStorage.setItem("lup-review-token", "current-launch");
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
    expect(localStorage.getItem("lup-review-token")).toBeNull();
  });

  test("a denied storage read reports the problem and preserves access already held in memory", () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new DOMException("Storage blocked", "SecurityError"); } });
    expect(takeToken().token).toBe("");
    expect(takeToken("current-tab")).toEqual({ token: "current-tab", notice: expect.stringContaining("Browser storage is unavailable") });
  });
});

describe("review stream", () => {
  test("decodes records and UTF-8 across arbitrary byte boundaries", async () => {
    const wanted = { ...snapshot, errors: [{ root: "é/check", message: "Readable" }] };
    const encoded = new TextEncoder().encode(`${JSON.stringify(wanted)}\n\n${JSON.stringify(snapshot)}`);
    globalThis.fetch = Object.assign(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of encoded) controller.enqueue(Uint8Array.of(byte));
        controller.close();
      },
    })), { preconnect() {} });
    const received = [];
    for await (const entry of followInbox("secret", new AbortController().signal)) received.push(entry);
    expect(received).toEqual([wanted, snapshot]);
  });

  test("surfaces refused credentials instead of decoding an error as a snapshot", async () => {
    globalThis.fetch = Object.assign(async () => Response.json({ detail: "Access denied" }, { status: 403 }), { preconnect() {} });
    await expect(followInbox("bad", new AbortController().signal).next()).rejects.toThrow("Access denied");
  });

  test("closing a reader cancels the underlying stream", async () => {
    let cancelled = false;
    globalThis.fetch = Object.assign(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode(`${JSON.stringify(snapshot)}\n`)); },
      cancel() { cancelled = true; },
    })), { preconnect() {} });
    const reader = followInbox("secret", new AbortController().signal);
    await reader.next();
    await reader.return(undefined);
    expect(cancelled).toBe(true);
  });
});
