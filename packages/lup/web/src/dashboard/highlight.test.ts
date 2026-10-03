import { describe, expect, test } from "bun:test";
import { HIGHLIGHT_LIMIT, highlightedLines, type Line } from "./highlight";

/** A file from its lines. Markers are built from strings so no line of this file starts with one. */
const file = (...lines: string[]) => lines.join("\n");
const OPEN = "<<<<<<<";
const BASE = "|||||||";
const SPLIT = "=======";
const CLOSE = ">>>>>>>";

/** The line a document's text holds, numbered from one as the buffer numbers it. */
function at(lines: Line[], number: number): Line {
  const line = lines[number - 1];
  if (line === undefined) throw new Error(`there is no line ${number}`);
  return line;
}

/** Whether some token on a line carries a class. */
const carries = (line: Line, name: string) => line.tokens.some((token) => token.classes.split(" ").includes(name));

/** The classes the token holding some text carries. */
function classesOf(line: Line, text: string): string {
  const token = line.tokens.find((each) => each.text.includes(text));
  if (token === undefined) throw new Error(`no token holds ${JSON.stringify(text)}`);
  return token.classes;
}

/** Every line's text, joined back from its tokens. */
const texts = (lines: Line[]) => lines.map((line) => line.tokens.map((token) => token.text).join(""));

// Our side opens a string the common text after the conflict closes, and
// their side holds code and opens a string of its own. Read whole, the first
// string swallows their code, their string's opening quotes close it, and the
// common closing quotes open one that runs to the end of the file.
const PYTHON = file(
  "def greet(name):",
  `${OPEN} HEAD`,
  '    message = f"""Hello {name},',
  SPLIT,
  '    message = "Hey " + name',
  "    return message.strip()",
  '    message += """',
  `${CLOSE} feat-x`,
  '    and welcome"""',
  "    return message",
  "",
  "def after():",
  "    return 42",
);

// Our side opens a block comment the common text closes; read whole, their
// side's code is comment.
const TYPESCRIPT = file(
  "export function greet(name: string): string {",
  `${OPEN} HEAD`,
  "  /* the greeting, kept short",
  SPLIT,
  "  const message = `Hey ${name}`;",
  "  /* the greeting, made casual",
  `${CLOSE} feat-x`,
  "     and closed here */",
  "  return `Hello ${name}`;",
  "}",
  "",
  "export const answer: number = 42;",
);

describe("highlighting a file a merge left conflicted", () => {
  test("a string opened on one side stays on that side: their code is code, and what follows the conflict is read as ours reads it", () => {
    const lines = highlightedLines(PYTHON, "python");
    expect(texts(lines)).toEqual(PYTHON.split("\n"));
    expect(classesOf(at(lines, 3), 'f"""Hello')).toContain("hljs-string");
    expect(carries(at(lines, 5), "hljs-string")).toBe(true);
    expect(classesOf(at(lines, 5), "+ name")).toBe("");
    expect(classesOf(at(lines, 6), "return")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 7), '"""')).toBe("hljs-string");
    expect(classesOf(at(lines, 9), "and welcome")).toBe("hljs-string");
    expect(classesOf(at(lines, 10), "return")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 12), "def")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 13), "42")).toBe("hljs-number");
  });

  test("a block comment opened on one side does not reach into the other", () => {
    const lines = highlightedLines(TYPESCRIPT, "typescript");
    expect(texts(lines)).toEqual(TYPESCRIPT.split("\n"));
    expect(classesOf(at(lines, 3), "kept short")).toBe("hljs-comment");
    expect(classesOf(at(lines, 5), "const")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 5), "`Hey ")).toBe("hljs-string");
    expect(classesOf(at(lines, 6), "made casual")).toBe("hljs-comment");
    expect(classesOf(at(lines, 8), "closed here")).toBe("hljs-comment");
    expect(classesOf(at(lines, 9), "return")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 12), "export")).toBe("hljs-keyword");
  });

  test("a comment opened before the conflict and closed on each side ends on each", () => {
    const text = file(
      "/**",
      " * Greets someone.",
      `${OPEN} HEAD`,
      " * @param name who is greeted",
      " */",
      "export function greet(name: string): string {",
      SPLIT,
      " */",
      "export function greet(name: string, loud = false): string {",
      `${CLOSE} feat-x`,
      "  return `Hello ${name}`;",
      "}",
    );
    const lines = highlightedLines(text, "typescript");
    expect(classesOf(at(lines, 4), "name who")).toBe("hljs-comment");
    expect(classesOf(at(lines, 8), "*/")).toBe("hljs-comment");
    expect(classesOf(at(lines, 9), "export")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 9), "false")).toContain("hljs-literal");
  });

  test("each marker is one token in its side's classes, naming the side and its label, never read by the grammar", () => {
    const lines = highlightedLines(TYPESCRIPT, "typescript");
    expect(at(lines, 2)).toEqual({ tokens: [{ text: `${OPEN} HEAD`, classes: "cfm cfs-ours" }], conflict: { side: "ours", marker: "open", label: "HEAD" } });
    expect(at(lines, 4)).toEqual({ tokens: [{ text: SPLIT, classes: "cfm cfs-theirs" }], conflict: { side: "theirs", marker: "open", label: "feat-x" } });
    expect(at(lines, 7)).toEqual({ tokens: [{ text: `${CLOSE} feat-x`, classes: "cfm cfs-theirs" }], conflict: { side: "theirs", marker: "close", label: "feat-x" } });
    expect(at(lines, 3).conflict).toEqual({ side: "ours", marker: null, label: "HEAD" });
    expect(at(lines, 5).conflict).toEqual({ side: "theirs", marker: null, label: "feat-x" });
    expect(at(lines, 1).conflict).toBeNull();
    expect(at(lines, 9).conflict).toBeNull();
  });

  test("diff3's ancestor is read whole as its own version, its marker in its own classes", () => {
    const text = file(
      "def greet(name):",
      `${OPEN} HEAD`,
      '    message = f"Hello {name}"',
      `${BASE} 1a2b3c4`,
      '    message = """Hello',
      '    there""" + suffix',
      SPLIT,
      '    message = "Hey " + name',
      `${CLOSE} feat-x`,
      "    return message",
    );
    const lines = highlightedLines(text, "python");
    expect(at(lines, 4)).toEqual({ tokens: [{ text: `${BASE} 1a2b3c4`, classes: "cfm cfs-base" }], conflict: { side: "base", marker: "open", label: "1a2b3c4" } });
    expect(at(lines, 5).conflict).toEqual({ side: "base", marker: null, label: "1a2b3c4" });
    expect(classesOf(at(lines, 5), '"""Hello')).toBe("hljs-string");
    expect(classesOf(at(lines, 6), 'there"""')).toBe("hljs-string");
    expect(classesOf(at(lines, 6), "+ suffix")).toBe("");
    expect(classesOf(at(lines, 8), '"Hey "')).toBe("hljs-string");
    expect(classesOf(at(lines, 10), "return")).toBe("hljs-keyword");
  });

  test("text inside a string that only looks like a marker is a line of the string", () => {
    const text = file(
      `${OPEN} HEAD`,
      'HELP = """',
      `    ${OPEN} HEAD opens our side`,
      `    ${SPLIT} then theirs`,
      '"""',
      SPLIT,
      `HELP = "see ${CLOSE} in git help merge"`,
      `${CLOSE} feat-x`,
      "x = 1",
    );
    const lines = highlightedLines(text, "python");
    expect(at(lines, 3).conflict).toEqual({ side: "ours", marker: null, label: "HEAD" });
    expect(classesOf(at(lines, 3), "opens our side")).toBe("hljs-string");
    expect(classesOf(at(lines, 4), "then theirs")).toBe("hljs-string");
    expect(classesOf(at(lines, 7), "git help merge")).toBe("hljs-string");
    expect(classesOf(at(lines, 9), "1")).toBe("hljs-number");
  });

  test("an unterminated or out-of-order conflict is highlighted as the file stands, never plain", () => {
    for (const text of [
      file("def before():", `${OPEN} HEAD`, "    return 1", SPLIT, "    return 2", "def after():", "    return 3"),
      file("def before():", `${OPEN} HEAD`, "    return 1", `${CLOSE} feat-x`, "def after():", "    return 3"),
    ]) {
      const lines = highlightedLines(text, "python");
      expect(texts(lines)).toEqual(text.split("\n"));
      expect(lines.every((line) => line.conflict === null)).toBe(true);
      expect(classesOf(at(lines, 1), "def")).toBe("hljs-keyword");
      expect(classesOf(at(lines, lines.length - 1), "def")).toBe("hljs-keyword");
    }
  });

  test("CRLF lines keep their carriage returns, and the conflict is read through them", () => {
    const text = PYTHON.split("\n").join("\r\n");
    const lines = highlightedLines(text, "python");
    expect(texts(lines)).toEqual(text.split("\n"));
    expect(at(lines, 2).conflict).toEqual({ side: "ours", marker: "open", label: "HEAD" });
    expect(at(lines, 8).conflict).toEqual({ side: "theirs", marker: "close", label: "feat-x" });
    expect(classesOf(at(lines, 6), "return")).toBe("hljs-keyword");
    expect(classesOf(at(lines, 12), "def")).toBe("hljs-keyword");
  });

  test("a `# lup:` marker in a comment on a side is drawn in its kind's colour", () => {
    const text = file(`${OPEN} HEAD`, "x = 1", SPLIT, "x = 2  # lup: defer: settle which value wins", `${CLOSE} feat-x`);
    const lines = highlightedLines(text, "python");
    expect(classesOf(at(lines, 4), "lup: defer:")).toBe("hljs-comment mk mk-defer");
  });

  test("a file no grammar reads still has its conflict marked, its text plain", () => {
    const lines = highlightedLines(file("a", `${OPEN} HEAD`, "b", SPLIT, "c", `${CLOSE} feat-x`), null);
    expect(lines.map((line) => line.conflict?.side ?? null)).toEqual([null, "ours", "ours", "theirs", "theirs", "theirs"]);
    expect(at(lines, 3).tokens).toEqual([{ text: "b", classes: "" }]);
    expect(at(lines, 2).tokens).toEqual([{ text: `${OPEN} HEAD`, classes: "cfm cfs-ours" }]);
  });

  test("the size limit is measured against the larger side: two sides under it are highlighted, one over it leaves every side plain", () => {
    const half = "x = 1\n".repeat(Math.ceil(HIGHLIGHT_LIMIT / 6 / 2) + 1000);
    const under = file("y = 2", `${OPEN} HEAD`, half, SPLIT, half, `${CLOSE} feat-x`, "z = 3");
    expect(under.length).toBeGreaterThan(HIGHLIGHT_LIMIT);
    const read = highlightedLines(under, "python");
    expect(classesOf(at(read, 3), "1")).toBe("hljs-number");
    expect(classesOf(at(read, read.length), "3")).toBe("hljs-number");
    const over = file("y = 2", `${OPEN} HEAD`, "x = 1", SPLIT, half + half, `${CLOSE} feat-x`, "z = 3");
    const plain = highlightedLines(over, "python");
    expect(at(plain, 3).tokens).toEqual([{ text: "x = 1", classes: "" }]);
    expect(at(plain, 2).tokens).toEqual([{ text: `${OPEN} HEAD`, classes: "cfm cfs-ours" }]);
    expect(at(plain, plain.length).tokens).toEqual([{ text: "z = 3", classes: "" }]);
  });
});

describe("highlighting a file without conflicts", () => {
  test("a document is read whole, a string spanning lines coloured on each", () => {
    const lines = highlightedLines(file('x = """one', 'two"""', "y = 2"), "python");
    expect(classesOf(at(lines, 2), "two")).toBe("hljs-string");
    expect(classesOf(at(lines, 3), "2")).toBe("hljs-number");
    expect(lines.every((line) => line.conflict === null)).toBe(true);
  });

  test("past the size limit, or with no grammar, a document is plain", () => {
    const long = "x = 1\n".repeat(Math.ceil(HIGHLIGHT_LIMIT / 6) + 1);
    expect(at(highlightedLines(long, "python"), 1).tokens).toEqual([{ text: "x = 1", classes: "" }]);
    expect(at(highlightedLines("x = 1", null), 1).tokens).toEqual([{ text: "x = 1", classes: "" }]);
  });
});
