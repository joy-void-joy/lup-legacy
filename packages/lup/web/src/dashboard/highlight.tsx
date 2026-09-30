// Syntax highlighting for the documents a review shows, one line at a time.
//
// A diff shows lines out of their document, so each document is highlighted
// whole — a docstring or a template string spanning lines is coloured as the
// language reads it — and then cut at its newlines, every token carried onto
// the lines it spans. Only the grammars registered here are bundled; a file
// whose name none of them claims is shown as plain text.
import type { ReactNode } from "react";
import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import makefile from "highlight.js/lib/languages/makefile";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const lowlight = createLowlight({ bash, css, diff, dockerfile, go, ini, javascript, json, makefile, markdown, python, rust, typescript, xml, yaml });

/** The grammar each file extension is read with; a name absent here is plain text. */
const EXTENSIONS: Record<string, string> = {
  py: "python", pyi: "python",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  json: "json", jsonl: "json",
  yml: "yaml", yaml: "yaml",
  toml: "ini", ini: "ini", cfg: "ini",
  md: "markdown", markdown: "markdown",
  sh: "bash", bash: "bash", zsh: "bash",
  css: "css",
  html: "xml", xml: "xml", svg: "xml",
  diff: "diff", patch: "diff",
  rs: "rust",
  go: "go",
};

/** The grammar each whole file name is read with, where its extension says nothing. */
const NAMES: Record<string, string> = { Dockerfile: "dockerfile", Makefile: "makefile", makefile: "makefile" };

/** Past this many characters a document is shown plain: highlighting it would stall the page. */
const HIGHLIGHT_LIMIT = 400_000;

/** The grammar a path is read with, or null where none is registered for it. */
export function languageFor(path: string): string | null {
  const name = path.slice(path.lastIndexOf("/") + 1);
  if (name in NAMES) return NAMES[name] ?? null;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? EXTENSIONS[name.slice(dot + 1).toLowerCase()] ?? null : null;
}

type Tree = ReturnType<typeof lowlight.highlight>;
type Content = Tree["children"][number];

/** One run of text on one line, and the grammar classes it carries. */
export type Token = { text: string; classes: string };

/**
 * A document cut into lines of tokens. The tree the grammar produced is
 * walked once, each text node split at its newlines, and every piece
 * carries the classes of every element it sits inside, so a token opened
 * on one line keeps its colour on the next.
 */
export function highlightedLines(text: string, language: string | null): Token[][] {
  const plain = () => text.split("\n").map((line) => [{ text: line, classes: "" }]);
  if (language === null || text.length > HIGHLIGHT_LIMIT) return plain();
  let tree: Tree;
  try {
    tree = lowlight.highlight(language, text);
  } catch {
    return plain();
  }
  const lines: Token[][] = [[]];
  function walk(nodes: Content[], classes: string) {
    for (const node of nodes) {
      if (node.type === "text") {
        node.value.split("\n").forEach((piece, index) => {
          if (index > 0) lines.push([]);
          if (piece !== "") lines[lines.length - 1]?.push({ text: piece, classes });
        });
      } else if (node.type === "element") {
        const named = node.properties.className;
        const own = Array.isArray(named) ? named.join(" ") : "";
        walk(node.children, own === "" ? classes : classes === "" ? own : `${classes} ${own}`);
      }
    }
  }
  walk(tree.children, "");
  return lines;
}

/** One line's tokens as elements, or the raw text where no grammar read it. */
export function Tokens({ tokens }: { tokens: Token[] | undefined }): ReactNode {
  if (tokens === undefined) return null;
  return tokens.map((token, index) => token.classes === ""
    ? token.text
    : <span key={index} className={token.classes}>{token.text}</span>);
}
