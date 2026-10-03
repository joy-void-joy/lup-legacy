// One pane of the editor: the buffer's rows, each one line of the editor with
// its two sign columns, its line numbers and its text. The first sign column
// says the change (+, −, ~), the second what is attached to the line (? the
// policy asks about it, X a rule exception, N/D/S/T a `# lup:` marker), so no
// state is told by colour alone: a line inside a conflict a merge left is
// barred by its side — ours solid, the ancestor dotted, theirs double — and
// each marker line names the side it opens or closes after it.
import { memo, useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import type { ReviewFile } from "../generated/views";
import { caretAt, columnAt, lastColumn, lineText } from "./caret";
import { standingWords } from "./conflicts";
import { semanticKey, type Dashboard } from "./dashboard";
import { askHover, codeAt, fold, pickLine, placeCursor, setCursor } from "./editor";
import { highlightedLines, languageFor, useGrammars, type Line, type Paint, type Token } from "./highlight";
import { EFFECT_SIGN, exceptionRules, MARKER_LETTER, markerLabel, plural, relative, reviewLabel, type DraftComment, type Row, type RowOf, type SentComment, type Side } from "./review";
import { clock, GLYPH, mailHeads, standing } from "./supervision";
import type { LiveState } from "./live";
import type { Peek } from "./state";
import { memberById } from "./supervision";
import { memberName } from "./threads";
import { Clamp } from "./Touch";

/** A document's lines as they were last coloured: with how many grammars settled, and over which server's paint. */
type Coloured = { settled: number; overlay: Paint[] | undefined; lines: Line[] };

const LINES = new WeakMap<ReviewFile, Partial<Record<Side, Coloured>>>();

/** How long the pointer rests on a name before its hover opens, as an editor's does. */
const POINTER_REST = 500;

/** Scrolling the view carries the cursor, as Neovim's does, so the next live update never scrolls back to where it was left. */
function followScroll(d: Dashboard, pane: 0 | 1, scroller: HTMLElement | null): void {
  const cur = scroller?.querySelector<HTMLElement>(".r.cur");
  if (scroller === null || cur === null || cur === undefined) return;
  const top = scroller.scrollTop;
  const bottom = top + scroller.clientHeight;
  if (cur.offsetTop >= top && cur.offsetTop + cur.offsetHeight <= bottom) return;
  const shown = [...scroller.querySelectorAll<HTMLElement>(".r[data-i]")].filter((row) => row.offsetTop >= top && row.offsetTop + row.offsetHeight <= bottom);
  const next = cur.offsetTop < top ? shown[0] : shown.at(-1);
  if (next !== undefined) setCursor(d, pane, Number(next.dataset.i));
}

/**
 * A file side's lines as its grammar and its language server coloured them,
 * read once per file, and again only once a grammar arrives or the server's
 * paint does.
 */
function linesFor(file: ReviewFile, side: Side, overlay: Paint[] | undefined, settled: number): Line[] {
  const held = LINES.get(file) ?? {};
  LINES.set(file, held);
  const known = held[side];
  if (known !== undefined && known.settled === settled && known.overlay === overlay) return known.lines;
  const lines = highlightedLines(file[side] ?? "", languageFor(file.path), overlay);
  held[side] = { settled, overlay, lines };
  return lines;
}

/** A text with every match of the search lit, as hlsearch lights it. */
export function lit(text: string, pattern: RegExp | null, classes = ""): ReactNode {
  if (pattern === null || text === "") return classes === "" ? text : <span className={classes}>{text}</span>;
  const parts: ReactNode[] = [];
  let at = 0;
  pattern.lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    if (match[0] === "") break;
    const index = match.index;
    if (index > at) parts.push(text.slice(at, index));
    parts.push(<mark className="sm" key={index}>{match[0]}</mark>);
    at = index + match[0].length;
  }
  if (parts.length === 0) return classes === "" ? text : <span className={classes}>{text}</span>;
  if (at < text.length) parts.push(text.slice(at));
  return classes === "" ? <>{parts}</> : <span className={classes}>{parts}</span>;
}

/** A line's tokens, each carrying the capture or the server's type that coloured it (`data-s`), so what a colour means can be read off the page. */
function Toks({ tokens, pattern }: { tokens: Token[]; pattern: RegExp | null }): ReactNode {
  return tokens.map((token, index) => <span key={index} className={token.classes === "" ? undefined : token.classes} data-s={token.capture}>{lit(token.text, pattern)}</span>);
}

const blank = <span className="sg"><b /><b /></span>;

type RowProps = {
  row: Row;
  d: Dashboard;
  pane: 0 | 1;
  cur: boolean;
  vis: boolean;
  editing: string | null;
  pattern: RegExp | null;
  files: ReviewFile[];
  target: string;
  live: LiveState | null;
  touch: boolean;
  judgedRule: string;
  /** On a phone, whether this row's long prose is opened past its four lines. */
  open: boolean;
  now: number;
  /** How many grammars have settled, so a row redraws once its own arrives. */
  settled: number;
  /** The language server's paint over the document this row's line comes from. */
  overlay: Paint[] | undefined;
  /** A line of a document `gd` opened, as coloured. */
  code: Line | undefined;
};

function Annotation({ row }: { row: RowOf<"line"> }) {
  if (row.jg === true) return <b className="an jq" title="the policy asks about this">?</b>;
  if (row.exception !== null) return <b className="an ex" title="rule exception">X</b>;
  if (row.marker !== null) return <b className={`an mk-${row.marker.kind}`} title={markerLabel(row.marker)}>{MARKER_LETTER[row.marker.kind]}</b>;
  return <b> </b>;
}

/** A line of a document `gd` opened: read-only, numbered from one, coloured as its review's lines are. */
function CodeRow({ row, cur, pattern, code }: RowProps & { row: RowOf<"code"> }) {
  return <div className={`r one ctx code${cur ? " cur" : ""}`} data-i={row.i}>
    <span className="sg"><b> </b><b> </b></span>
    <span className="ln">{row.n}</span>
    <span className="tx">{code === undefined ? lit(row.text, pattern) : <Toks tokens={code.tokens} pattern={pattern} />}</span>
  </div>;
}

function LineRow({ row, d, pane, cur, vis, pattern, files, judgedRule, overlay, settled }: RowProps & { row: RowOf<"line"> }) {
  const kind = row.kind === "add" ? "add" : row.kind === "remove" ? "del" : row.kind === "meta" ? "meta" : "ctx";
  const file = files[row.fi];
  const number = row.tokenSide === "before" ? row.old : row.new;
  const coloured = row.kind === "meta" || file === undefined || number === null ? undefined : linesFor(file, row.tokenSide, overlay, settled)[number - 1];
  const conflict = coloured?.conflict ?? null;
  const conflictClass = conflict === null ? "" : ` cf cf-${conflict.side}${conflict.marker === null ? "" : " cf-at"}`;
  const sign = row.kind === "add" ? row.chg ? <b className="dg-c" title="changed line">~</b> : <b className="dg-a" title="added line">+</b>
    : row.kind === "remove" ? <b className="dg-r" title="removed line">−</b> : <b> </b>;
  const number_ = (side: Side, line: number | null) => <span className="ln" data-side={side} onClick={(event) => {
    if (line === null) return;
    event.stopPropagation();
    d.set({ pane });
    setCursor(d, pane, row.i);
    pickLine(d, side, line, event.shiftKey, row.fi);
  }}>{line ?? ""}</span>;
  return <div className={`r ${kind}${conflictClass}${row.single ? " one" : ""}${row.jg === true ? " jg" : ""}${row.rng ? " rng" : ""}${cur ? " cur" : ""}${vis ? " vis" : ""}`} data-i={row.i}>
    <span className="sg">{sign}<Annotation row={row} /></span>
    {row.single ? number_(row.side, row.num) : <>{number_("before", row.old)}{number_("after", row.new)}</>}
    <span className="tx">{coloured === undefined ? lit(row.text, pattern) : <Toks tokens={coloured.tokens} pattern={pattern} />}
      {conflict !== null && conflict.marker !== null && <span className={`vt cfs-${conflict.side}`}>◂ {standingWords(conflict)}</span>}
      {row.jgFirst && <span className="vt jq">◂ the policy asks about this{judgedRule !== "" ? ` · ${judgedRule}` : ""}</span>}
      {row.exception !== null && <span className="vt ex">◂ exception · {exceptionRules(row.exception)}{row.exception.introduced ? " · added here" : ""}</span>}
      {row.markerStart && row.marker !== null && row.exception === null && <span className={`vt mk-${row.marker.kind}`}>◂ {markerLabel(row.marker)}</span>}
    </span>
  </div>;
}

function CommentBox({ row, d, editing, cur }: RowProps & { row: RowOf<"cm"> }) {
  const comment = row.comment;
  const span = comment.start === comment.end ? `line ${comment.start}` : `lines ${comment.start}–${comment.end}`;
  const side = comment.side === "before" ? " (before the change)" : "";
  const box = useRef<HTMLTextAreaElement>(null);
  const draft = row.draft ? comment as DraftComment : null;
  const writing = draft !== null && editing === draft.id;
  useEffect(() => {
    const element = box.current;
    if (!writing || element === null) return;
    element.focus({ preventScroll: true });
    element.setSelectionRange(element.value.length, element.value.length);
    if (document.activeElement === element && !d.state.typing) d.set({ typing: true });
  }, [writing, d]);
  if (draft === null) {
    const sent = comment as SentComment;
    return <div className={`r cm${row.single ? " one" : ""}${cur ? " cur" : ""}`} data-i={row.i}>
      <span className="sg"><b /><b className="an cm">●</b></span>
      <div className="cmbox"><span className="h">● {sent.author} · {span}{side} · sent {clock(sent.at)}</span><p>{comment.note}</p></div>
    </div>;
  }
  const open = d.current();
  return <div className={`r cm${row.single ? " one" : ""}${cur ? " cur" : ""}`} data-i={row.i}>
    <span className="sg"><b /><b className="an dr">○</b></span>
    <div className="cmbox draft"><span className="h">○ draft · {span}{side}</span>
      {writing ? <>
        <textarea ref={box} className="cmedit" data-comment={draft.id} aria-label={`Comment on ${span}`} value={draft.note}
          placeholder={`Comment on ${span}; it goes with your decision (Ctrl+Enter approves), or alone with Alt+Enter. Esc leaves.`}
          onChange={(event) => { if (open !== null) { const note = event.target.value; d.setDraft(open.row.key, (now) => ({ comments: now.comments.map((each) => each.id === draft.id ? { ...each, note } : each) })); } }} />
        <span className="muted">Esc done · x on this line deletes · u brings it back · Alt+Enter sends it without deciding</span>
      </> : <>
        <p>{draft.note !== "" ? draft.note : <span className="muted">empty; it is dropped when you leave it</span>}</p>
        <span className="muted">i or Enter edits · x deletes</span>
      </>}
    </div>
  </div>;
}

function FileHeader({ row, d, pane, cur, target, pattern }: RowProps & { row: RowOf<"file"> }) {
  const file = row.file;
  const op = ({ modify: "M", create: "A", delete: "D", overwrite: "R" } as Record<string, string>)[file.operation] ?? "M";
  const extra = [row.exceptions > 0 ? plural(row.exceptions, "exception") : "", row.comments > 0 ? plural(row.comments, "draft comment") : ""].filter((each) => each !== "").join(" · ");
  const held = row.holders.length > 0;
  return <div className={`r full fh${row.jg === true ? " jg" : ""}${cur ? " cur" : ""}`} data-i={row.i} onClick={() => { setCursor(d, pane, row.i); fold(d, "toggle"); }}>
    <span className="sg"><b>{row.open ? "▾" : "▸"}</b>{held ? <b className="an hold" title="held">H</b> : <b className={`eff-${file.review_effect}`}>{EFFECT_SIGN[file.review_effect] ?? "·"}</b>}</span>
    <span className="tx">{op} {lit(relative(file.path, target), pattern)} <span className="addn">+{file.additions}</span> <span className="deln">−{file.deletions}</span> <span className={`why eff-${file.review_effect}`} title={file.review_reason}>{file.review_label.words || reviewLabel(file.review_effect)}</span>
      {extra !== "" && <span className="muted"> · {extra}</span>}
      {!row.open && <span className="muted"> · folded{row.needs ? "" : ", needs no reading"} · za opens</span>}
      {held && <span className="vt hold">◂ held by {row.holders.map((each) => `${each.name}${each.lock ? " (locked)" : ""}`).join(", ")}{row.holders.length > 1 ? " · contested" : ""}</span>}
    </span>
  </div>;
}

function Mail({ row, d, live, cur, pattern, touch, open }: RowProps & { row: RowOf<"mail"> }) {
  const message = row.m;
  if (live === null) return null;
  const heads = mailHeads(live, message);
  const state = message.prompt ? "the prompt its runtime was woken with, never in its mailbox" : message.recipient === "user" ? (message.waiting ? "unread" : "read") : message.waiting ? "waiting in its mailbox" : "taken";
  const flags = [message.redirect ? "redirect" : "", message.in_reply_to !== "" ? "reply" : ""].filter((each) => each !== "").join(" · ");
  return <div className={`r full mail${message.prompt ? " prompt" : message.sender === "user" ? " from-user" : ""}${row.unread ? " unread" : ""}${cur ? " cur" : ""}`} data-i={row.i}>
    <span className="sg"><b className={row.unread ? "warn" : "muted"}>{row.unread ? "●" : message.prompt ? "▶" : message.recipient === "user" ? "○" : ""}</b><b /></span>
    <span className="tx"><span className="mh"><b>{heads.from} → {heads.to}</b> · {clock(message.sent_at)} · {state} · through {message.door}{flags !== "" ? ` · ${flags}` : ""}</span>{"\n"}<Clamp d={d} narrow={touch} open={open} id={row.key}>{lit(message.text, pattern)}</Clamp></span>
  </div>;
}

/**
 * One post of a discussion: its author with their standing, each recipient
 * marked ✓ taken, ◷ waiting or (unread) for the operator, when, the post it
 * answers, and its text whole.
 */
function PostRow({ row, d, live, cur, pattern, touch, open, now }: RowProps & { row: RowOf<"post"> }) {
  if (live === null) return null;
  const post = row.post;
  const author = post.prompt ? "prompt" : memberName(live, post.repository, post.sender);
  const session = memberById(live, post.repository, post.sender);
  const held = session === undefined ? null : standing(session, now);
  const mark = (recipient: string, waiting: boolean) => recipient === "user" ? (waiting ? " (unread)" : "") : waiting ? " ◷" : " ✓";
  return <div className={`r full post${post.sender === "user" ? " from-user" : ""}${row.unread ? " unread" : ""}${cur ? " cur" : ""}`} data-i={row.i}>
    <span className="sg"><b className={row.unread ? "warn" : "muted"}>{row.unread ? "●" : ""}</b><b /></span>
    <span className="tx"><span className="ph">{post.prompt ? <span className="muted" title="the prompt its runtime was woken with">▶ </span> : held !== null ? <span className={`g-${held}`} title={held}>{GLYPH[held]} </span> : post.sender === "user" ? <span className="info">◆ </span> : null}
      <b>{author}</b> → {post.copies.map((copy, at) => <span key={copy.id}>{at > 0 ? ", " : ""}{memberName(live, post.repository, copy.recipient)}<span className="muted" title={copy.waiting ? "waiting in its mailbox" : "taken"}>{mark(copy.recipient, copy.waiting)}</span></span>)}
      {" "}· {clock(post.sent_at)}{post.redirect && <> · <span className="warn">redirect</span></>}</span>
      {row.answered !== null && <>{"\n"}<span className="quote">↩ {memberName(live, post.repository, row.answered.sender)}: {row.answered.text.split("\n")[0]}</span></>}
      {"\n"}<Clamp d={d} narrow={touch} open={open} id={row.key}>{lit(post.text, pattern)}</Clamp></span>
  </div>;
}

function marks(row: RowOf<"cmd">, pattern: RegExp | null): ReactNode {
  const parts: ReactNode[] = [];
  let at = 0;
  for (const [start, end] of row.marks) {
    parts.push(<span key={`t${start}`}>{lit(row.text.slice(at, start), pattern)}</span>);
    parts.push(<mark className="jgm" key={`m${start}`}>{lit(row.text.slice(start, end), pattern)}</mark>);
    at = end;
  }
  parts.push(<span key="rest">{lit(row.text.slice(at), pattern)}</span>);
  return row.text === "" ? " " : parts;
}

/** One row, drawn by its kind. */
const RowView = memo(function RowView(props: RowProps) {
  const { row, d, pane, cur, pattern } = props;
  const cls = (extra: string) => `r ${extra}${row.jg === true ? " jg" : ""}${cur ? " cur" : ""}`;
  switch (row.t) {
    case "line": return <LineRow {...props} row={row} />;
    case "cm": return <CommentBox {...props} row={row} />;
    case "file": return <FileHeader {...props} row={row} />;
    case "mail": return <Mail {...props} row={row} />;
    case "verdict": return <div className={cls("full vd")} data-i={row.i}><span className="sg"><b /><b className="an jq">?</b></span><span className="tx">{reviewLabel(row.file.review_effect).toLowerCase()}: {lit(row.file.review_reason, pattern)}</span></div>;
    case "note": return <div className={cls("full note")} data-i={row.i}>{blank}<span className="tx"><span className="lab">the agent's note</span><Clamp d={d} narrow={props.touch} open={props.open} id={row.key}>{lit(row.text, pattern)}</Clamp></span></div>;
    case "hunk": return <div className={cls("full hk")} data-i={row.i}>{blank}<span className="tx">{lit(row.text, pattern)}</span></div>;
    case "gap": return <div className={cls("full gap")} data-i={row.i} onClick={() => { setCursor(d, pane, row.i); fold(d, "toggle"); }}><span className="sg"><b>┈</b><b /></span>
      <span className="tx">┈┈ {plural(row.count, "unchanged line")} ┈┈ {props.touch ? `tap to show ${row.count === 1 ? "it" : "them"}` : `Enter or za shows ${row.count === 1 ? "it" : "them"}, f the whole file`}</span></div>;
    case "raw": return <div className={cls("full raw")} data-i={row.i}>{blank}<span className="tx">{lit(row.text, pattern)}</span></div>;
    case "json": return <div className={cls("full json")} data-i={row.i}>{blank}<span className="tx">{lit(row.text.replaceAll("\\n", "\\n\n"), pattern)}</span></div>;
    case "msg": return <div className={cls("full msg")} data-i={row.i}>{blank}<span className={`tx ${row.tone}`}>{lit(row.text, pattern)}</span></div>;
    case "sec": return <div className={cls("full sec")} data-i={row.i}><span className="sg"><b>▾</b><b /></span><span className="tx">{lit(row.text, pattern)}{row.sub !== "" && <span className="muted"> {row.sub}</span>}</span></div>;
    case "cmd": return <div className={cls("full cmd")} data-i={row.i}><span className="sg"><b />{row.jg === true ? <b className="an jq">?</b> : <b />}</span><span className="tx">{marks(row, pattern)}</span></div>;
    case "seg": return <div className={cls("full seg")} data-i={row.i}><span className="sg"><b />{row.allowed ? <b className="eff-allow">✓</b> : <b className="an jq">{row.segment.effect === "deny" ? "✗" : "?"}</b>}</span>
      <span className="tx"><span className="sn">step {row.si + 1}/{row.total}</span> {lit(row.segment.command || "the line as a whole", pattern)}{row.allowed && row.segment.rule !== "" && <span className="muted"> · {row.segment.rule}</span>}</span></div>;
    case "segwhy": return <div className={cls("full segwhy")} data-i={row.i}>{blank}<span className="tx">{row.segment.effect === "deny" ? "refused" : "asks"} · {row.segment.rule || "unattributed"} — {lit(row.segment.reason, pattern)}</span></div>;
    case "fold": return <div className={cls("full fold")} data-i={row.i} onClick={() => { setCursor(d, pane, row.i); fold(d, "toggle"); }}><span className="sg"><b>{row.open ? "▾" : "▸"}</b><b className="eff-allow">✓</b></span><span className="tx">{row.text} · {row.open ? "za folds" : "za or Enter opens"}</span></div>;
    case "step": return <div className={cls("full step")} data-i={row.i}><span className="sg"><b /><b className="muted">◷</b></span><span className="tx">{lit(row.step.command, pattern)}  <span className="muted">{stepWords(row)}</span></span></div>;
    case "kv": return <div className={cls("full kv")} data-i={row.i}>{blank}<span className="tx"><span className="k">{row.k}</span>{lit(row.v, pattern)}</span></div>;
    case "said": return <div className={cls("full said")} data-i={row.i}>{blank}<span className="tx"><Clamp d={d} narrow={props.touch} open={props.open} id={row.key}>{lit(row.text, pattern)}</Clamp></span></div>;
    case "post": return <PostRow {...props} row={row} />;
    case "code": return <CodeRow {...props} row={row} />;
    case "log": return <div className={cls("full tl")} data-i={row.i}>{blank}<span className="tx"><span className="muted">{clock(row.at)}</span> {lit(row.text, pattern)}</span></div>;
    case "earlier": return <div className={cls("full fold")} data-i={row.i} onClick={() => void d.loadEarlier(row.repository)}><span className="sg"><b>▸</b><b /></span>
      <span className="tx">Load earlier messages · E or Enter <span className="muted">(an older page of the mail record, before byte {row.before.toLocaleString("en")})</span></span></div>;
    case "verb": return <div className={cls("full verb")} data-i={row.i}>{blank}<span className="tx"><span className="k">{row.command}</span>{row.what} <i className={`srv ${row.server.startsWith("today") ? "today" : "new"}`}>{row.server}</i></span></div>;
  }
});

/** What a step no document shows says of itself: quietly, what is known of its effect, and its own verdict where one was recorded. */
export function stepWords(row: RowOf<"step">): string {
  const effect = row.step.cause === "run" ? "effect shown only after it runs" : "leaves a file that is not text";
  const verdict = row.verdict === "allow" ? " · allowed on its own" : row.verdict === "ask" ? " · this step asks" : row.verdict === "deny" ? " · this step is refused" : "";
  return `${effect}${verdict}`;
}

/** The lines of a visual range, for the rows it lights. */
function inRange(row: Row, span: { fi: number; side: Side; start: number; end: number } | null): boolean {
  if (span === null || row.t !== "line" || row.fi !== span.fi) return false;
  const number = span.side === "before" ? row.old : row.new;
  return number !== null && number >= span.start && number <= span.end;
}

/**
 * Draw the caret on the character under the cursor of the pane with focus, as
 * a block over it; nothing while the buffer lacks focus. The statusline's
 * column is read back from the same line the caret stands on.
 */
function useCaret(d: Dashboard, pane: 0 | 1, element: React.RefObject<HTMLDivElement | null>, caret: React.RefObject<HTMLSpanElement | null>, shown: boolean, cur: number, want: number, rows: Row[]): void {
  useLayoutEffect(() => {
    const scroller = element.current;
    const block = caret.current;
    if (scroller === null || block === null) return;
    const place = () => {
      const row = scroller.querySelector(`.r[data-i="${cur}"]`);
      const column = Math.min(want, lastColumn(lineText(row)));
      const at = shown && row !== null ? caretAt(row, column) : null;
      if (at === null) { block.hidden = true; return; }
      const frame = scroller.getBoundingClientRect();
      block.hidden = false;
      block.textContent = at.char;
      block.style.font = at.font;
      block.style.top = `${at.rect.top - frame.top + scroller.scrollTop - scroller.clientTop}px`;
      block.style.left = `${at.rect.left - frame.left + scroller.scrollLeft - scroller.clientLeft}px`;
      block.style.height = `${at.rect.height}px`;
      block.style.minWidth = `${Math.max(at.rect.width, 2)}px`;
      if (shown && d.state.column !== column) d.set({ column });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [d, pane, element, caret, shown, cur, want, rows]);
}

/**
 * Ask the language server about every document the window draws, once each:
 * both sides of each of the review's files, or the document `gd` opened.
 */
function useSemantic(d: Dashboard, review: string, checkout: string, files: ReviewFile[], peek: Peek | null): void {
  useEffect(() => {
    if (peek !== null) { d.askSemantic(peek.source, peek.text); return; }
    if (review === "") return;
    for (const file of files) {
      for (const side of ["after", "before"] as const) {
        const text = file[side];
        if (text !== null) d.askSemantic({ review, side, checkout, path: file.path }, text);
      }
    }
  }, [d, review, checkout, files, peek]);
}

/**
 * Rest the pointer on a name in code and its hover opens beside it, as an
 * editor's does; moving off the name closes a hover the pointer opened. On a
 * touch screen nothing hovers: a long press asks instead.
 */
function usePointerHover(d: Dashboard, pane: 0 | 1, touch: boolean) {
  const resting = useRef<{ timer: ReturnType<typeof setTimeout> | undefined; on: string }>({ timer: undefined, on: "" });
  useEffect(() => () => clearTimeout(resting.current.timer), []);
  return {
    move: (event: React.MouseEvent) => {
      if (touch) return;
      const now = resting.current;
      clearTimeout(now.timer);
      const target = event.target instanceof Element ? event.target : null;
      const hit = target?.closest<HTMLElement>(".r") ?? null;
      const at = hit === null || target?.closest(".tx") === null ? null : codeAt(d, pane, Number(hit.dataset.i), columnAt(hit, event.clientX, event.clientY));
      const on = at === null || at.name === "" ? "" : `${hit?.dataset.i ?? ""}:${at.name}`;
      const float = d.state.float;
      if (on !== now.on && float?.kind === "hover" && float.code?.pointer === true) d.set({ float: null });
      now.on = on;
      if (at === null || on === "") return;
      const top = event.clientY + 16;
      const left = event.clientX;
      now.timer = setTimeout(() => {
        const open = d.state.float;
        if (open === null || (open.kind === "hover" && open.code?.pointer === true)) askHover(d, at, top, left, true);
      }, POINTER_REST);
    },
    leave: () => { clearTimeout(resting.current.timer); resting.current.on = ""; },
  };
}

export function BufferView({ d, pane, rows, cur, want, active, focused, editing, pattern, span, files, target, live, touch, judgedRule, title, numberWidth, unclamped, now, review, checkout, peek, semantic }: {
  d: Dashboard;
  pane: 0 | 1;
  rows: Row[];
  cur: number;
  want: number;
  active: boolean;
  /** Whether keys act in this pane: it is the active one and the buffer has focus. */
  focused: boolean;
  editing: string | null;
  pattern: RegExp | null;
  span: { fi: number; side: Side; start: number; end: number } | null;
  files: ReviewFile[];
  target: string;
  live: LiveState | null;
  touch: boolean;
  judgedRule: (fi: number) => string;
  title: string;
  numberWidth: string;
  unclamped: ReadonlySet<string>;
  now: number;
  /** The open review's key and the checkout it changes, where the window shows one. */
  review: string;
  checkout: string;
  /** The document `gd` opened in this window in place of the review. */
  peek: Peek | null;
  /** Each document's paint from its language server, by its key. */
  semantic: ReadonlyMap<string, Paint[]>;
}) {
  const element = useRef<HTMLDivElement>(null);
  const caret = useRef<HTMLSpanElement>(null);
  const following = useRef(0);
  const settled = useGrammars(peek !== null ? [languageFor(peek.source.path)] : files.map((file) => languageFor(file.path)));
  useSemantic(d, review, checkout, files, peek);
  const pointer = usePointerHover(d, pane, touch);
  const peekOverlay = peek === null ? undefined : semantic.get(semanticKey(peek.source));
  const peekLines = useMemo(() => peek === null ? [] : highlightedLines(peek.text, languageFor(peek.source.path), peekOverlay), [peek, peekOverlay, settled]);
  const overlayOf = (row: Row) => row.t !== "line" ? undefined : (() => {
    const file = files[row.fi];
    return file === undefined ? undefined : semantic.get(semanticKey({ review, side: row.tokenSide, checkout, path: file.path }));
  })();
  const heading = peek === null ? title : `${peek.name} · ${relative(peek.source.path, checkout)}:${peek.line} · ${peek.source.review === "" ? "as it stands" : `the review's ${peek.source.side}`} · read-only`;
  useLayoutEffect(() => {
    d.elements.panes[pane] = element.current;
    return () => { d.elements.panes[pane] = null; };
  }, [d, pane]);
  useCaret(d, pane, element, caret, focused, cur, want, rows);
  useLayoutEffect(() => {
    const scroller = element.current;
    const row = scroller?.querySelector<HTMLElement>(".r.cur");
    if (scroller === null || row === null || row === undefined) return;
    const how = d.scroll[pane];
    d.scroll[pane] = "nearest";
    if (how === "top") {
      const lead = 2 * (Number.parseFloat(getComputedStyle(scroller).lineHeight) || 18);
      scroller.scrollTop = Math.max(0, row.offsetTop - lead);
    } else row.scrollIntoView({ block: how, inline: "nearest" });
  }, [cur, rows, d, pane]);
  return <div className={`buf pane${active ? " active" : ""}`} ref={element} tabIndex={-1} role="region" aria-label={pane === 0 ? "Buffer" : "Second window"} data-pane={pane}
    style={{ ["--lnw" as string]: numberWidth }}
    onScroll={() => { cancelAnimationFrame(following.current); following.current = requestAnimationFrame(() => { followScroll(d, pane, element.current); }); }}
    onMouseMove={pointer.move} onMouseLeave={pointer.leave}
    onClick={(event) => {
      const hit = event.target instanceof Element ? event.target.closest<HTMLElement>(".r") : null;
      if (hit === null || (event.target instanceof Element && event.target.closest("textarea, button") !== null)) return;
      // A click puts the cursor on the character clicked, and focus in the buffer.
      d.set({ pane });
      if (d.state.focus !== "editor") d.focusWin("editor");
      placeCursor(d, pane, Number(hit.dataset.i), columnAt(hit, event.clientX, event.clientY));
    }}>
    {heading !== "" && <div className={`pane-title${peek !== null ? " peek" : ""}`}>{heading} <span className="muted">· {peek !== null ? `${d.keymap.spoken("jump.back")} or q comes back` : "Space v changes what this window shows"}</span></div>}
    {rows.map((row) => <RowView key={row.key} row={row} d={d} pane={pane} cur={row.i === cur} vis={inRange(row, span)} editing={row.t === "cm" ? editing : null} pattern={pattern}
      files={files} target={target} live={row.t === "mail" || row.t === "post" ? live : null} touch={touch} judgedRule={"fi" in row && row.fi !== undefined ? judgedRule(row.fi) : ""}
      open={unclamped.has(row.key)} now={row.t === "post" ? now : 0} settled={row.t === "line" || row.t === "code" ? settled : 0}
      overlay={overlayOf(row)} code={row.t === "code" ? peekLines[row.n - 1] : undefined} />)}
    <span className="caret" ref={caret} aria-hidden="true" hidden />
  </div>;
}
