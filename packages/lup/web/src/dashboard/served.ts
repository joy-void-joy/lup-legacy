// What the dashboard's server serves, and what each action waiting on new
// server work needs from it (decision 62). The page builds only what today's
// server supports; every action the catalog marks `server: "new"` is refused
// here with the route or field it waits on, rather than pretending to work.
// When the server serves one, it lands in `SERVED` and its action lights up.

/** A piece of server work an action waits on, by decision 62's item. */
export type Feature =
  | "reply-thread"
  | "redirect"
  | "interrupt"
  | "bare-wake"
  | "rename"
  | "stop"
  | "transcript"
  | "notices"
  | "describe"
  | "claims"
  | "inbox-read"
  | "thread-post";

/** What each piece of server work is, as a refusal names it. */
export const NEEDS: Record<Feature, string> = {
  "reply-thread": "in_reply_to on POST …/sessions/<member>/messages (decision 62, item 6)",
  redirect: "redirect on POST …/sessions/<member>/messages (decision 62, item 6)",
  interrupt: "priority \"now\" on POST …/sessions/<member>/messages, measured live on Claude first (decision 62, items 6 and 16)",
  "bare-wake": "POST …/sessions/<member>/wake over the wake the message route already makes (decision 62, item 7)",
  rename: "POST …/sessions/<member>/name over RepositoryPeers.rename (decision 62, item 8)",
  stop: "POST …/sessions/<member>/stop, checking the runtime's pid, start time and namespace (decision 62, item 9)",
  transcript: "GET …/sessions/<member>/transcript and transcript frames on the stream (decision 62, items 2 and 5)",
  notices: "POST and DELETE …/repositories/<key>/notices over RepositoryPeers.notify (decision 62, item 11)",
  describe: "the user row in coordination_peers, and POST /api/user/description (decision 62, items 12 and 14)",
  claims: "the user's claims counted, and POST and DELETE …/repositories/<key>/claims (decision 62, items 13 and 14)",
  "inbox-read": "POST …/repositories/<key>/inbox/read committing your mailbox (decision 62, item 15)",
  "thread-post": "POST …/repositories/<key>/threads/<thread>/posts, one message per participant sharing a post and a thread id, with thread on coordination_send so agents answer into it (decision 116, items 1–3)",
};

/** What this dashboard's server serves of the work above: nothing yet. */
export const SERVED: ReadonlySet<Feature> = new Set();

/** Why an action cannot run here yet, or nothing where the server serves what it needs. */
export function unserved(feature: Feature): string {
  return SERVED.has(feature) ? "" : `needs new server work: ${NEEDS[feature]}`;
}
