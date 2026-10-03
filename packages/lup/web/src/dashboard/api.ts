import type {
  Broadcast, Claimed, ClaimRequest, Described, DescriptionRequest, FollowedFrom, FollowOutcome, FollowRequest, InboxRead, InboxReadRequest, KeyBindings, KeyLine, KeyTry,
  LiveNotice, MessagePage, MessageRequest, NameRequest, PostOutcome, PostRequest, Released, Renamed, ReplyOutcome, ReviewAnswer, ReviewDecision, ReviewDetail,
  ReviewHistory, ReviewRemarkRequest, ReviewSnapshot, SetupPane, Stopped, StreamFrame, TextRequest, TranscriptPage, Withdrawn,
} from "../generated/views";

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

/**
 * One page of History: `limit` of the reviews that left the queue, most recently settled first, from `offset`.
 * Naming a `review` reads the ones it links to instead, wherever in History they stand.
 */
export async function readHistory(offset: number, limit: number, token: string, signal?: AbortSignal, review: ReviewLink | null = null): Promise<ReviewHistory> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit),
    ...(review === null ? {} : { review: review.id, ...(review.root === null ? {} : { root: review.root }) }) });
  return (await accepted(await fetch(`api/reviews/history?${query}`, { headers: authorization(token), signal }))).json();
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

/** How a message reaches its agent: the post it answers, whether it redirects, and whether it interrupts the turn. */
export type Sending = Partial<Omit<MessageRequest, "text">>;

const repo = (repository: string) => `api/repositories/${encodeURIComponent(repository)}`;
const agent = (repository: string, member: string) => `${repo(repository)}/sessions/${encodeURIComponent(member)}`;

/** The operator's message to one session or subagent, addressed by its repository's key and its member id. */
export async function sendReply(repository: string, member: string, text: string, token: string, sending: Sending = {}): Promise<ReplyOutcome> {
  const request: MessageRequest = { text, in_reply_to: "", redirect: false, priority: "next", ...sending };
  return posted(`${agent(repository, member)}/messages`, request, token);
}

/** Make an agent look, with whatever waits for it, or a line saying the operator asked it to. */
export async function wakeAgent(repository: string, member: string, token: string): Promise<ReplyOutcome> {
  return posted(`${agent(repository, member)}/wake`, {}, token);
}

/** What the agent is called from now on. */
export async function renameAgent(repository: string, member: string, name: string, token: string): Promise<Renamed> {
  const request: NameRequest = { name };
  return posted(`${agent(repository, member)}/name`, request, token);
}

/** End an agent's runtime, where the dashboard can be sure which process it is. */
export async function stopAgent(repository: string, member: string, token: string): Promise<Stopped> {
  return posted(`${agent(repository, member)}/stop`, {}, token);
}

/** One post to every working member of a repository, each woken as a message is. */
export async function broadcastTo(repository: string, text: string, token: string): Promise<Broadcast> {
  const request: TextRequest = { text };
  return posted(`${repo(repository)}/broadcast`, request, token);
}

/** A standing notice every session of a repository reads at the head of each prompt. */
export async function postNotice(repository: string, text: string, token: string): Promise<LiveNotice> {
  const request: TextRequest = { text };
  return posted(`${repo(repository)}/notices`, request, token);
}

/** Take one standing notice down. */
export async function withdrawNotice(repository: string, id: string, token: string): Promise<Withdrawn> {
  return deleted(`${repo(repository)}/notices/${encodeURIComponent(id)}`, null, token);
}

/** What the operator is on, said on their row in every repository served. */
export async function describeYou(text: string, token: string): Promise<Described> {
  const request: DescriptionRequest = { text };
  return posted("api/user/description", request, token);
}

/** Hold an absolute path as the operator: an agent writing under it is asked first. */
export async function holdPath(repository: string, path: string, token: string): Promise<Claimed> {
  const request: ClaimRequest = { path };
  return posted(`${repo(repository)}/claims`, request, token);
}

/** Give back a path the operator holds. */
export async function releasePath(repository: string, path: string, token: string): Promise<Released> {
  const request: ClaimRequest = { path };
  return deleted(`${repo(repository)}/claims`, request, token);
}

/** Take exactly these messages out of the operator's mailbox, as read. */
export async function readInbox(repository: string, ids: [string, ...string[]], token: string): Promise<InboxRead> {
  const request: InboxReadRequest = { ids };
  return posted(`${repo(repository)}/inbox/read`, request, token);
}

/** One post into a discussion, to everyone in it, answering its latest post or the one named. */
export async function postInto(repository: string, thread: string, request: PostRequest, token: string): Promise<PostOutcome> {
  return posted(`${repo(repository)}/threads/${encodeURIComponent(thread)}/posts`, request, token);
}

/** One page of an agent's transcript, its lines ending by byte `before`, or its latest page. */
export async function readTranscript(repository: string, member: string, token: string, before: number | null = null, signal?: AbortSignal): Promise<TranscriptPage> {
  const query = before === null ? "" : `?${new URLSearchParams({ before: String(before) })}`;
  return (await accepted(await fetch(`${agent(repository, member)}/transcript${query}`, { headers: authorization(token), signal }))).json();
}

/** Follow these transcripts from where the tab has read each, renewed while the tab shows them. */
export async function followTranscripts(sessions: FollowedFrom[], token: string): Promise<FollowOutcome> {
  const request: FollowRequest = { sessions };
  return posted("api/transcripts/follow", request, token);
}

/**
 * One older page of a repository's messages, oldest first: those whose lines on its mail record end
 * by byte `before`, which is where the messages the page holds for it start.
 */
export async function readMessages(repository: string, before: number, token: string, signal?: AbortSignal): Promise<MessagePage> {
  const query = new URLSearchParams({ before: String(before) });
  return (await accepted(await fetch(`api/repositories/${encodeURIComponent(repository)}/messages?${query}`, {
    headers: authorization(token), signal,
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

async function posted<Reply>(path: string, body: unknown, token: string): Promise<Reply> {
  return (await accepted(await fetch(path, {
    method: "POST",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }))).json();
}

/** A `DELETE`, held to the same capability, origin and JSON a `POST` is; *body* null sends none. */
async function deleted<Reply>(path: string, body: unknown, token: string): Promise<Reply> {
  return (await accepted(await fetch(path, {
    method: "DELETE",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    ...(body === null ? {} : { body: JSON.stringify(body) }),
  }))).json();
}

/** The person's keys with this tab's `:map` lines checked over them, by the same catalog their config is checked against. */
export async function tryKeys(lines: KeyLine[], token: string): Promise<KeyBindings> {
  const tried: KeyTry = { lines };
  return posted("api/keys/try", tried, token);
}

/** Write this tab's `:map` lines into `[dashboard.keys]` of the person's lup config, its comments kept. */
export async function writeKeys(lines: KeyLine[], token: string): Promise<KeyBindings> {
  const tried: KeyTry = { lines };
  return posted("api/keys", tried, token);
}
