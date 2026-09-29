import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import type { ReviewFile } from "../generated/views";

export type FileNavigation = { moveFile(offset: number): void; moveException(offset: number): void };
type Evidence = "diff" | "before" | "after" | "raw";

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

export function Files({ files, navigation, command }: { files: ReviewFile[]; navigation: Ref<FileNavigation>; command: string | null }) {
  const [full, setFull] = useState(false);
  const [selected, setSelected] = useState(files[0]?.path ?? "");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"files" | "exceptions">("files");
  const [view, setView] = useState<Evidence>("diff");
  const [mobile, setMobile] = useState<"navigator" | "evidence">("evidence");
  const [occurrence, setOccurrence] = useState("");
  const [jump, setJump] = useState<{ line: number; revision: number } | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const diffLines = useRef(new Map<number, HTMLTableRowElement>());
  const sourceLines = useRef(new Map<number, HTMLSpanElement>());
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
  const fileIndex = visible.findIndex((item) => item.path === file?.path);
  const exceptionIndex = suppressions.findIndex((item) => item.key === occurrence);
  const introduced = suppressions.filter((item) => item.suppression.introduced).length;
  const directory = commonDirectory(scoped);
  const label = (path: string) => path.slice(directory.length);

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
    setJump(null);
    setMobile("evidence");
  }

  function moveFile(offset: number) {
    const target = visible[fileIndex < 0 ? offset > 0 ? 0 : visible.length - 1 : fileIndex + offset];
    if (target !== undefined) select(target.path);
  }

  function reveal(item: typeof suppressions[number]) {
    setSelected(item.file.path);
    setOccurrence(item.key);
    setView(item.file.hunks.some((hunk) => hunk.lines.some((line) => line.new_line === item.suppression.line)) ? "diff" : "after");
    setMobile("evidence");
    setJump((previous) => ({ line: item.suppression.line, revision: (previous?.revision ?? 0) + 1 }));
  }

  function moveException(offset: number) {
    const index = exceptionIndex < 0 ? offset > 0 ? 0 : suppressions.length - 1 : exceptionIndex + offset;
    const target = suppressions[index];
    if (target !== undefined) reveal(target);
  }

  useImperativeHandle(navigation, () => ({ moveFile, moveException }));
  useEffect(() => {
    if (jump !== null) {
      const target = view === "after" ? sourceLines.current.get(jump.line) : diffLines.current.get(jump.line);
      target?.scrollIntoView({ block: "center", inline: "nearest" });
      target?.focus({ preventScroll: true });
    } else {
      viewport.current?.scrollTo({ top: 0, left: 0 });
    }
  }, [file?.path, view, jump]);

  return <section className="files" aria-label="Proposed file changes" data-mobile-panel={mobile}>
    <div className="review-scope" aria-label="Review scope">
      <div><button type="button" aria-pressed={!full} onClick={() => showFull(false)}>Needs review ({required.length})</button>
        <button type="button" aria-pressed={full} onClick={() => showFull(true)}>Full operation ({files.length})</button></div>
      <span>Approval applies to the complete submitted operation.</span>
    </div>
    <nav className="file-overview" aria-label="File navigator">
      <div className="file-overview-heading"><h3>{visible.length} {visible.length === 1 ? "file" : "files"}{query !== "" ? " matching" : ""}</h3>
        <span className="change-counts"><span className="added">+{additions}</span> <span className="removed">−{deletions}</span></span></div>
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
            {exceptionsFor(item).length > 0 && <span className="suppression-badge">{exceptionsFor(item).length} exceptions</span>}</span>
        </button></li>)}</ul>
        {visible.length === 0 && <p className="empty">No matching files.</p>}
      </> : <div className="suppression-overview" aria-label="Rule exceptions">
        <p className="exception-counts"><strong>{suppressions.length} occurrences</strong><span>{introduced} added · {suppressions.length - introduced} existing</span></p>
        {[...groups].map(([rule, entries]) => <details className="suppression-group" key={rule}>
          <summary><code>{rule}</code><span className="suppression-badge">{entries.filter((item) => item.suppression.introduced).length} added · {entries.filter((item) => !item.suppression.introduced).length} existing</span></summary>
          <ul>{entries.map((item) => <li key={item.key}><button type="button" aria-current={occurrence === item.key ? "true" : undefined} onClick={() => reveal(item)}>
            <code title={item.key}>{label(item.file.path)}:{item.suppression.line}</code><span>{item.suppression.reason || "No reason supplied"}</span>
            <span className={`file-review-state ${item.suppression.review_effect}`} title={item.suppression.review_reason}>{reviewLabel(item.suppression.review_effect)}</span>
          </button></li>)}</ul>
        </details>)}
        {suppressions.length === 0 && <p className="empty">{full ? "No rule exceptions in these files." : "No rule exceptions need review in these files."}</p>}
      </div>}
      <button className="mobile-only" type="button" onClick={() => setMobile("evidence")}>Back to diff</button>
    </nav>
    {file === undefined ? <section className="file no-file-review">
      <h3>Review the operation</h3>
      <p>No file-specific approval is required. The request reason and exact operation still need review.</p>
      {command !== null && <pre>{command}</pre>}
      <p>Open Full operation to inspect the submitted files.</p>
    </section> : <section className="file" aria-label={`Selected file ${file.path}`}>
      <header className="file-heading" tabIndex={-1}>
        <button className="mobile-only" type="button" onClick={() => setMobile("navigator")}>Browse files / exceptions</button>
        <code title={file.path}>{label(file.path)}</code><span className="file-facts"><span className="state">{file.operation}</span>
          <span className="change-counts"><span className="added">+{file.additions}</span> <span className="removed">−{file.deletions}</span></span>
          <span className="file-paging"><button type="button" disabled={visible.length === 0 || fileIndex === 0} aria-label="Previous file" aria-keyshortcuts="[" onClick={() => moveFile(-1)}>←</button>
            <span>{fileIndex < 0 ? "Outside search" : `${fileIndex + 1} / ${visible.length}`}</span><button type="button" disabled={visible.length === 0 || fileIndex + 1 >= visible.length} aria-label="Next file" aria-keyshortcuts="]" onClick={() => moveFile(1)}>→</button></span></span>
        <details className="file-review"><summary>{reviewLabel(file.review_effect)} · Why</summary><p>{file.review_effect === "defer" && "No Lup approval requested; the native provider decides. "}{file.review_reason}</p></details>
        <div className="file-target"><span>Target file</span><code tabIndex={0}>{file.path}</code></div>
      </header>
      <div className="evidence-tabs" aria-label="File evidence">
        {([['diff', 'Diff'], ['before', 'Before'], ['after', 'After'], ['raw', 'Raw diff']] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={view === value} onClick={() => { setView(value); setJump(null); }}>{label}</button>)}
        {suppressions.length > 0 && <span className="exception-paging"><button type="button" aria-label="Previous exception" aria-keyshortcuts="P" disabled={exceptionIndex === 0} onClick={() => moveException(-1)}>↑</button>
          <button type="button" onClick={() => { setTab("exceptions"); setMobile("navigator"); }}>Exceptions {exceptionIndex < 0 ? suppressions.length : `${exceptionIndex + 1}/${suppressions.length}`}</button>
          <button type="button" aria-label="Next exception" aria-keyshortcuts="N" disabled={exceptionIndex + 1 >= suppressions.length} onClick={() => moveException(1)}>↓</button></span>}
      </div>
      <div className="file-evidence diff-scroll" tabIndex={0} role="region" aria-label={`${view === "diff" ? "Diff" : view === "raw" ? "Raw diff" : view === "before" ? "Before" : "After"} for ${file.path}`} ref={viewport}>
        {view === "diff" ? file.unchanged ? <p className="unchanged">This leaves the file unchanged.</p> : file.hunks.length === 0 ? <p className="unchanged">{file.before === null ? "Creates an empty file." : "Deletes an empty file."}</p> :
          <table className="diff" aria-label={`Changes to ${file.path}`}>
            <thead className="sr-only"><tr><th>Before line</th><th>After line</th><th>Change</th><th>Content</th></tr></thead>
            {file.hunks.map((hunk, hunkIndex) => <tbody key={hunkIndex}>
              <tr className="diff-hunk"><td colSpan={4}><code>{hunk.header}</code></td></tr>
              {hunk.lines.map((line, lineIndex) => <tr className={`diff-line ${line.kind}${line.new_line !== null && marked.has(line.new_line) ? " suppression" : ""}`} key={lineIndex} tabIndex={line.new_line !== null && marked.has(line.new_line) ? -1 : undefined}
                ref={(row) => { if (line.new_line !== null) { if (row === null) diffLines.current.delete(line.new_line); else diffLines.current.set(line.new_line, row); } }}>
                <td className="line-number">{line.old_line}</td><td className="line-number">{line.new_line}</td>
                <td className="line-sign" aria-label={line.kind}>{line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}</td>
                <td className="line-content" title={line.new_line !== null && marked.has(line.new_line) ? "Rule exception" : undefined}><code>{line.text}</code></td>
              </tr>)}
            </tbody>)}
          </table> : view === "raw" ? <pre>{file.unified}</pre> : view === "before" ? file.before === null ? <p className="unchanged">File absent</p> : file.before === "" ? <p className="unchanged">Empty file</p> : <pre>{file.before}</pre>
            : file.after === null ? <p className="unchanged">File absent</p> : file.after === "" ? <p className="unchanged">Empty file</p> : <pre className="source-text">{file.after.split("\n").map((line, index, lines) => <span
              className={`source-line${marked.has(index + 1) ? " suppression" : ""}`} data-line={index + 1} key={index} tabIndex={-1}
              ref={(node) => { if (node === null) sourceLines.current.delete(index + 1); else sourceLines.current.set(index + 1, node); }}>{line}{index + 1 < lines.length ? "\n" : ""}</span>)}</pre>}
      </div>
    </section>}
  </section>;
}
