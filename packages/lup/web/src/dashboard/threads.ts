// Discussions: the coordination mail read as threads (decisions 113–116). The
// mail record holds one message per recipient, each with its sender, its
// `in_reply_to` (a post), whether it redirects, and the post and thread it
// belongs to. A discussion is what those messages add up to:
//
// - a post is the copies one send left in several mailboxes, sharing its
//   `post` id, shown once with every recipient;
// - a thread is the posts sharing a `thread`, which is its first post's id,
//   and the posts that reply to each other, transitively;
// - a conversation is the posts between the same members that reply to
//   nothing and nothing replies to: their running exchange.
//
// A message the record kept from before it named posts has neither id, and is
// one post with the copies sharing its sender, text and time, threaded by its
// `in_reply_to` alone.
import type { LiveMessage } from "../generated/views";
import type { LiveState } from "./live";
import { plural, Rows, type Buffer } from "./review";
import { memberById } from "./supervision";

/** One send: its first copy's fields, and every copy it left, one per recipient. */
export type Post = {
  /** Its post id, which a reply names; a message from before posts had ids is its own. */
  id: string;
  /** The thread it is in, its first post's id; empty for a message from before threads. */
  thread: string;
  repository: string;
  sender: string;
  door: string;
  text: string;
  sent_at: string;
  in_reply_to: string;
  redirect: boolean;
  /** A bare prompt its recipient's runtime was woken with, a resume's "continue", rather than a message. */
  prompt: boolean;
  copies: LiveMessage[];
};

export type Discussion = {
  key: string;
  repository: string;
  kind: "thread" | "conversation";
  posts: Post[];
  /** Everyone who wrote in it or was written to, the operator (`user`) included. */
  participants: string[];
  title: string;
  /** The first post's id: a thread's root. */
  root: string;
  /** Where a post into it goes: the thread its latest post is in, which the server reads its participants from. */
  thread: string;
  last: string;
  /** Copies addressed to the operator still waiting in their mailbox. */
  unread: number;
};

/** A member's name as a discussion shows it: `you` for the operator, the roster's name, or the id. */
export function memberName(live: LiveState, repository: string, id: string): string {
  if (id === "user") return "you";
  const session = memberById(live, repository, id);
  return session === undefined ? id : session.name || session.id;
}

const sentOrder = (left: LiveMessage, right: LiveMessage) => Date.parse(left.sent_at) - Date.parse(right.sent_at) || left.at - right.at;

/** Every discussion the mail the page holds adds up to, the most recently written first. */
// lup: solved: group by each message's `post` and `thread` once LiveMessage carries them (feat-supervision-server): a thread's first post has `thread` equal to its own `post`, a one-post thread replying to nothing is conversation material, and a legacy message reads thread "" and keeps today's grouping
export function discussions(live: LiveState): Discussion[] {
  const posts: Post[] = [];
  const byIdentity = new Map<string, Post>();
  const postOf = new Map<string, Post>();
  for (const message of [...live.messages.values()].sort(sentOrder)) {
    // The post id where the record names one; the copies' shared sender, text and time where it does not.
    const identity = message.post !== "" ? JSON.stringify([message.repository, message.post])
      : JSON.stringify([message.repository, message.sender, message.sent_at, message.text]);
    const known = byIdentity.get(identity);
    const post = known ?? {
      id: message.post || message.id, thread: message.thread, repository: message.repository, sender: message.sender, door: message.door,
      text: message.text, sent_at: message.sent_at, in_reply_to: message.in_reply_to, redirect: message.redirect, prompt: message.prompt, copies: [],
    };
    if (known === undefined) {
      byIdentity.set(identity, post);
      posts.push(post);
    }
    post.copies.push(message);
    if (post.in_reply_to === "") post.in_reply_to = message.in_reply_to;
    postOf.set(`${message.repository}/${message.id}`, post);
    postOf.set(`${message.repository}/${post.id}`, post);
  }
  // The posts sharing a thread, and those that reply to each other, transitively, are one thread:
  // a union of each reply with what it answers, and of each post with its thread's first one here.
  const parent = new Map(posts.map((post) => [post, post]));
  const find = (post: Post): Post => {
    const above = parent.get(post) ?? post;
    if (above === post) return post;
    const root = find(above);
    parent.set(post, root);
    return root;
  };
  const linked = new Set<Post>();
  const link = (earlier: Post, later: Post): void => {
    const [early, late] = [find(earlier), find(later)].sort((left, right) => posts.indexOf(left) - posts.indexOf(right));
    if (early !== undefined && late !== undefined) parent.set(late, early);
    linked.add(earlier);
    linked.add(later);
  };
  const threadFirst = new Map<string, Post>();
  for (const post of posts) {
    const answered = post.in_reply_to === "" ? undefined : postOf.get(`${post.repository}/${post.in_reply_to}`);
    if (answered !== undefined) link(answered, post);
    if (post.thread === "") continue;
    const first = threadFirst.get(`${post.repository}/${post.thread}`);
    if (first === undefined) threadFirst.set(`${post.repository}/${post.thread}`, post);
    else link(first, post);
  }
  const groups = new Map<string, { key: string; repository: string; kind: Discussion["kind"]; posts: Post[] }>();
  for (const post of posts) {
    const members = [...new Set([post.sender, ...post.copies.map((copy) => copy.recipient)])].sort();
    const thread = linked.has(post);
    const key = thread ? `${post.repository}/thread/${find(post).id}` : `${post.repository}/with/${members.join("+")}`;
    const group = groups.get(key) ?? { key, repository: post.repository, kind: thread ? "thread" as const : "conversation" as const, posts: [] };
    groups.set(key, group);
    group.posts.push(post);
  }
  return [...groups.values()].flatMap((group) => {
    const first = group.posts[0];
    const last = group.posts[group.posts.length - 1];
    if (first === undefined || last === undefined) return [];
    const participants = [...new Set(group.posts.flatMap((post) => [post.sender, ...post.copies.map((copy) => copy.recipient)]))];
    const others = participants.filter((id) => id !== "user").map((id) => memberName(live, group.repository, id));
    const title = group.kind === "thread" ? first.text.split("\n")[0] ?? "" : `${others.join(", ")}${participants.includes("user") ? " and you" : ""}`;
    const copies = group.posts.flatMap((post) => post.copies);
    return [{
      ...group, participants, title, root: first.id, thread: last.thread || last.id, last: last.sent_at,
      unread: copies.filter((copy) => copy.recipient === "user" && copy.waiting).length,
    }];
  }).sort((left, right) => Date.parse(right.last) - Date.parse(left.last));
}

/** The words a discussion row says: its title, repository, kind, size and who is in it. */
export function discussionLine(live: LiveState, discussion: Discussion): string {
  const names = discussion.participants.map((id) => memberName(live, discussion.repository, id)).join(", ");
  return `${live.repositories.get(discussion.repository)?.name ?? ""} · ${discussion.kind} · ${plural(discussion.posts.length, "post")} · ${names}`;
}

/** The discussion whole: a heading naming everyone in it, then each post in order. */
export function threadBuffer(live: LiveState, discussion: Discussion): Buffer {
  const out = new Rows();
  const names = discussion.participants.map((id) => memberName(live, discussion.repository, id));
  out.push({ t: "sec", key: "head", text: discussion.title, sub: `${discussion.kind === "thread" ? "a thread: these posts reply to each other" : "a conversation: these members wrote to each other, replying to nothing"} · ${names.join(", ")}` });
  // A reply names a post by its id; one from before posts had ids named a copy.
  const byCopy = new Map(discussion.posts.flatMap((post) => [[post.id, post] as const, ...post.copies.map((copy) => [copy.id, post] as const)]));
  for (const post of discussion.posts) {
    const answered = post.in_reply_to === "" ? null : byCopy.get(post.in_reply_to) ?? null;
    const unread = post.copies.some((copy) => copy.recipient === "user" && copy.waiting);
    out.push({ t: "post", key: `post:${post.repository}/${post.id}`, post, answered, unread });
  }
  return out.buffer();
}

/** Who a post into the discussion reaches: everyone in it but the operator. */
export const reaches = (live: LiveState, discussion: Discussion) =>
  discussion.participants.filter((id) => id !== "user").map((id) => memberName(live, discussion.repository, id));
