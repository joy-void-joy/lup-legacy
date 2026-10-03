// What the dashboard's server serves of supervising, as the stream's whole
// state names it (`served`), and what each piece needs from a server that does
// not. The page is served by the dashboard it talks to, but that dashboard can
// run older code than its checkout (the running-code notice says so), so an
// action whose route this server does not serve is refused naming the route,
// its draft kept, rather than sent to a route that would answer 404.
import type { Feature } from "../generated/views";

export type { Feature };

/** What each piece of supervising needs from the dashboard's server, as a refusal names it. */
export const NEEDS: Record<Feature, string> = {
  "reply-thread": "in_reply_to on POST …/sessions/<member>/messages",
  redirect: "redirect on POST …/sessions/<member>/messages",
  interrupt: "priority \"now\" on POST …/sessions/<member>/messages",
  "bare-wake": "POST …/sessions/<member>/wake",
  rename: "POST …/sessions/<member>/name",
  stop: "POST …/sessions/<member>/stop",
  transcript: "GET …/sessions/<member>/transcript and transcript frames on the stream",
  notices: "POST and DELETE …/repositories/<key>/notices",
  describe: "POST /api/user/description",
  claims: "POST and DELETE …/repositories/<key>/claims",
  "inbox-read": "POST …/repositories/<key>/inbox/read",
  "thread-post": "POST …/repositories/<key>/threads/<thread>/posts",
  budgets: "POST /api/budget/turtle, POST …/sessions/<member>/budget and budget frames on the stream",
  profiles: "POST …/repositories/<key>/profile",
};

/** Why an action cannot run against a server serving *served*, or nothing where it serves what the action needs. */
export function unserved(served: readonly Feature[], feature: Feature): string {
  return served.includes(feature) ? "" : `this dashboard's server is older than the page and does not serve ${NEEDS[feature]}`;
}
