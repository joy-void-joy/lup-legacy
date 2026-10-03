// The floating windows. Each is drawn only while it is open — never hidden in
// place, so no stylesheet rule can leave a closed one on the screen — and every
// one closes the same way: `q`, `Esc` or `Ctrl+[`, its ✕, or the key that
// opened it. Floats have a border and no shadow, as the high-contrast themes do.
import { useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import type { Dashboard } from "./dashboard";
import { COMMANDS, openCommand, runCommand } from "./commands";
import { rowHere } from "./editor";
import { found, pick, type FinderItem } from "./finder";
import { highlightedLines } from "./highlight";
import { CATALOG, prettyKeys } from "./keys";
import { MODE_TEXT, PALETTE, worst } from "./palette";
import { exceptionRules, headOf, headText, judgedOf, markerLabel, plural, reviewLabel, sentComments } from "./review";
import { unserved } from "./served";
import type { PageState } from "./state";
import { activityBrief, attention, clock, kindWords, mailHeads, standing, stamp } from "./supervision";
import { memberName } from "./threads";

/** A value as pretty-printed JSON, coloured, every string's `\n` followed by a real line break so a document reads by its lines and stays exact JSON. */
export function Json({ value }: { value: unknown }) {
  const lines = useMemo(() => highlightedLines(JSON.stringify(value, null, 2) ?? "null", "json"), [value]);
  return <pre className="json">{lines.map((line, index) => <span key={index}>{line.tokens.map((token, at) => <span key={at} className={token.classes === "" ? undefined : token.classes}>{token.text.replaceAll("\\n", "\\n\n")}</span>)}{"\n"}</span>)}</pre>;
}

function Shell({ d, title, hint, label, children }: { d: Dashboard; title: string; hint: string; label: string; children: ReactNode }) {
  const body = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    d.elements.float = body.current;
    body.current?.focus({ preventScroll: true });
    return () => { d.elements.float = null; };
  }, [d]);
  return <div className="float big" role="dialog" aria-label={label}>
    <div className="fh"><span>{title}</span><span className="grow" /><span className="muted hint">{hint}</span>
      <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ float: null })}>✕</button></div>
    <div className="fb" ref={body} tabIndex={-1}>{children}</div>
  </div>;
}

function Help({ d }: { d: Dashboard }) {
  const keymap = d.keymap;
  return <Shell d={d} title="keys · every binding the page answers" hint="? q or Esc closes" label="Keys">
    <p>The triage loop: a review opens in its note box. While the box is empty, <b>j</b>/<b>k</b> move to the next and previous review (and <b>↓</b>/<b>↑</b>, <b>Ctrl+d</b>/<b>Ctrl+u</b> scroll the diff); the first letter you type makes it yours, and from then on every key types. <b>Ctrl+Enter</b> approves, <b>Alt+Delete</b> declines and <b>Alt+Enter</b> sends without deciding, from anywhere; <b>Alt+↑</b>/<b>Alt+↓</b> move on while you type, and your draft stays with its review. Focus decides where keys act, and the statusline says where you are: in the tree <b>j</b>/<b>k</b> walk its rows, opening each; in the buffer the cursor moves like an editor's (<b>h j k l</b>, <b>w b e</b>, <b>0 ^ $</b>, counts, <b>gg</b>/<b>G</b>, <b>/</b> with <b>;</b> and <b>,</b>, <b>{"{"}</b>/<b>{"}"}</b>); in the context they walk its items. <b>Esc</b> leaves the box for the buffer; <b>Tab</b> and <b>Shift+Tab</b> move between the tree, the buffer, the box and the context, and a click moves there too. Every row names its action: your <code>[dashboard.keys]</code> rebinds it by that name, and <b>:map</b> shows what yours changed. Tags: <span className="ok">yours</span> is a binding you changed; <span className="ok">decided</span> one you chose; <span className="warn">changed</span> differs from the page before; <span className="info">new</span> is new. The last column says whether the dashboard's server supports it today or it needs new work.</p>
    <div className="cols">
      {CATALOG.groups.map((group) => <section key={group.id}><h4>{group.title}</h4>
        {CATALOG.actions.filter((action) => action.group === group.id && !action.hidden).map((action) => {
          const yours = keymap.yours(action);
          const lups = [action.name, ...action.also].map((name) => CATALOG.actions.find((each) => each.name === name)?.keys.map(prettyKeys).join(", ") || "unbound").join(" / ");
          return <div key={action.name} className={`kr${yours ? " yours" : ""}`}>
            <span className="ks">{keymap.row(action)}</span>
            <span>{action.description} <span className="muted">· {action.name}</span>{action.focus === "buffer" && <span className="muted"> · in the buffer</span>}{yours && <span className="ok"> · yours; lup's is {lups}</span>}{action.was !== "" && <span className="muted"> · was {action.was}</span>}{action.note !== "" && <span className="muted"> · {action.note}</span>}</span>
            <span className={`tag ${yours ? "yours" : action.stands}`}>{yours ? "yours" : action.stands}</span>
            <i className={`srv ${action.server}`}>{action.server === "new" ? "new server" : "works today"}</i>
          </div>;
        })}
      </section>)}
      <section><h4>command line (:)</h4>
        {COMMANDS.map((command) => <div key={command.name} className="kr"><span className="ks">:{command.name}{command.args !== undefined || command.takes === true ? " …" : ""}</span><span>{command.description}</span><span />
          <i className={`srv ${command.needs === undefined ? "today" : "new"}`}>{command.needs === undefined ? "works today" : "new server"}</i></div>)}
      </section>
      <section><h4>mouse and touch</h4>
        <div className="kr"><span className="ks">click a line number</span><span>comment on that line</span><span className="tag decided">decided</span><i /></div>
        <div className="kr"><span className="ks">Shift+click another</span><span>comment on the range between</span><span className="tag decided">decided</span><i /></div>
        <div className="kr"><span className="ks">click anything else</span><span>moves the cursor there; the tree, tabs, files, folds, context items and the statusline all answer a click</span><span className="tag new">new</span><i /></div>
        <div className="kr"><span className="ks">long-press a line</span><span>on a touch screen, start a range; tap another line to stretch it</span><span className="tag new">new</span><i /></div>
      </section>
    </div>
  </Shell>;
}

function FullContext({ d, state }: { d: Dashboard; state: PageState }) {
  const kind = d.centerKind(state);
  const live = state.live;
  const target = kind === "inbox" ? d.boxTarget(state) : null;
  const session = kind === "member" ? live?.sessions.get(state.sel.key) : target?.kind === "member" ? target.session : undefined;
  if (session !== undefined) {
    return <Shell d={d} title={`full context · ${session.name || session.id}`} hint="I q or Esc closes" label="Full context">
      <section className="cx"><h3>its row on the stream <span className="k">LiveSession</span></h3><Json value={session} /></section>
      <section className="cx"><h3>what new server work adds</h3><p className="muted">{unserved("transcript")}</p></section>
    </Shell>;
  }
  const entry = d.current(state);
  if (entry === null || entry.detail === null) return <Shell d={d} title="full context" hint="q or Esc closes" label="Full context"><p className="muted">Open a review or an agent first.</p></Shell>;
  const { row, detail } = entry;
  const root = d.roots(state).find((each) => each.id === row.root_id);
  const url = new URL(window.location.href);
  url.search = "";
  url.hash = new URLSearchParams({ review: row.id, root: row.root_id }).toString();
  const question = detail.question;
  const facts: [string, string][] = [
    ["review", row.id], ["link", url.href], ["queue checkout", root?.path ?? "checkout not present in the current watch list"], ["operation directory", question.operation.cwd],
    ["changes checkout", row.target], ["requester", `${row.requester}${question.member !== "" ? ` · member ${question.member}` : ""}${question.agent !== "" ? ` · subagent ${question.agent}` : ""}`],
    ["operation", row.operation], ["rule", row.rule || "unattributed"], ["fingerprint", question.fingerprint], ["created", row.created],
  ];
  return <Shell d={d} title={`full context · ${headText(headOf(entry))}`} hint="I q or Esc closes · scrolls with ↑↓ PgUp PgDn" label="Full context">
    <section className="cx"><dl className="facts">{facts.map(([name, value]) => <span key={name} className="fact"><dt>{name}</dt><dd>{value}</dd></span>)}</dl></section>
    <section className="cx"><h3>tool input <span className="k">{question.operation.tool}</span></h3><Json value={question.operation.payload} /></section>
    {detail.preview_notice !== "" && <section className="cx"><h3>how these documents were worked out</h3><p>{detail.preview_notice}</p></section>}
    <section className="cx"><h3>the complete record</h3><Json value={question} /></section>
  </Shell>;
}

function Messages({ d, state }: { d: Dashboard; state: PageState }) {
  return <Shell d={d} title=":messages · what this page said, oldest first" hint="q or Esc closes" label="Messages">
    {state.said.length === 0 ? <p className="muted">Nothing yet.</p> : state.said.map((line, index) => <p key={index} className="logl">{line}</p>)}
  </Shell>;
}

function Contrast({ d }: { d: Dashboard }) {
  return <Shell d={d} title="palette · sources and contrast" hint="q or Esc closes" label="Palette">
    <p>Each colour is taken from its source and, where it read below 7:1 on a background it sits on, moved along its own lightness until it reaches 7:1, hue and saturation kept. The theme follows the system's light or dark setting. Mode blocks read against their own text ({MODE_TEXT.dark} in dark, {MODE_TEXT.light} in light).</p>
    <table><thead><tr><th>theme</th><th>token</th><th /><th>shipped</th><th>source</th><th>worst contrast</th><th>role</th></tr></thead>
      <tbody>{(["dark", "light"] as const).flatMap((theme) => PALETTE.map((token) => {
        const shade = token[theme];
        const reads = worst(token, theme);
        const before = worst(token, theme, shade.source);
        return <tr key={`${theme}${token.name}`}><td>{theme}</td><td>{token.name}</td><td><span className="sw" style={{ background: shade.hex }} /></td>
          <td>{shade.hex}{shade.hex !== shade.source && <span className="muted"> (was {shade.source}{before !== null ? `, ${before.toFixed(2)}:1` : ""})</span>}</td>
          <td>{shade.from}</td><td>{reads === null ? "—" : `${reads.toFixed(2)}:1`}</td><td className="muted">{token.role}</td></tr>;
      }))}</tbody></table>
  </Shell>;
}

function Keys({ d }: { d: Dashboard }) {
  const keys = d.keys();
  const yours = d.keymap.bound.filter((bound) => bound.origin !== "lup");
  const toml = yours.map((bound) => `"${bound.action.name}" = ${bound.keys.length === 1 ? JSON.stringify(bound.keys[0]) : JSON.stringify(bound.keys)}`).join("\n");
  const line = (entry: (typeof keys.report.applied)[number], index: number) => <p key={index} className="logl"><b>{entry.action || "(unmap)"}</b> = {JSON.stringify(entry.keys)} <span className="muted">· {entry.origin === "tab" ? "this tab" : "your config"}</span>
    {entry.why !== "" && <><br /><span className="err">refused: `{entry.what}` {entry.why}{entry.way !== "" ? ` — ${entry.way}` : ""}</span></>}</p>;
  return <Shell d={d} title=":map · your keys" hint="q or Esc closes" label="Your keys">
    <p>From <b>{keys.source !== "" ? keys.source : "no lup config"}</b>, <code>[dashboard.keys]</code>: an action's name, then a key or a list of keys; <code>[]</code> unbinds it. <code>:map {"{action} {keys}"}</code> tries one in this tab, <code>:unmap {"{keys}"}</code> takes a key off whatever holds it, and <code>:mapwrite</code> writes this tab's lines to your config, keeping its comments.</p>
    {keys.unread !== "" && <p className="err">Your config could not be read, so lup's keys stand: {keys.unread}</p>}
    <h4>applied</h4>{keys.report.applied.length > 0 ? keys.report.applied.map(line) : <p className="muted">Nothing: every key is lup's default.</p>}
    <h4>refused</h4>{keys.report.refused.length > 0 ? keys.report.refused.map(line) : <p className="muted">Nothing refused.</p>}
    {keys.report.waits.length > 0 && <><h4>waits</h4>{keys.report.waits.map((wait) => <p key={wait} className="warn">{wait}</p>)}</>}
    <h4>to write down</h4><pre className="json">{`[dashboard.keys]\n${toml !== "" ? toml : "# nothing differs from lup's keys"}`}</pre>
  </Shell>;
}

function Checkouts({ d, state }: { d: Dashboard; state: PageState }) {
  const roots = d.roots(state);
  return <Shell d={d} title={`watching ${roots.length} checkout queues`} hint="q or Esc closes" label="Checkouts">
    {roots.map((root) => <p key={root.id} className="logl">{root.path} <span className="muted">· {root.repository_name}</span></p>)}
  </Shell>;
}

/** What `K` says of what the cursor is on. */
function hovered(d: Dashboard, state: PageState): ReactNode {
  const live = state.live;
  if (live === null) return null;
  if (state.focus === "queue" && state.view !== "history") {
    const item = d.tree(state)[state.treeCur];
    if (item?.t === "member") {
      const session = live.sessions.get(item.key);
      if (session === undefined) return null;
      return <><h4>{session.name || session.id} <span className="muted">{kindWords(session)} · {standing(session, state.now)}</span></h4><p>{session.doing || "It has not said."}</p><p className="muted">{activityBrief(session, state.now)}</p>
        {attention(live, d.roots(state), d.pending(state), session, state.now).map((flag) => <p key={flag.key} className="warn">{flag.text}</p>)}</>;
    }
    if (item?.t === "review") {
      const entry = d.entry(item.key, state);
      return entry === null ? null : <><h4>{headText(headOf(entry))}</h4><p>{entry.row.title}</p><p className="warn">{entry.row.reason}</p></>;
    }
    if (item?.t === "repo") return <><h4>{live.repositories.get(item.key)?.name}</h4><p className="muted">{live.repositories.get(item.key)?.repository}</p></>;
    return <p className="muted">Your own row: what was sent to you and your verbs.</p>;
  }
  const at = rowHere(d);
  const entry = d.current(state);
  if (at === undefined) return <p className="muted">Nothing to say about this line.</p>;
  if (entry !== null && entry.detail !== null) {
    const detail = entry.detail;
    const file = "fi" in at && at.fi !== undefined ? detail.files[at.fi] : undefined;
    switch (at.t) {
      case "line": {
        if (file === undefined) return null;
        const judged = at.jg === true ? judgedOf(detail, entry.row).find((item) => item.kind === "file" && item.fi === at.fi) : undefined;
        const comments = [...sentComments(detail), ...d.draft(entry.row.key, state).comments].filter((comment) => comment.path === file.path && comment.side === at.side && at.num !== null && at.num >= comment.start && at.num <= comment.end);
        const said = [
          judged !== undefined ? <p key="j" className="warn">? the policy asks about this: {judged.reason || entry.row.reason}</p> : null,
          at.marker !== null ? <div key="m"><p className={`an mk-${at.marker.kind}`}>{markerLabel(at.marker)} (lines {at.marker.line}–{at.marker.end_line})</p><p>{at.marker.text}</p></div> : null,
          at.exception !== null ? <div key="x"><p className="orange">rule exception: {exceptionRules(at.exception, true)} · {at.exception.introduced ? "added by this change" : "already there"}</p><p>{at.exception.reason || "No reason supplied"}</p><p className="muted">{reviewLabel(at.exception.review_effect)}: {at.exception.review_reason}</p></div> : null,
          ...comments.map((comment, index) => <p key={`c${index}`} className="info">● {"author" in comment ? comment.author : "draft"}: {comment.note || "(empty draft)"}</p>),
        ].filter((each) => each !== null);
        return <><h4>{file.path.slice(file.path.lastIndexOf("/") + 1)} · {at.kind === "add" ? "added" : at.kind === "remove" ? "removed" : "unchanged"} line{at.old !== null ? ` · before ${at.old}` : ""}{at.new !== null ? ` · after ${at.new}` : ""}</h4>
          {said.length > 0 ? said : <p className="muted">Nothing is attached to this line. i comments on it; V picks a range.</p>}</>;
      }
      case "file": case "verdict": case "note": {
        if (file === undefined) return null;
        return <><h4>{file.path}</h4><p>{file.operation} · +{file.additions} −{file.deletions} · <span className={`eff-${file.review_effect}`}>{file.review_label.words || reviewLabel(file.review_effect)}</span></p><p className="muted">as the policy put it: {file.review_reason}</p>
          {file.about !== "" && <><p className="info">the agent's note</p><p>{file.about}</p></>}</>;
      }
      case "seg": case "segwhy": return <><h4>{at.t === "seg" ? `step ${at.si + 1} · ` : ""}<span className={`eff-${at.segment.effect}`}>{reviewLabel(at.segment.effect)}</span></h4><p>{at.segment.command}</p><p className="muted">{at.segment.rule || "unattributed"}{at.segment.reason !== "" ? ` — ${at.segment.reason}` : ""}</p></>;
      case "step": return <><h4>a step no document shows</h4><p>{at.step.command}</p><p className="muted">{at.step.cause === "run" ? "Its result exists only once it runs." : "It leaves a file that does not read as text."}</p></>;
      case "cmd": return <><h4>the command, whole</h4><p className="muted">What an approval runs, exactly, in {detail.question.operation.cwd}. The marked parts are the steps that ask.</p></>;
      default: return <p className="muted">Nothing to say about this line.</p>;
    }
  }
  if (at.t === "mail") {
    const heads = mailHeads(live, at.m);
    return <><h4>{heads.from} → {heads.to}</h4><p className="muted">{stamp(at.m.sent_at)} · id {at.m.id} · byte {at.m.at.toLocaleString("en")} of the mail record</p><p>{at.m.text}</p></>;
  }
  if (at.t === "post") {
    const post = at.post;
    return <><h4>{memberName(live, post.repository, post.sender)} · {stamp(post.sent_at)}</h4>
      <p className="muted">one send, {plural(post.copies.length, "copy", "copies")}: {post.copies.map((copy) => `${memberName(live, post.repository, copy.recipient)} ${copy.waiting ? "waiting" : "taken"}`).join(", ")} · through {post.door}{post.redirect ? " · a redirect" : ""}</p>
      <p>{post.text}</p><p className="muted">r replies to it · T its author's transcript</p></>;
  }
  if (at.t === "log") return <><h4>{clock(at.at)}</h4><p>{at.text}</p></>;
  return <p className="muted">Nothing to say about this line.</p>;
}

function Hover({ d, state, top, left }: { d: Dashboard; state: PageState; top: number; left: number }) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const float = element.current;
    if (float === null || state.narrow) return;
    const height = float.offsetHeight;
    const width = float.offsetWidth;
    float.style.top = `${top + height + 8 < window.innerHeight ? top : Math.max(4, top - height - 24)}px`;
    float.style.left = `${Math.min(Math.max(4, left), window.innerWidth - width - 4)}px`;
  });
  return <div id="hover" className="float" ref={element} role="dialog" aria-label="Hover">
    {hovered(d, state)}
    <p className="muted">K or Esc closes</p>
    <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ float: null, touch: { ...state.touch, sheet: "" } })}>✕</button>
  </div>;
}

function Marked({ text, at }: { text: string; at: number[] }) {
  const hit = new Set(at);
  return <>{[...text].map((char, index) => hit.has(index) ? <em key={index}>{char}</em> : char)}</>;
}

function Finder({ d, state, picker, query, cur }: { d: Dashboard; state: PageState; picker: string; query: string; cur: number }) {
  const input = useRef<HTMLInputElement>(null);
  const results = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    d.elements.float = results.current;
    input.current?.focus({ preventScroll: true });
    return () => { d.elements.float = null; };
  }, [d]);
  const commands: FinderItem[] = COMMANDS.map((command) => ({
    text: `:${command.name} · ${command.description}${command.needs !== undefined ? " · new server work" : ""}`,
    run: (dashboard) => command.args !== undefined || command.takes === true ? openCommand(dashboard, `${command.name} `) : runCommand(dashboard, command.name),
  }));
  const shown = found(d, picker, query, commands);
  const at = Math.min(cur, Math.max(0, shown.length - 1));
  const chosen = shown[at];
  useLayoutEffect(() => { results.current?.querySelector(".fi.cur")?.scrollIntoView({ block: "nearest" }); }, [at]);
  const preview = chosen?.item.preview?.();
  return <div id="finder" className="float" role="dialog" aria-label="Find">
    <div className="fp"><span className="warn">{picker}</span><span>›</span>
      <input ref={input} aria-label="Find" autoComplete="off" spellCheck={false} value={query} onChange={(event) => d.set({ float: { kind: "finder", picker, query: event.target.value, cur: 0 } })} />
      <span className="muted">{shown.length}</span>
      <button type="button" className="fx" aria-label="Close" onClick={() => d.set({ float: null })}>✕</button></div>
    <div className="fbody">
      <div className="fres" ref={results} tabIndex={-1}>
        {shown.length === 0 ? <div className="fi muted">no match</div> : shown.slice(0, 400).map((each, index) => <div key={index} className={`fi${index === at ? " cur" : ""}`} role="option" aria-selected={index === at}
          onMouseMove={() => { if (index !== at) d.set({ float: { kind: "finder", picker, query, cur: index } }); }}
          onClick={() => pick(d, each.item)}><span className="ft"><Marked text={each.item.text} at={each.at} /></span></div>)}
      </div>
      {!state.narrow && <div className="fprev">{preview !== undefined && <><p><b>{preview.heading}</b></p>{preview.lines.map((line, index) => <p key={index} className={line.tone}>{line.text}</p>)}</>}</div>}
    </div>
    <div className="ffoot">↑↓ or Ctrl+J/K move · Enter open · Esc close{state.narrow ? " · tap to open" : ""}</div>
  </div>;
}

/** What follows the keys typed so far: each next key, the action it runs, or the group it opens. */
function WhichKey({ d }: { d: Dashboard }) {
  const typed = d.sequencer.typed;
  const next = new Map<string, { label: string; group: boolean }>();
  for (const binding of d.sequencer.continuations(d.keymap.acting("normal", d.where()))) {
    const key = binding.tokens[typed.length] ?? "";
    if (binding.tokens.length === typed.length + 1) next.set(key, { label: `${binding.bound.action.description}${binding.bound.origin !== "lup" ? " · yours" : ""}`, group: false });
    else if (!next.has(key)) next.set(key, { label: typed.length === 1 && typed[0] === "<leader>" ? LEADER_GROUPS[key] ?? "+more" : "+more", group: true });
  }
  if (next.size === 0) return null;
  const title = typed.map((key) => key === "<leader>" ? "Space" : key).join(" ");
  return <div id="whichkey" className="float" aria-label="What comes next">
    <div className="fh"><span>{title}</span><span className="grow" /><span className="muted hint">Esc cancels · Backspace goes back</span></div>
    <div className="grid">{[...next].sort(([left], [right]) => left.localeCompare(right)).map(([key, value]) => <div key={key}><span className="k">{key === "<leader>" ? "Space" : prettyKeys(key)}</span> <span className={value.group ? "g" : ""}>{value.label}</span></div>)}</div>
  </div>;
}

/** What each group under the leader holds, as which-key titles it. */
const LEADER_GROUPS: Record<string, string> = { a: "+agent (the one selected, or who asked)", p: "+you as a peer", t: "+tree filter", f: "+find", w: "+window", v: "+view", u: "+ui toggles" };

export function Floats({ d, state }: { d: Dashboard; state: PageState }) {
  const float = state.float;
  return <>
    {state.whichKey && state.pending !== "" && !state.narrow && <WhichKey d={d} />}
    {float?.kind === "help" && <Help d={d} />}
    {float?.kind === "context" && <FullContext d={d} state={state} />}
    {float?.kind === "messages" && <Messages d={d} state={state} />}
    {float?.kind === "contrast" && <Contrast d={d} />}
    {float?.kind === "keys" && <Keys d={d} />}
    {float?.kind === "checkouts" && <Checkouts d={d} state={state} />}
    {float?.kind === "hover" && <Hover d={d} state={state} top={float.top} left={float.left} />}
    {float?.kind === "finder" && <Finder d={d} state={state} picker={float.picker} query={float.query} cur={float.cur} />}
  </>;
}
