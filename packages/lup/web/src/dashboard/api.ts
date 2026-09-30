import type { ReplyOutcome, ReplyRequest, ReviewAnswer, ReviewDecision, ReviewDetail, ReviewRemarkRequest, ReviewSnapshot, SetupPane, StreamFrame } from "../generated/views";

/** Where this origin keeps the operator's capability, and the key a storage event names. */
export const TOKEN_KEY = "lup-dashboard-token";
export type ReviewAccess = { token: string; notice: string };

/** Share the launch capability only with tabs at this exact browser origin. */
export function takeToken(previous = ""): ReviewAccess {
  const url = new URL(window.location.href);
  const fragment = new URLSearchParams(url.hash.slice(1));
  const supplied = fragment.get("token");
  if (supplied !== null) {
    fragment.delete("token");
    url.hash = fragment.toString();
    window.history.replaceState(null, "", url);
  }
  try {
    if (supplied !== null) localStorage.setItem(TOKEN_KEY, supplied);
    return { token: supplied ?? localStorage.getItem(TOKEN_KEY) ?? "", notice: "" };
  } catch {
    return { token: supplied ?? previous, notice: "Browser storage is unavailable. Open the operator's launch link in each tab; access cannot be shared between tabs." };
  }
}

export class ReviewError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function accepted(response: Response): Promise<Response> {
  if (response.ok) return response;
  const payload: unknown = await response.json().catch(() => null);
  const detail = (payload as { detail?: unknown } | null)?.detail;
  throw new ReviewError(response.status, typeof detail === "string" ? detail : `HTTP ${response.status}`);
}

function authorization(token: string): Record<string, string> {
  return token === "" ? {} : { Authorization: `Bearer ${token}` };
}

export type ReviewLink = { id: string; root: string | null };

export function readReviewLink(): ReviewLink | null {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const id = fragment.get("review");
  return id === null ? null : { id, root: fragment.get("root") };
}

/** A shareable address names a review and never carries browser authority. */
export function reviewLink(id: string, root: string | null = null): string {
  const url = new URL(window.location.href);
  const fragment = new URLSearchParams({ review: id });
  if (root !== null) fragment.set("root", root);
  url.search = "";
  url.hash = fragment.toString();
  return url.href;
}

/** Each repository's setup pane, by the path that opens it. */
export async function readSetupPanes(token: string, signal?: AbortSignal): Promise<SetupPane[]> {
  return (await accepted(await fetch("api/setup", { headers: authorization(token), signal }))).json();
}

export async function readReviews(token: string, signal?: AbortSignal): Promise<ReviewSnapshot> {
  return (await accepted(await fetch("api/reviews", { headers: authorization(token), signal }))).json();
}

export async function readReview(key: string, token: string, signal?: AbortSignal): Promise<ReviewDetail> {
  return (await accepted(await fetch(`api/reviews/${encodeURIComponent(key)}`, {
    headers: authorization(token), signal,
  }))).json();
}

export async function answerReview(key: string, answer: ReviewAnswer, token: string): Promise<ReviewDecision> {
  return (await accepted(await fetch(`api/reviews/${encodeURIComponent(key)}/answer`, {
    method: "POST",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    body: JSON.stringify(answer),
  }))).json();
}

/** The operator's note and line comments on a review, sent without deciding it. */
export async function remarkReview(key: string, remark: ReviewRemarkRequest, token: string): Promise<ReviewDecision> {
  return (await accepted(await fetch(`api/reviews/${encodeURIComponent(key)}/remark`, {
    method: "POST",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    body: JSON.stringify(remark),
  }))).json();
}

/** The operator's message to one session or subagent, addressed by its repository's key and its member id. */
export async function sendReply(repository: string, member: string, text: string, token: string): Promise<ReplyOutcome> {
  const request: ReplyRequest = { text };
  return (await accepted(await fetch(`api/repositories/${encodeURIComponent(repository)}/sessions/${encodeURIComponent(member)}/messages`, {
    method: "POST",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    body: JSON.stringify(request),
  }))).json();
}

/** What the stream hands a tab: a frame to apply, or word that the tab is now current. */
export type Followed = { kind: "frame"; frame: StreamFrame } | { kind: "live" };

/** One server-sent event's fields, as the event-stream format spells them, one per line. */
function followed(block: string): Followed | null {
  const lines = block.split("\n");
  const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart());
  if (data.length > 0) return { kind: "frame", frame: JSON.parse(data.join("\n")) as StreamFrame };
  return lines.some((line) => line === ": live") ? { kind: "live" } : null;
}

/**
 * Follow everything live on the dashboard, from the frame named by `resume` where the tab saw one.
 * Events and UTF-8 may span chunks. Silence is bounded once the stream has said anything, since
 * the dashboard says it is still there at least every fifteen seconds.
 */
export async function* followDashboard(token: string, signal: AbortSignal, resume = "", silenceMs = 45_000): AsyncGenerator<Followed> {
  const response = await accepted(await fetch("api/stream", {
    headers: { ...authorization(token), ...(resume === "" ? {} : { "Last-Event-ID": resume }) }, signal,
  }));
  if (response.body === null) throw new Error("The dashboard stream has no response body.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let received = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    for (;;) {
      const reading = reader.read();
      const chunk = received ? await Promise.race([reading, new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("The dashboard stream stopped updating.")), silenceMs);
      })]) : await reading;
      clearTimeout(timer);
      buffered += decoder.decode(chunk.value, { stream: !chunk.done }).replaceAll("\r\n", "\n");
      if (chunk.value !== undefined && chunk.value.length > 0) received = true;
      let boundary = buffered.indexOf("\n\n");
      for (; boundary !== -1; boundary = buffered.indexOf("\n\n")) {
        const block = buffered.slice(0, boundary);
        buffered = buffered.slice(boundary + 2);
        const entry = followed(block);
        if (entry !== null) yield entry;
      }
      if (chunk.done) {
        const entry = buffered.trim() === "" ? null : followed(buffered);
        if (entry !== null) yield entry;
        return;
      }
    }
  } finally {
    clearTimeout(timer);
    try {
      await reader.cancel();
    } finally {
      reader.releaseLock();
    }
  }
}
