// The editor: one file of a review at a time, as a coloured diff with its
// line comments, its `# lup:` markers and its rule exceptions marked where
// they stand, the whole file a keystroke away.
import { useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from "react";
import type { LineComment, ReviewFile, ReviewLine, ReviewMarker } from "../generated/views";
import { highlightedLines, languageFor, Tokens, type Token } from "./highlight";

export type FileNavigation = {
  moveFile(offset: number): void;
  moveException(offset: number): void;
  moveMarker(offset: number): void;
  toggleWhole(): void;
};

/** A line comment being written, which keeps an id of its own until it is sent. */
export type DraftComment = LineComment & { id: string };

type Evidence = "diff" | "before" | "after" | "raw";
type Side = "before" | "after";
type Jump = { side: Side; line: number; revision: number };
type DiffRow =
  | { kind: "hunk"; key: string; header: string }
  | { kind: "gap"; key: string; count: number }
  | { kind: "line"; key: string; line: ReviewLine };

let drafted = 0;
/** A fresh id for a comment drafted on this page. */
function draftId(): string {
  drafted += 1;
  return `draft-${Date.now()}-${drafted}`;
}

function commonDirectory(files: ReviewFile[]): string {
  const addresses = files.map((file) => { const url = new URL("file:///"); url.pathname = encodeURI(file.path); return url; });
  const first = addresses[0];
  if (first === undefined) return "";
  for (let directory = new URL(".", first);; directory = new URL("..", directory)) {
    const prefix = decodeURIComponent(directory.pathname);
    if (addresses.every((address) => address.pathname.startsWith(directory.pathname)) && files.every((file) => file.path.startsWith(prefix))) return prefix;
    if (directory.pathname === "/") return "";
  }
}

function reviewLabel(effect: ReviewFile["review_effect"]): string {
  switch (effect) {
    case "allow": return "Automatic";
    case "defer": return "Native decision";
    case "ask": return "Needs approval";
    case "deny": return "Blocked";
    default: return "Unclassified";
  }
}

/** What a marker is called on the page, by its kind. */
export function markerLabel(marker: ReviewMarker): string {
  switch (marker.kind) {
    case "defer": return marker.condition === null ? "deferred" : `deferred[${marker.condition}]`;
    case "solved": return "solved";
    case "template": return "template";
    case "ignore": return "exception";
    default: return "open note";
  }
}

/** How many lines a document has, a trailing newline ending the last rather than opening another. */
function lineCount(text: string | null): number {
  if (text === null || text === "") return 0;
  const parts = text.split("\n").length;
  return text.endsWith("\n") ? parts - 1 : parts;
}

/**
 * The diff as rows: each hunk, and between and around them the unchanged
 * lines it leaves out -- folded into one expander each, or shown in place
 * where the operator opened that gap or asked for the whole file. Unchanged
 * lines read the same in both documents, so a gap's text is the after
 * document's, numbered on both sides from where the hunk before it ended.
 */
function diffRows(file: ReviewFile, whole: boolean, opened: ReadonlySet<string>): DiffRow[] {
  const after = (file.after ?? "").split("\n");
  const rows: DiffRow[] = [];
  let nextOld = 1;
  let nextNew = 1;
  function gap(count: number, key: string) {
    if (count <= 0) return;
    if (!whole && !opened.has(key)) {
      rows.push({ kind: "gap", key, count });
      return;
    }
    for (let offset = 0; offset < count; offset += 1) {
      rows.push({ kind: "line", key: `${key}:${offset}`, line: {
        kind: "context", text: `${after[nextNew - 1 + offset] ?? ""}\n`,
        old_line: nextOld + offset, new_line: nextNew + offset, suppression: false,
      } });
    }
  }
  file.hunks.forEach((hunk, index) => {
    gap(hunk.new_start - nextNew, `gap-${index}`);
    if (!whole) rows.push({ kind: "hunk", key: `hunk-${index}`, header: hunk.header });
    hunk.lines.forEach((line, at) => rows.push({ kind: "line", key: `${index}:${at}`, line }));
    nextOld = hunk.old_end + 1;
    nextNew = hunk.new_end + 1;
  });
  gap(lineCount(file.after) - nextNew + 1, "gap-end");
  return rows;
}

/** Which side and line each of a diff row's numbers stands for, the side a comment on it lands on first. */
function rowSides(line: ReviewLine): { side: Side; line: number }[] {
  switch (line.kind) {
    case "remove": return line.old_line === null ? [] : [{ side: "before", line: line.old_line }];
    case "add": return line.new_line === null ? [] : [{ side: "after", line: line.new_line }];
    case "context": return [
      ...(line.new_line === null ? [] : [{ side: "after" as const, line: line.new_line }]),
      ...(line.old_line === null ? [] : [{ side: "before" as const, line: line.old_line }]),
    ];
    default: return [];
  }
}

/** Every line one marker covers, so each is marked where it stands. */
function markedLines(markers: ReviewMarker[], side: Side): Map<number, ReviewMarker> {
  return new Map(markers.filter((marker) => marker.side === side)
    .flatMap((marker) => Array.from({ length: marker.end_line - marker.line + 1 }, (_, offset) => [marker.line + offset, marker] as const)));
}

export function Files({ files, navigation, command, comments, recorded, editable, target, onComments }: {
  files: ReviewFile[];
  navigation: Ref<FileNavigation>;
  command: string | null;
  comments: DraftComment[];
  recorded: LineComment[];
  editable: boolean;
  target: string;
  onComments(comments: DraftComment[]): void;
}) {
  const [full, setFull] = useState(false);
  const [selected, setSelected] = useState(files[0]?.path ?? "");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"files" | "exceptions">("files");
  const [view, setView] = useState<Evidence>("diff");
  const [whole, setWhole] = useState(false);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const [mobile, setMobile] = useState<"navigator" | "evidence">("evidence");
  const [occurrence, setOccurrence] = useState("");
  const [markerAt, setMarkerAt] = useState(-1);
  const [why, setWhy] = useState(false);
  const [jump, setJump] = useState<Jump | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<{ path: string; side: Side; line: number } | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const lines = useRef(new Map<string, HTMLElement>());
  const required = files.filter((item) => item.review_effect !== "allow" && item.review_effect !== "defer");
  const scoped = full ? files : required;
  const visible = scoped.filter((item) => item.path.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const file = scoped.find((item) => item.path === selected) ?? scoped[0];
  const additions = visible.reduce((total, item) => total + item.additions, 0);
  const deletions = visible.reduce((total, item) => total + item.deletions, 0);
  const exceptionsFor = (item: ReviewFile) => item.suppressions.filter((suppression) => full || (suppression.review_effect !== "allow" && suppression.review_effect !== "defer"));
  const suppressions = visible.flatMap((item) => exceptionsFor(item).map((suppression) => ({ file: item, suppression, key: `${item.path}:${suppression.line}` })));
  const marked = new Set(file === undefined ? [] : exceptionsFor(file).map((suppression) => suppression.line));
  const groups = new Map<string, typeof suppressions>();
  for (const item of suppressions) {
    const ids = full ? item.suppression.rule_ids : item.suppression.review_rule_ids;
    const rules = ids === null ? ["Unscoped directive"] : ids.length === 0 ? ["No rule IDs"] : ids;
    for (const rule of rules) groups.set(rule, [...(groups.get(rule) ?? []), item]);
  }
  const removed = (item: ReviewFile) => new Set(item.hunks.flatMap((hunk) => hunk.lines.flatMap((line) => line.kind === "remove" && line.old_line !== null ? [line.old_line] : [])));
  // A marker on an unchanged line stands on both sides; it is stopped at once, on the after side.
  const stops = visible.flatMap((item) => {
    const gone = removed(item);
    return item.markers.filter((marker) => marker.side === "after" || gone.has(marker.line)).map((marker) => ({ file: item, marker }));
  });
  const fileIndex = visible.findIndex((item) => item.path === file?.path);
  const exceptionIndex = suppressions.findIndex((item) => item.key === occurrence);
  const introduced = suppressions.filter((item) => item.suppression.introduced).length;
  const directory = commonDirectory(scoped);
  const label = (path: string) => path.startsWith(`${target}/`) ? path.slice(target.length + 1) : path.slice(directory.length) || path;
  const language = file === undefined ? null : languageFor(file.path);
  const before = useMemo(() => highlightedLines(file?.before ?? "", language), [file?.path, file?.before, language]);
  const after = useMemo(() => highlightedLines(file?.after ?? "", language), [file?.path, file?.after, language]);
  const markers = useMemo(() => ({ before: markedLines(file?.markers ?? [], "before"), after: markedLines(file?.markers ?? [], "after") }), [file]);
  const rows = useMemo(() => file === undefined ? [] : diffRows(file, whole, opened), [file, whole, opened]);
  const drafts = comments.filter((comment) => comment.path === file?.path);
  const sent = recorded.filter((comment) => comment.path === file?.path);
  const active = drafts.find((comment) => comment.id === editing) ?? null;

  function showFull(value: boolean) {
    setFull(value);
    setQuery("");
    setOccurrence("");
    setView("diff");
    setJump(null);
  }

  function select(path: string) {
    setSelected(path);
    setView("diff");
    setOpened(new Set());
    setJump(null);
    setMobile("evidence");
  }

  function moveFile(offset: number) {
    const found = visible[fileIndex < 0 ? offset > 0 ? 0 : visible.length - 1 : fileIndex + offset];
    if (found !== undefined) select(found.path);
  }

  /** Show one line of one file where it stands: in the diff, or in the whole file where no hunk holds it. */
  function reveal(item: ReviewFile, side: Side, line: number) {
    const shown = item.hunks.some((hunk) => hunk.lines.some((row) => side === "after" ? row.new_line === line : row.old_line === line));
    setSelected(item.path);
    setView("diff");
    if (!shown) setWhole(true);
    setMobile("evidence");
    setJump((previous) => ({ side, line, revision: (previous?.revision ?? 0) + 1 }));
  }

  function moveException(offset: number) {
    const index = exceptionIndex < 0 ? offset > 0 ? 0 : suppressions.length - 1 : exceptionIndex + offset;
    const found = suppressions[index];
    if (found === undefined) return;
    setOccurrence(found.key);
    reveal(found.file, "after", found.suppression.line);
  }

  function moveMarker(offset: number) {
    const index = markerAt < 0 ? offset > 0 ? 0 : stops.length - 1 : markerAt + offset;
    const found = stops[index];
    if (found === undefined) return;
    setMarkerAt(index);
    reveal(found.file, found.marker.side, found.marker.line);
  }

  /** Open a comment on the lines the operator picked: one line, or a range from the last one picked. */
  function pick(side: Side, line: number, extend: boolean) {
    if (!editable || file === undefined) return;
    const from = extend && anchor !== null && anchor.path === file.path && anchor.side === side ? anchor.line : line;
    const start = Math.min(from, line);
    const end = Math.max(from, line);
    if (!extend) setAnchor({ path: file.path, side, line });
    const same = drafts.find((comment) => comment.side === side && comment.start === start && comment.end === end);
    if (same !== undefined) {
      setEditing(same.id);
      return;
    }
    if (extend && active !== null && active.side === side) {
      onComments(comments.map((comment) => comment.id === active.id ? { ...comment, start, end } : comment));
      return;
    }
    const id = draftId();
    const kept = comments.filter((comment) => comment.id !== editing || comment.note.trim() !== "");
    onComments([...kept, { id, path: file.path, side, start, end, note: "" }]);
    setEditing(id);
  }

  function closeEditor() {
    if (active !== null && active.note.trim() === "") onComments(comments.filter((comment) => comment.id !== active.id));
    setEditing(null);
    viewport.current?.focus({ preventScroll: true });
  }

  useImperativeHandle(navigation, () => ({ moveFile, moveException, moveMarker, toggleWhole: () => setWhole((value) => !value) }));
  useEffect(() => {
    if (jump !== null) {
      const found = lines.current.get(`${view === "before" ? "before" : view === "after" ? "after" : jump.side}:${jump.line}`);
      found?.scrollIntoView({ block: "center", inline: "nearest" });
      found?.focus({ preventScroll: true });
    } else {
      viewport.current?.scrollTo({ top: 0, left: 0 });
    }
  }, [file?.path, view, jump, whole]);

  function registered(side: Side, line: number | null) {
    return (node: HTMLElement | null) => {
      if (line === null) return;
      const key = `${side}:${line}`;
      if (node === null) lines.current.delete(key);
      else lines.current.set(key, node);
    };
  }

  function commentRows(side: Side, line: number, columns: number) {
    const here = [...sent.filter((comment) => comment.side === side && comment.end === line).map((comment) => ({ comment, draft: null })),
      ...drafts.filter((comment) => comment.side === side && comment.end === line).map((comment) => ({ comment, draft: comment }))];
    return here.map(({ comment, draft }, index) => <tr className={`comment-row${draft === null ? " recorded" : " drafted"}`} key={draft?.id ?? `sent-${side}-${line}-${index}`}>
      <td colSpan={columns}><div className="line-comment">
        <span className="line-comment-anchor">{comment.start === comment.end ? `Line ${comment.start}` : `Lines ${comment.start}–${comment.end}`}{comment.side === "before" ? " (before)" : ""}{draft === null ? " · sent" : " · draft"}</span>
        {draft !== null && draft.id === editing ? <>
          <textarea className="line-comment-editor" aria-label={`Comment on ${comment.start === comment.end ? `line ${comment.start}` : `lines ${comment.start} to ${comment.end}`}`} autoFocus rows={2} value={draft.note}
            onChange={(event) => onComments(comments.map((each) => each.id === draft.id ? { ...each, note: event.target.value } : each))}
            onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); closeEditor(); } }}
            placeholder="Comment on these lines; it is sent with your decision or with Alt+Enter" />
          <span className="line-comment-actions"><button type="button" onClick={closeEditor}>Done</button>
            <button type="button" onClick={() => { onComments(comments.filter((each) => each.id !== draft.id)); setEditing(null); }}>Delete</button></span>
        </> : <>
          <p>{comment.note}</p>
          {draft !== null && editable && <span className="line-comment-actions"><button type="button" onClick={() => setEditing(draft.id)}>Edit</button>
            <button type="button" onClick={() => onComments(comments.filter((each) => each.id !== draft.id))}>Delete</button></span>}
        </>}
      </div></td>
    </tr>);
  }

  function inRange(side: Side, line: number | null) {
    return active !== null && line !== null && active.side === side && line >= active.start && line <= active.end;
  }

  function anchorCell(side: Side, line: number | null) {
    if (line === null) return <td className="line-number" />;
    return <td className="line-number"><button type="button" className="line-anchor" tabIndex={-1} disabled={!editable}
      aria-label={`Comment on line ${line} ${side === "before" ? "before" : "after"} the change`}
      onClick={(event) => pick(side, line, event.shiftKey)}>{line}</button></td>;
  }

  function sourceTable(side: Side, text: string | null) {
    if (text === null) return <p className="unchanged">File absent</p>;
    if (text === "") return <p className="unchanged">Empty file</p>;
    const tokens = side === "before" ? before : after;
    const count = lineCount(text);
    return <table className="diff source" aria-label={`${side === "before" ? "Before" : "After"} ${file?.path ?? ""}`}><tbody>
      {Array.from({ length: count }, (_, index) => index + 1).map((line) => {
        const marker = markers[side].get(line);
        return [<tr key={line} ref={registered(side, line)} tabIndex={-1}
          className={`diff-line context${marker !== undefined ? ` marker marker-${marker.kind}` : ""}${side === "after" && marked.has(line) ? " suppression" : ""}${inRange(side, line) ? " commenting" : ""}`}>
          {anchorCell(side, line)}
          <td className="line-content"><code><Tokens tokens={tokens[line - 1]} /></code>{marker !== undefined && marker.line === line && <span className="marker-badge">{markerLabel(marker)}</span>}</td>
        </tr>, ...commentRows(side, line, 2)];
      })}
    </tbody></table>;
  }

  function tokensOf(line: ReviewLine): Token[] | undefined {
    if (line.kind === "remove" && line.old_line !== null) return before[line.old_line - 1];
    if ((line.kind === "add" || line.kind === "context") && line.new_line !== null) return after[line.new_line - 1];
    return [{ text: line.text.replace(/\n$/, ""), classes: "" }];
  }

  // One file whose review holds nothing else to navigate -- no auto-allowed sibling, no
  // exception to list -- gives the editor the whole width.
  const single = files.length === 1 && required.length === 1 && files.every((item) => item.suppressions.length === 0);
  return <section className="files" aria-label="Proposed file changes" data-mobile-panel={mobile} data-single={single ? "true" : undefined}>
    {!single && <nav className="file-overview" aria-label="File navigator">
      <div className="file-overview-heading"><h3>{visible.length} {visible.length === 1 ? "file" : "files"}{query !== "" ? " matching" : ""}</h3>
        <span className="change-counts"><span className="added">+{additions}</span> <span className="removed">−{deletions}</span></span></div>
      <div className="review-scope" aria-label="Review scope" title="Approval applies to the complete submitted operation.">
        <button type="button" aria-pressed={!full} onClick={() => showFull(false)}>Needs review ({required.length})</button>
        <button type="button" aria-pressed={full} onClick={() => showFull(true)}>Full operation ({files.length})</button></div>
      <div className="navigator-tabs" aria-label="Navigator view">
        <button type="button" aria-pressed={tab === "files"} onClick={() => setTab("files")}>Files</button>
        <button type="button" aria-pressed={tab === "exceptions"} onClick={() => setTab("exceptions")}>Exceptions ({suppressions.length})</button>
      </div>
      {directory !== "" && <details className="file-prefix"><summary>Common directory</summary><code>{directory}</code></details>}
      <label className="file-search">Find a file<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Path or filename" /></label>
      {tab === "files" ? <>
        <ul className="file-list">{visible.map((item) => <li key={item.path}><button type="button" aria-current={item.path === file?.path ? "true" : undefined} onClick={() => select(item.path)}>
          <code title={item.path}>{label(item.path)}</code><span className="file-facts"><span>{item.unchanged ? "unchanged" : item.operation}</span>
            <span className="change-counts" aria-label={`${item.additions} added lines, ${item.deletions} deleted lines`}><span className="added">+{item.additions}</span> <span className="removed">−{item.deletions}</span></span>
            <span className={`file-review-state ${item.review_effect}`} title={item.review_reason}>{reviewLabel(item.review_effect)}</span>
            {exceptionsFor(item).length > 0 && <span className="suppression-badge">{exceptionsFor(item).length} exceptions</span>}
            {comments.some((comment) => comment.path === item.path) && <span className="comment-badge">{comments.filter((comment) => comment.path === item.path).length} comments</span>}</span>
        </button></li>)}</ul>
        {visible.length === 0 && <p className="empty">No matching files.</p>}
      </> : <div className="suppression-overview" aria-label="Rule exceptions">
        <p className="exception-counts"><strong>{suppressions.length} occurrences</strong><span>{introduced} added · {suppressions.length - introduced} existing</span></p>
        {[...groups].map(([rule, entries]) => <details className="suppression-group" key={rule}>
          <summary><code>{rule}</code><span className="suppression-badge">{entries.filter((item) => item.suppression.introduced).length} added · {entries.filter((item) => !item.suppression.introduced).length} existing</span></summary>
          <ul>{entries.map((item) => <li key={item.key}><button type="button" aria-current={occurrence === item.key ? "true" : undefined} onClick={() => { setOccurrence(item.key); reveal(item.file, "after", item.suppression.line); }}>
            <code title={item.key}>{label(item.file.path)}:{item.suppression.line}</code><span>{item.suppression.reason || "No reason supplied"}</span>
            <span className={`file-review-state ${item.suppression.review_effect}`} title={item.suppression.review_reason}>{reviewLabel(item.suppression.review_effect)}</span>
          </button></li>)}</ul>
        </details>)}
        {suppressions.length === 0 && <p className="empty">{full ? "No rule exceptions in these files." : "No rule exceptions need review in these files."}</p>}
      </div>}
      <button className="mobile-only" type="button" onClick={() => setMobile("evidence")}>Back to diff</button>
    </nav>}
    {file === undefined ? <section className="file no-file-review">
      <h3>Review the operation</h3>
      <p>No file-specific approval is required. The request reason and exact operation still need review.</p>
      {command !== null && <pre>{command}</pre>}
      {required.length !== files.length && <button type="button" onClick={() => showFull(true)}>Show the full operation ({files.length} files)</button>}
    </section> : <section className="file" aria-label={`Selected file ${file.path}`}>
      <header className="file-bar">
        {!single && <button className="mobile-only" type="button" onClick={() => setMobile("navigator")}>Files</button>}
        <code className="file-name" title={file.path} tabIndex={0}>{label(file.path)}</code>
        <span className="file-facts"><span className="state">{file.operation}</span>
          <span className="change-counts"><span className="added">+{file.additions}</span> <span className="removed">−{file.deletions}</span></span>
          <button type="button" className={`file-review-state ${file.review_effect}`} aria-expanded={why} title={file.review_reason} onClick={() => setWhy((open) => !open)}>{reviewLabel(file.review_effect)}</button></span>
        {!single && <span className="file-paging"><button type="button" disabled={visible.length === 0 || fileIndex === 0} aria-label="Previous file" aria-keyshortcuts="[" onClick={() => moveFile(-1)}>←</button>
          <span>{fileIndex < 0 ? "Outside search" : `${fileIndex + 1} / ${visible.length}`}</span><button type="button" disabled={visible.length === 0 || fileIndex + 1 >= visible.length} aria-label="Next file" aria-keyshortcuts="]" onClick={() => moveFile(1)}>→</button></span>}
        <span className="evidence-tabs" aria-label="File evidence">
          {([["diff", "Diff"], ["before", "Before"], ["after", "After"], ["raw", "Raw"]] as const).map(([value, name]) => <button key={value} type="button" aria-pressed={view === value} onClick={() => { setView(value); setJump(null); }}>{name}</button>)}
          {view === "diff" && <button type="button" aria-pressed={whole} aria-keyshortcuts="F" onClick={() => setWhole((value) => !value)}>Whole file</button>}
        </span>
        {suppressions.length > 0 && <span className="exception-paging"><button type="button" aria-label="Previous exception" aria-keyshortcuts="P" disabled={exceptionIndex === 0} onClick={() => moveException(-1)}>↑</button>
          <button type="button" onClick={() => setTab("exceptions")}>Exceptions {exceptionIndex < 0 ? suppressions.length : `${exceptionIndex + 1}/${suppressions.length}`}</button>
          <button type="button" aria-label="Next exception" aria-keyshortcuts="N" disabled={exceptionIndex + 1 >= suppressions.length} onClick={() => moveException(1)}>↓</button></span>}
        {stops.length > 0 && <span className="marker-paging"><button type="button" aria-label="Previous marker" aria-keyshortcuts="Shift+M" disabled={markerAt === 0} onClick={() => moveMarker(-1)}>↑</button>
          <span>Markers {markerAt < 0 ? stops.length : `${markerAt + 1}/${stops.length}`}</span>
          <button type="button" aria-label="Next marker" aria-keyshortcuts="M" disabled={markerAt + 1 >= stops.length} onClick={() => moveMarker(1)}>↓</button></span>}
      </header>
      {file.about !== "" && <p className="file-about"><span className="account-source">agent's note on this file</span> {file.about}</p>}
      {why && <p className="file-why">{file.review_effect === "defer" && "No Lup approval requested; the native provider decides. "}{file.review_reason}<span className="file-target"> Target file: <code>{file.path}</code></span></p>}
      <div className="file-evidence diff-scroll" tabIndex={0} role="region" aria-label={`${view === "diff" ? whole ? "Whole file" : "Diff" : view === "raw" ? "Raw diff" : view === "before" ? "Before" : "After"} for ${file.path}`} ref={viewport}>
        {view === "diff" ? file.unchanged ? <p className="unchanged">This leaves the file unchanged.</p> : file.hunks.length === 0 ? <p className="unchanged">{file.before === null ? "Creates an empty file." : "Deletes an empty file."}</p> :
          <table className={`diff${language === null ? "" : " highlighted"}`} aria-label={`Changes to ${file.path}`}>
            <thead className="sr-only"><tr><th>Before line</th><th>After line</th><th>Change</th><th>Content</th></tr></thead>
            <tbody>{rows.map((row) => {
              if (row.kind === "hunk") return <tr className="diff-hunk" key={row.key}><td colSpan={4}><code>{row.header}</code></td></tr>;
              if (row.kind === "gap") return <tr className="diff-gap" key={row.key}><td colSpan={4}>
                <button type="button" onClick={() => setOpened((current) => new Set([...current, row.key]))}>Show {row.count} unchanged {row.count === 1 ? "line" : "lines"}</button></td></tr>;
              const { line } = row;
              const sides = rowSides(line);
              const marker = line.kind === "remove" ? line.old_line === null ? undefined : markers.before.get(line.old_line)
                : line.new_line === null ? undefined : markers.after.get(line.new_line);
              const exception = line.kind !== "remove" && line.new_line !== null && marked.has(line.new_line);
              return [<tr key={row.key} tabIndex={exception || marker !== undefined ? -1 : undefined}
                className={`diff-line ${line.kind}${exception ? " suppression" : ""}${marker !== undefined ? ` marker marker-${marker.kind}` : ""}${sides.some((each) => inRange(each.side, each.line)) ? " commenting" : ""}`}
                ref={(node) => { for (const each of sides) registered(each.side, each.line)(node); }}>
                {anchorCell("before", line.kind === "add" ? null : line.old_line)}{anchorCell("after", line.kind === "remove" ? null : line.new_line)}
                <td className="line-sign" aria-label={line.kind}>{line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}</td>
                <td className="line-content" title={exception ? "Rule exception" : undefined}><code><Tokens tokens={tokensOf(line)} /></code>
                  {marker !== undefined && (line.kind === "remove" ? line.old_line : line.new_line) === marker.line && <span className="marker-badge">{markerLabel(marker)}</span>}</td>
              </tr>, ...sides.flatMap((each) => commentRows(each.side, each.line, 4))];
            })}</tbody>
          </table> : view === "raw" ? <pre>{file.unified}</pre> : sourceTable(view, view === "before" ? file.before : file.after)}
      </div>
    </section>}
  </section>;
}
