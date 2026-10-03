// The finder: Telescope-style pickers over reviews, History, agents, the inbox, the discussions,
// a review's files, the lines of the buffer, every message, the commands, the
// keys, and a review's markers and exceptions. Type to filter; matched letters
// are underlined; the preview shows what Enter opens.
import type { Dashboard } from "./dashboard";
import { jumpTo, openMessage, reveal, rowsOf } from "./editor";
import { CATALOG } from "./keys";
import { basename, EFFECT_SIGN, exceptionRules, exceptionStops, headOf, headText, MARKER_LETTER, markerLabel, markerStops, relative, reviewLabel, rowText, SOURCES, stateLabel, stateSign, type Entry } from "./review";
import type { Tone } from "./state";
import { activityBrief, attention, clock, GLYPH, inboxOf, kindWords, mailHeads, standing } from "./supervision";
import { discussionLine, memberName } from "./threads";

/** What a picker's preview pane shows. */
export type Preview = { heading: string; lines: { text: string; tone: Tone | "muted" }[] };

export type FinderItem = { text: string; run: (d: Dashboard) => void; preview?: () => Preview };

/** A query's letters, in order, in a text: where each matched, and how well the whole matches. */
export function score(query: string, text: string): { score: number; at: number[] } | null {
  if (query === "") return { score: 0, at: [] };
  const haystack = text.toLowerCase();
  let position = 0;
  let total = 0;
  let run = 0;
  const at: number[] = [];
  for (const char of query.toLowerCase()) {
    if (char === " ") continue;
    const found = haystack.indexOf(char, position);
    if (found < 0) return null;
    run = found === position ? run + 1 : 0;
    total += 1 + run * 2 + (found === 0 || /[\s/._:-]/.test(haystack[found - 1] ?? "") ? 3 : 0);
    at.push(found);
    position = found + 1;
  }
  return { score: total - text.length / 200, at };
}

function reviewItems(d: Dashboard, rows: ReturnType<Dashboard["rows"]>): FinderItem[] {
  return rows.map((row) => {
    const entry: Entry = d.entry(row.key) ?? { row, detail: null };
    return {
      text: `${stateSign(row)} ${d.short(row)} · ${row.session || row.requester} · ${row.id.slice(0, 8)} · ${stateLabel(row)}`,
      run: (dashboard) => dashboard.openReview(row.key, { mode: "normal" }),
      preview: () => ({
        heading: headText(headOf(entry)),
        lines: [
          { text: row.title, tone: "muted" },
          { text: `${row.rule || "unattributed"} — ${row.reason}`, tone: "warn" },
          ...(entry.detail?.question.account ?? []).flatMap((said) => [{ text: SOURCES[said.source] ?? said.source, tone: "info" as const }, { text: said.text, tone: "" as const }]),
        ],
      }),
    };
  });
}

/** Every picker, by the name `:find` and the leader keys open it with. */
export const PICKERS: Record<string, { title: string; items: (d: Dashboard) => FinderItem[] }> = {
  reviews: { title: "reviews", items: (d) => reviewItems(d, [...d.pending(), ...d.settled()]) },
  history: { title: "history", items: (d) => reviewItems(d, d.settled()) },
  agents: {
    title: "agents",
    items: (d) => {
      const live = d.state.live;
      if (live === null) return [];
      return [...live.sessions.values()].sort((left, right) => Number(right.running) - Number(left.running) || (left.name || left.id).localeCompare(right.name || right.id)).map((session) => {
        const state = standing(session, d.state.now);
        return {
          text: `${GLYPH[state]} ${session.name || session.id} · ${live.repositories.get(session.repository)?.name ?? ""} · ${kindWords(session)} · ${activityBrief(session, d.state.now)}`,
          run: (dashboard) => dashboard.openOther("member", session.key),
          preview: () => ({
            heading: `${session.name || session.id} · ${state} · ${kindWords(session)}`,
            lines: [{ text: session.doing, tone: "" }, { text: activityBrief(session, d.state.now), tone: "muted" }, ...attention(live, d.roots(), d.pending(), session, d.state.now).map((flag) => ({ text: flag.text, tone: "warn" as const }))],
          }),
        };
      });
    },
  },
  threads: {
    title: "discussions",
    items: (d) => {
      const live = d.state.live;
      if (live === null) return [];
      return d.discussions().map((discussion) => ({
        text: `${discussion.unread > 0 ? "●" : "○"} ${discussion.title} · ${discussionLine(live, discussion)}`,
        run: (dashboard) => dashboard.openThread(discussion.key),
        preview: () => ({
          heading: discussion.title,
          lines: discussion.posts.flatMap((post) => [
            { text: `${memberName(live, discussion.repository, post.sender)} · ${clock(post.sent_at)}`, tone: "info" as const },
            { text: post.text, tone: "" as const },
          ]),
        }),
      }));
    },
  },
  inbox: {
    title: "inbox",
    items: (d) => {
      const live = d.state.live;
      if (live === null) return [];
      return inboxOf(live).map((message) => {
        const heads = mailHeads(live, message);
        return {
          text: `${message.waiting ? "●" : "○"} ${heads.from} · ${message.text}`,
          run: (dashboard) => openMessage(dashboard, message),
          preview: () => ({ heading: `${heads.from} → you`, lines: [{ text: live.repositories.get(message.repository)?.name ?? "", tone: "muted" }, { text: message.text, tone: "" }] }),
        };
      });
    },
  },
  files: {
    title: "files",
    items: (d) => {
      const entry = d.current();
      if (entry === null || entry.detail === null) return [];
      return entry.detail.files.map((file, fi) => ({
        text: `${EFFECT_SIGN[file.review_effect] ?? "·"} ${relative(file.path, entry.row.target)} +${file.additions} −${file.deletions}`,
        run: (dashboard) => jumpTo(dashboard, rowsOf(dashboard).findIndex((row) => row.t === "file" && row.fi === fi), "top"),
        preview: () => ({ heading: file.path, lines: [{ text: `${reviewLabel(file.review_effect)}: ${file.review_reason}`, tone: "warn" }, ...(file.about !== "" ? [{ text: "the agent's note", tone: "info" as const }, { text: file.about, tone: "" as const }] : [])] }),
      }));
    },
  },
  lines: {
    title: "lines",
    items: (d) => rowsOf(d).filter((row) => ["line", "cmd", "seg", "mail", "said", "json", "log"].includes(row.t)).map((row) => ({
      text: row.t === "line" ? `${basename(d.current()?.detail?.files[row.fi]?.path ?? "")}:${row.num ?? ""} ${row.text}` : rowText(row),
      run: (dashboard) => jumpTo(dashboard, row.i),
    })),
  },
  messages: {
    title: "messages",
    items: (d) => {
      const live = d.state.live;
      if (live === null) return [];
      return [...live.messages.values()].sort((left, right) => Date.parse(right.sent_at) - Date.parse(left.sent_at)).map((message) => {
        const heads = mailHeads(live, message);
        return {
          text: `${heads.from} → ${heads.to} · ${message.text}`,
          run: (dashboard) => openMessage(dashboard, message),
          preview: () => ({ heading: `${heads.from} → ${heads.to}`, lines: [{ text: message.text, tone: "" }] }),
        };
      });
    },
  },
  commands: { title: "commands", items: () => [] },
  keys: {
    title: "keys",
    items: (d) => CATALOG.actions.filter((action) => !action.hidden).map((action) => ({
      text: `${d.keymap.row(action)} · ${action.description} · ${action.name}${d.keymap.yours(action) ? " · yours" : ""}${action.needs !== null && d.lacks(action.needs) !== "" ? " · not served here" : ""}`,
      run: () => undefined,
    })),
  },
  markers: {
    title: "markers",
    items: (d) => {
      const entry = d.current();
      if (entry === null || entry.detail === null) return [];
      const full = d.ui(entry.row.key).full;
      return [
        ...markerStops(entry.detail, full).map((stop) => ({ text: `${MARKER_LETTER[stop.marker.kind]} ${basename(stop.file.path)}:${stop.marker.line} ${markerLabel(stop.marker)} · ${stop.marker.text.split("\n")[0] ?? ""}`, run: (dashboard: Dashboard) => { reveal(dashboard, stop.fi, stop.marker.side, stop.marker.line); } })),
        ...exceptionStops(entry.detail, full).map((stop) => ({ text: `X ${basename(stop.file.path)}:${stop.suppression.line} exception · ${exceptionRules(stop.suppression)} · ${stop.suppression.reason}`, run: (dashboard: Dashboard) => { reveal(dashboard, stop.fi, "after", stop.suppression.line); } })),
      ];
    },
  },
};

export function openFinder(d: Dashboard, picker: string): void {
  if (PICKERS[picker] === undefined) { d.say(`E: :find takes ${Object.keys(PICKERS).join(", ")}`, "err"); return; }
  d.set({ float: { kind: "finder", picker, query: "", cur: 0 }, cmdline: null });
}

/** One picker's items for a query, best first, each with the letters it matched. */
export function found(d: Dashboard, picker: string, query: string, commands: FinderItem[]): { item: FinderItem; at: number[] }[] {
  const items = picker === "commands" ? commands : PICKERS[picker]?.items(d) ?? [];
  const scored = items.map((item) => ({ item, match: score(query.trim(), item.text) })).filter((each): each is { item: FinderItem; match: { score: number; at: number[] } } => each.match !== null);
  if (query.trim() !== "") scored.sort((left, right) => right.match.score - left.match.score);
  return scored.map(({ item, match }) => ({ item, at: match.at }));
}

/** Close the finder and do what its item does. */
export function pick(d: Dashboard, item: FinderItem): void {
  d.set({ float: null });
  d.focusWin(d.state.focus === "composer" ? "editor" : d.state.focus);
  item.run(d);
}
