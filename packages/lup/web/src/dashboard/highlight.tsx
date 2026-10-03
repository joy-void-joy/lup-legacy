// Syntax highlighting for the documents a review shows, one line at a time,
// drawn the way Neovim draws them: tree-sitter first, a language server on top.
//
// A diff shows lines out of their document, so each document is parsed whole
// by its language's tree-sitter grammar and coloured by that grammar's own
// highlights query — a docstring or a template string spanning lines is
// coloured as the language reads it — and then cut at its newlines, every
// token carried onto the lines it spans. Where several patterns capture one
// node the last of them wins, as tree-sitter and Neovim both read a query,
// and a node inside another is painted over it. Where a language server
// classified the names of the document (`textDocument/semanticTokens`), its
// classes are laid over tree-sitter's, as Neovim lays them: a name the server
// calls a class, a function or a parameter takes that colour, whatever its
// spelling suggested.
//
// A grammar is a WebAssembly module and a query, loaded the first time a
// document in its language is drawn and never before, so the first paint
// waits on no grammar it does not need: until one arrives its documents are
// drawn plain, and drawn again once it has.
//
// A document a merge left conflicted is no program a grammar can read whole:
// a string or a comment opened on one side runs on across the markers into
// the other side and past it. So each version of it — ours, theirs, and the
// common ancestor's where the conflict records it — is highlighted whole as
// the file it would be, and every line takes its tokens from the version it
// belongs to: a common line from ours, a side's line from its own, and a
// marker line is drawn as the marker it is. A file whose name no grammar
// claims is shown as plain text.
import { useEffect, useSyncExternalStore } from "react";
import type { Language, Parser, Query, QueryCapture } from "web-tree-sitter";
import { hasBase, parseConflicts, placed, VERSIONS, versionLines, type Standing } from "./conflicts";

/** How a grammar is read: its WASM, the highlights queries coloured by in order, what this page adds after them, captures it renames, and the grammar its inline text is read with. */
export type GrammarSpec = { wasm: string; queries: string[]; supplement?: string; renames?: Record<string, string>; inline?: { node: string; grammar: string } };

/**
 * What nvim-treesitter reads in Python and the published query leaves out or
 * reads otherwise: a method call (the published query lets the attribute
 * rule after it win), a capitalized call constructs, parameters, `self` and
 * `cls`, modules, decorators, the builtin types, class names, and control
 * flow. Read after the published query, so where both capture a node this
 * wins.
 */
const PYTHON = `
(call function: (attribute attribute: (identifier) @function.method.call))
((call function: (identifier) @constructor) (#match? @constructor "^[A-Z]"))
((call function: (attribute attribute: (identifier) @constructor)) (#match? @constructor "^[A-Z]"))
(class_definition name: (identifier) @type)
(parameters (identifier) @variable.parameter)
(lambda_parameters (identifier) @variable.parameter)
(default_parameter name: (identifier) @variable.parameter)
(typed_parameter (identifier) @variable.parameter)
(typed_default_parameter name: (identifier) @variable.parameter)
(list_splat_pattern (identifier) @variable.parameter)
(dictionary_splat_pattern (identifier) @variable.parameter)
(keyword_argument name: (identifier) @variable.parameter)
((identifier) @variable.builtin (#match? @variable.builtin "^(self|cls)$"))
(import_statement name: (dotted_name (identifier) @module))
(aliased_import name: (dotted_name (identifier) @module))
(import_from_statement module_name: (dotted_name (identifier) @module))
(decorator "@" @attribute)
(decorator (identifier) @attribute)
(decorator (attribute attribute: (identifier) @attribute))
(decorator (call function: (identifier) @attribute))
(decorator (call function: (attribute attribute: (identifier) @attribute)))
((type (identifier) @type.builtin) (#match? @type.builtin "^(bool|bytes|complex|dict|float|frozenset|int|list|object|set|str|tuple|type)$"))
["if" "elif" "else" "for" "while" "try" "except" "finally" "with" "return" "yield" "raise" "break" "continue" "match" "case" "await"] @keyword.control
["and" "or" "not" "in" "is"] @keyword.operator
`;

/**
 * TypeScript's published query calls every capitalized name a type, its
 * constants among them; nvim-treesitter keeps an all-capitals name a
 * constant, so this does too, after it.
 */
const TYPESCRIPT = `
((identifier) @constant (#match? @constant "^[A-Z_][A-Z0-9_]+$"))
`;

/** Every grammar the page reads, by the name `languageFor` gives it, each with its package's own highlights queries. */
export const GRAMMARS: Record<string, GrammarSpec> = {
  python: { wasm: "python", queries: ["python/highlights"], supplement: PYTHON },
  typescript: { wasm: "typescript", queries: ["javascript/highlights", "typescript/highlights"], supplement: TYPESCRIPT },
  tsx: { wasm: "tsx", queries: ["javascript/highlights", "javascript/highlights-jsx", "tsx/highlights"], supplement: TYPESCRIPT },
  javascript: { wasm: "javascript", queries: ["javascript/highlights", "javascript/highlights-jsx", "javascript/highlights-params"] },
  json: { wasm: "json", queries: ["json/highlights"] },
  yaml: { wasm: "yaml", queries: ["yaml/highlights"] },
  toml: { wasm: "toml", queries: ["toml/highlights"] },
  ini: { wasm: "ini", queries: ["ini/highlights"] },
  markdown: { wasm: "markdown", queries: ["markdown/highlights"], inline: { node: "inline", grammar: "markdown_inline" } },
  markdown_inline: { wasm: "markdown_inline", queries: ["markdown_inline/highlights"] },
  bash: { wasm: "bash", queries: ["bash/highlights"] },
  css: { wasm: "css", queries: ["css/highlights"] },
  html: { wasm: "html", queries: ["html/highlights"], renames: { attribute: "tag.attribute" } },
  xml: { wasm: "xml", queries: ["xml/highlights"] },
  diff: { wasm: "diff", queries: ["diff/highlights"] },
  rust: { wasm: "rust", queries: ["rust/highlights"] },
  go: { wasm: "go", queries: ["go/highlights"] },
  dockerfile: { wasm: "dockerfile", queries: ["dockerfile/highlights"] },
  make: { wasm: "make", queries: ["make/highlights"] },
};

/** Where grammars come from: the tree-sitter runtime's own WASM, each grammar's WASM, and each query's text. */
export type GrammarSource = {
  runtime(): Promise<Uint8Array>;
  wasm(name: string): Promise<Uint8Array>;
  query(path: string): Promise<string>;
};

/** A module Vite built from a file: the file's text, or a WASM's bytes in base64. */
type Built = Promise<{ default: string }>;

/** The bytes base64 text carries. */
function bytesOf(base64: string): Uint8Array {
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

/**
 * Each grammar's WASM as a chunk of its own, imported only when its language
 * is drawn. The bundle the dashboard serves holds text alone, so a WASM is
 * carried as base64 (`vite.config.ts`'s `base64Files`), and each chunk is
 * fetched by name from the bundle's `assets/`.
 */
const WASMS: Record<string, () => Built> = {
  python: () => import("tree-sitter-wasm/python/tree-sitter-python.wasm?base64"),
  typescript: () => import("tree-sitter-wasm/typescript/tree-sitter-typescript.wasm?base64"),
  tsx: () => import("tree-sitter-wasm/tsx/tree-sitter-tsx.wasm?base64"),
  javascript: () => import("tree-sitter-wasm/javascript/tree-sitter-javascript.wasm?base64"),
  json: () => import("tree-sitter-wasm/json/tree-sitter-json.wasm?base64"),
  yaml: () => import("tree-sitter-wasm/yaml/tree-sitter-yaml.wasm?base64"),
  toml: () => import("tree-sitter-wasm/toml/tree-sitter-toml.wasm?base64"),
  ini: () => import("tree-sitter-wasm/ini/tree-sitter-ini.wasm?base64"),
  markdown: () => import("tree-sitter-wasm/markdown/tree-sitter-markdown.wasm?base64"),
  markdown_inline: () => import("tree-sitter-wasm/markdown_inline/tree-sitter-markdown_inline.wasm?base64"),
  bash: () => import("tree-sitter-wasm/bash/tree-sitter-bash.wasm?base64"),
  css: () => import("tree-sitter-wasm/css/tree-sitter-css.wasm?base64"),
  html: () => import("tree-sitter-wasm/html/tree-sitter-html.wasm?base64"),
  xml: () => import("tree-sitter-wasm/xml/tree-sitter-xml.wasm?base64"),
  diff: () => import("tree-sitter-wasm/diff/tree-sitter-diff.wasm?base64"),
  rust: () => import("tree-sitter-wasm/rust/tree-sitter-rust.wasm?base64"),
  go: () => import("tree-sitter-wasm/go/tree-sitter-go.wasm?base64"),
  dockerfile: () => import("tree-sitter-wasm/dockerfile/tree-sitter-dockerfile.wasm?base64"),
  make: () => import("tree-sitter-wasm/make/tree-sitter-make.wasm?base64"),
};

/** Each highlights query, by its package path, imported beside its grammar. */
const QUERIES: Record<string, () => Built> = {
  "python/highlights": () => import("tree-sitter-wasm/python/highlights.scm?raw"),
  "javascript/highlights": () => import("tree-sitter-wasm/javascript/highlights.scm?raw"),
  "javascript/highlights-jsx": () => import("tree-sitter-wasm/javascript/highlights-jsx.scm?raw"),
  "javascript/highlights-params": () => import("tree-sitter-wasm/javascript/highlights-params.scm?raw"),
  "typescript/highlights": () => import("tree-sitter-wasm/typescript/highlights.scm?raw"),
  "tsx/highlights": () => import("tree-sitter-wasm/tsx/highlights.scm?raw"),
  "json/highlights": () => import("tree-sitter-wasm/json/highlights.scm?raw"),
  "yaml/highlights": () => import("tree-sitter-wasm/yaml/highlights.scm?raw"),
  "toml/highlights": () => import("tree-sitter-wasm/toml/highlights.scm?raw"),
  "ini/highlights": () => import("tree-sitter-wasm/ini/highlights.scm?raw"),
  "markdown/highlights": () => import("tree-sitter-wasm/markdown/highlights.scm?raw"),
  "markdown_inline/highlights": () => import("tree-sitter-wasm/markdown_inline/highlights.scm?raw"),
  "bash/highlights": () => import("tree-sitter-wasm/bash/highlights.scm?raw"),
  "css/highlights": () => import("tree-sitter-wasm/css/highlights.scm?raw"),
  "html/highlights": () => import("tree-sitter-wasm/html/highlights.scm?raw"),
  "xml/highlights": () => import("tree-sitter-wasm/xml/highlights.scm?raw"),
  "diff/highlights": () => import("tree-sitter-wasm/diff/highlights.scm?raw"),
  "rust/highlights": () => import("tree-sitter-wasm/rust/highlights.scm?raw"),
  "go/highlights": () => import("tree-sitter-wasm/go/highlights.scm?raw"),
  "dockerfile/highlights": () => import("tree-sitter-wasm/dockerfile/highlights.scm?raw"),
  "make/highlights": () => import("tree-sitter-wasm/make/highlights.scm?raw"),
};

/** Grammars as the bundle carries them: each fetched as its own chunk on first use. */
export const BUNDLED: GrammarSource = {
  runtime: async () => bytesOf((await import("web-tree-sitter/web-tree-sitter.wasm?base64")).default),
  wasm: async (name) => {
    const load = WASMS[name];
    if (load === undefined) throw new Error(`no grammar is bundled for ${name}`);
    return bytesOf((await load()).default);
  },
  query: async (path) => {
    const load = QUERIES[path];
    if (load === undefined) throw new Error(`no query is bundled at ${path}`);
    return (await load()).default;
  },
};

/** A grammar ready to read with: its parser, its query, what it renames, and the grammar for its inline text. */
type Grammar = { parser: Parser; query: Query; renames: Record<string, string>; inline: { node: string; grammar: Grammar } | null };

let source: GrammarSource = BUNDLED;
let runtime: Promise<typeof import("web-tree-sitter")> | null = null;
const loading = new Map<string, Promise<Grammar | null>>();
const loaded = new Map<string, Grammar | null>();
const failures = new Map<string, string>();
const listeners = new Set<() => void>();
let settled = 0;

/** Read grammars from another source from now on, everything loaded so far forgotten: what a test hands the page. */
export function setGrammarSource(next: GrammarSource): void {
  source = next;
  runtime = null;
  loading.clear();
  loaded.clear();
  failures.clear();
}

/** The tree-sitter runtime, started once, from its own WASM. */
function treeSitter(): Promise<typeof import("web-tree-sitter")> {
  runtime ??= (async () => {
    const [module, binary] = await Promise.all([import("web-tree-sitter"), source.runtime()]);
    await module.Parser.init({ wasmBinary: binary });
    return module;
  })();
  return runtime;
}

async function build(name: string): Promise<Grammar | null> {
  const spec = Object.hasOwn(GRAMMARS, name) ? GRAMMARS[name] : undefined;
  if (spec === undefined) return null;
  const { Language, Parser, Query } = await treeSitter();
  const [bytes, ...queries] = await Promise.all([source.wasm(spec.wasm), ...spec.queries.map((path) => source.query(path))]);
  const language: Language = await Language.load(bytes);
  const parser = new Parser();
  parser.setLanguage(language);
  const query = new Query(language, [...queries, spec.supplement ?? ""].join("\n"));
  const inline = spec.inline === undefined ? null : await loadGrammar(spec.inline.grammar);
  return { parser, query, renames: spec.renames ?? {}, inline: spec.inline === undefined || inline === null ? null : { node: spec.inline.node, grammar: inline } };
}

/**
 * Load a grammar, once: its WASM and its queries, as a chunk of their own.
 * One that fails to load or compile is drawn plain, and why is kept
 * (`grammarFailure`) and said on the console, never swallowed.
 */
export function loadGrammar(name: string): Promise<Grammar | null> {
  const held = loading.get(name);
  if (held !== undefined) return held;
  const started = build(name).catch((failure: unknown) => {
    const why = failure instanceof Error ? failure.message : String(failure);
    failures.set(name, why);
    console.error(`the ${name} grammar did not load, so its files are drawn plain: ${why}`);
    return null;
  }).then((grammar) => {
    loaded.set(name, grammar);
    settled += 1;
    for (const listener of listeners) listener();
    return grammar;
  });
  loading.set(name, started);
  return started;
}

/** Whether a grammar is loaded and its documents can be coloured now. */
export function grammarReady(name: string | null): boolean {
  return name !== null && (loaded.get(name) ?? null) !== null;
}

/** Why a grammar did not load, where one did not. */
export function grammarFailure(name: string): string {
  return failures.get(name) ?? "";
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Load every grammar named, and draw again each time one arrives: the count
 * of grammars settled, which a view passes down so its rows redraw.
 */
export function useGrammars(names: (string | null)[]): number {
  const wanted = names.filter((name): name is string => name !== null && Object.hasOwn(GRAMMARS, name)).join(" ");
  useEffect(() => {
    for (const name of wanted.split(" ")) if (name !== "" && !loading.has(name)) void loadGrammar(name);
  }, [wanted]);
  return useSyncExternalStore(subscribe, () => settled, () => settled);
}

/** The grammar each file extension is read with; a name absent here is plain text. */
const EXTENSIONS: Record<string, string> = {
  py: "python", pyi: "python",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "tsx",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  json: "json", jsonl: "json",
  yml: "yaml", yaml: "yaml",
  toml: "toml", ini: "ini", cfg: "ini",
  md: "markdown", markdown: "markdown",
  sh: "bash", bash: "bash", zsh: "bash",
  css: "css",
  html: "html", xml: "xml", svg: "xml",
  diff: "diff", patch: "diff",
  rs: "rust",
  go: "go",
};

/** The grammar each whole file name is read with, where its extension says nothing. */
const NAMES: Record<string, string> = { Dockerfile: "dockerfile", Makefile: "make", makefile: "make", GNUmakefile: "make" };

/** Past this many characters a document — a conflicted one's longest version — is shown plain: highlighting it would stall the page. */
export const HIGHLIGHT_LIMIT = 400_000;

/** The grammar a path is read with, or null where none is registered for it. */
export function languageFor(path: string): string | null {
  const name = path.slice(path.lastIndexOf("/") + 1);
  if (Object.hasOwn(NAMES, name)) return NAMES[name] ?? null;
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  return Object.hasOwn(EXTENSIONS, extension) ? EXTENSIONS[extension] ?? null : null;
}

/**
 * The colours the page draws code in, each one of the palette's syntax tokens
 * (`s-kw`, `s-ty`, …) or a way of drawing: a class name `sy-<group>`. The
 * empty group draws in the text's own colour.
 */
export type Group = "" | "kw" | "ctl" | "str" | "com" | "num" | "con" | "fn" | "ty" | "var" | "key" | "add" | "del" | "chg" | "head" | "em" | "strong" | "link" | "err";

/**
 * Each capture name the queries use — nvim-treesitter's names and the older
 * ones published queries still spell — and the group it draws in. A name
 * absent here falls back on its parent (`keyword.import` on `keyword`), and
 * one with no parent here draws plain. The colours follow VS Code's
 * high-contrast themes: a namespace in the type colour, a parameter in the
 * variable colour, `self` and `this` as keywords.
 */
const CAPTURES: Record<string, Group> = {
  keyword: "kw", storage: "kw", include: "kw", import: "kw", charset: "kw", media: "kw", keyframes: "kw", supports: "kw",
  "keyword.control": "ctl", "keyword.conditional": "ctl", "keyword.repeat": "ctl", "keyword.return": "ctl", "keyword.exception": "ctl", "keyword.coroutine": "ctl",
  conditional: "ctl", repeat: "ctl", exception: "ctl",
  string: "str", character: "str", "text.literal": "str", "markup.raw": "str",
  "string.special.key": "key", "string.special.url": "link", "text.uri": "link", "markup.link.url": "link",
  "string.escape": "con", escape: "con", "character.special": "con",
  comment: "com",
  number: "num", float: "num",
  boolean: "con", constant: "con",
  function: "fn", method: "fn", attribute: "fn", decorator: "fn",
  "constructor": "ty" as Group, type: "ty", module: "ty", namespace: "ty", class: "ty",
  "variable.parameter": "var", parameter: "var", "variable.member": "var", property: "var", field: "var", "tag.attribute": "var", label: "var",
  "text.reference": "var", "markup.link": "var",
  "variable.builtin": "kw",
  variable: "", operator: "", punctuation: "", embedded: "", none: "", spell: "",
  "punctuation.special": "kw",
  tag: "kw", "tag.delimiter": "", "tag.error": "err",
  "text.title": "head", "markup.heading": "head", "text.emphasis": "em", "markup.italic": "em", "text.strong": "strong", "markup.strong": "strong",
  "diff.plus": "add", "diff.minus": "del", "diff.delta": "chg",
};

/** The group a capture name draws in, its own or its nearest parent's. */
export function groupOf(capture: string): Group {
  for (let name = capture; name !== ""; name = name.includes(".") ? name.slice(0, name.lastIndexOf(".")) : "") {
    if (Object.hasOwn(CAPTURES, name)) return CAPTURES[name] ?? "";
  }
  return "";
}

/**
 * What each kind of name a language server tells of draws as, by the type
 * the protocol (and basedpyright, for `self` and `cls`) calls it. A
 * `variable` or an `operator` draws nothing of its own, so tree-sitter's
 * colour stays under it, as it does in Neovim.
 */
const SEMANTIC: Record<string, Group> = {
  namespace: "ty", type: "ty", class: "ty", enum: "ty", interface: "ty", struct: "ty", typeParameter: "ty",
  parameter: "var", property: "var", event: "var", enumMember: "con",
  function: "fn", method: "fn", macro: "fn", decorator: "fn",
  keyword: "kw", modifier: "kw", selfParameter: "kw", clsParameter: "kw",
  comment: "com", string: "str", regexp: "str", number: "num",
  variable: "", operator: "",
};

/** One stretch of a document painted in one colour: where it starts and ends, its group, and the capture or token type that chose it. */
export type Paint = { start: number; end: number; group: Group; name: string };

/** A language server's semantic tokens, as the dashboard relays them: five numbers a token, in the server's legend. */
export type SemanticTokens = { types: string[]; modifiers: string[]; data: number[] };

/**
 * A language server's tokens over one document, as paint: each token placed
 * from the line and UTF-16 start the protocol gives relative to the one
 * before. A token whose type draws nothing of its own paints nothing.
 */
export function semanticPaint(text: string, tokens: SemanticTokens): Paint[] {
  const starts = [0];
  for (let at = text.indexOf("\n"); at >= 0; at = text.indexOf("\n", at + 1)) starts.push(at + 1);
  const paint: Paint[] = [];
  let line = 0;
  let character = 0;
  for (let at = 0; at + 4 < tokens.data.length; at += 5) {
    const [deltaLine = 0, deltaStart = 0, length = 0, type = 0, bits = 0] = tokens.data.slice(at, at + 5);
    line += deltaLine;
    character = deltaLine > 0 ? deltaStart : character + deltaStart;
    const kind = tokens.types[type] ?? "";
    const group = Object.hasOwn(SEMANTIC, kind) ? SEMANTIC[kind] ?? "" : "";
    const lineStart = starts[line];
    if (group === "" || lineStart === undefined) continue;
    const modifiers = tokens.modifiers.filter((_, index) => (bits & (1 << index)) !== 0);
    paint.push({ start: lineStart + character, end: lineStart + character + length, group, name: ["lsp", kind, ...modifiers].join(".") });
  }
  return paint;
}

/** One run of text on one line: the classes it is drawn with, and the capture or token type that coloured it. */
export type Token = { text: string; classes: string; capture?: string };

/** One line of a document: its tokens, and where it stands in a conflict a merge left, null outside one. */
export type Line = { tokens: Token[]; conflict: Standing | null };

/**
 * What a grammar's query paints over a text, each capture of a node it
 * matched: where several patterns capture one node the last of them wins,
 * and a capture whose name starts with `_` is the query's own. Text a
 * grammar reads inline (Markdown's paragraphs) is read again by that grammar
 * and painted after, so it is drawn over the block it sits in.
 */
function captured(grammar: Grammar, text: string, offset: number): Paint[] {
  const tree = grammar.parser.parse(text);
  if (tree === null) return [];
  try {
    const last = new Map<number, QueryCapture>();
    for (const capture of grammar.query.captures(tree.rootNode)) {
      const held = last.get(capture.node.id);
      if (held === undefined || capture.patternIndex >= held.patternIndex) last.set(capture.node.id, capture);
    }
    const own = [...last.values()].filter((capture) => !capture.name.startsWith("_")).map((capture) => {
      const name = Object.hasOwn(grammar.renames, capture.name) ? grammar.renames[capture.name] ?? capture.name : capture.name;
      return { start: capture.node.startIndex + offset, end: capture.node.endIndex + offset, group: groupOf(name), name };
    });
    const inline = grammar.inline;
    if (inline === null) return own;
    const within = tree.rootNode.descendantsOfType(inline.node).flatMap((node) => node === null ? [] : captured(inline.grammar, text.slice(node.startIndex, node.endIndex), offset + node.startIndex));
    return [...own, ...within];
  } finally {
    tree.delete();
  }
}

/**
 * A text cut into lines of tokens, painted by a grammar and then by a
 * language server, or plain where no grammar is given or loaded. Each paint
 * covers its stretch, outer ones first and the ones inside them after, so a
 * name inside a string or a call is drawn as itself; then a token cut where
 * the colour changes and where a line ends, so a token opened on one line
 * keeps its colour on the next.
 */
function tokenLines(text: string, language: string | null, overlay: Paint[] = []): Token[][] {
  const grammar = language === null ? null : loaded.get(language) ?? null;
  if (grammar === null && overlay.length === 0) return text.split("\n").map((line) => [{ text: line, classes: "" }]);
  const paints = [...(grammar === null ? [] : captured(grammar, text, 0))].sort((left, right) => left.start - right.start || right.end - left.end);
  const covering = new Int32Array(text.length).fill(-1);
  const all = [...paints, ...overlay];
  all.forEach((paint, index) => covering.fill(index, Math.max(0, paint.start), Math.min(text.length, paint.end)));
  const lines: Token[][] = [[]];
  let start = 0;
  const close = (end: number) => {
    if (end <= start) return;
    const paint = all[covering[start] ?? -1];
    const token: Token = paint === undefined ? { text: text.slice(start, end), classes: "" } : { text: text.slice(start, end), classes: paint.group === "" ? "" : `sy-${paint.group}`, capture: paint.name };
    lines[lines.length - 1]?.push(token);
  };
  for (let at = 0; at < text.length; at += 1) {
    if (text[at] === "\n") {
      close(at);
      lines.push([]);
      start = at + 1;
    } else if (at > start && covering[at] !== covering[at - 1]) {
      close(at);
      start = at;
    }
  }
  close(text.length);
  return lines.map((tokens) => tokens.length === 0 ? [{ text: "", classes: "" }] : tokens);
}

/**
 * A document cut into lines of tokens, each `# lup:` marker drawn in its
 * kind's colour, a language server's paint over the grammar's where one is
 * given. A conflicted document's versions are each read whole and every line
 * takes the tokens of the one it belongs to; a marker line is one token in
 * its side's classes. One whose conflicts cannot be read — left open, or
 * markers out of order — is read whole as it stands. A server's paint is for
 * the document as it stands, so it is laid over that and never over a
 * conflicted document's versions.
 */
export function highlightedLines(text: string, language: string | null, overlay: Paint[] = []): Line[] {
  const regions = parseConflicts(text);
  if (regions === null) {
    const long = text.length > HIGHLIGHT_LIMIT;
    return tokenLines(text, long ? null : language, long ? [] : overlay).map((tokens) => ({ tokens: marked(tokens), conflict: null }));
  }
  const versions = VERSIONS.filter((version) => version !== "base" || hasBase(regions)).map((version) => ({ version, text: versionLines(regions, version).join("\n") }));
  const grammar = Math.max(...versions.map((each) => each.text.length)) > HIGHLIGHT_LIMIT ? null : language;
  const read = new Map(versions.map((each) => [each.version, tokenLines(each.text, grammar)]));
  return placed(regions).map((place) => place.kind === "marker"
    ? { tokens: [{ text: place.marker.text, classes: `cfm cfs-${place.standing.side}` }], conflict: place.standing }
    : { tokens: marked(read.get(place.version)?.[place.at] ?? []), conflict: place.standing });
}

/** A `# lup:` marker's head, as the repository spells each kind. */
const MARKER = /lup:\s*(defer(?:\[[^\]]*\])?:|solved:|template:|ignore\[[^\]]*\]|)/;

function markerKind(head: string): string {
  if (head.startsWith("defer")) return "defer";
  if (head.startsWith("solved")) return "solved";
  if (head.startsWith("template")) return "template";
  if (head.startsWith("ignore")) return "ignore";
  return "note";
}

/** One line's tokens with each `# lup:` marker inside a comment drawn in its kind's colour. */
function marked(tokens: Token[]): Token[] {
  return tokens.flatMap((token) => {
    if (token.capture === undefined || groupOf(token.capture) !== "com") return [token];
    const found = MARKER.exec(token.text);
    if (found === null) return [token];
    const at = found.index;
    return [
      { ...token, text: token.text.slice(0, at) },
      { ...token, text: found[0], classes: `${token.classes} mk mk-${markerKind(found[1] ?? "")}` },
      { ...token, text: token.text.slice(at + found[0].length) },
    ].filter((piece) => piece.text !== "");
  });
}
