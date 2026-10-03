import { beforeAll, describe, expect, test } from "bun:test";
import { GRAMMARS, grammarFailure, grammarReady, groupOf, HIGHLIGHT_LIMIT, highlightedLines, languageFor, loadGrammar, semanticPaint, type Line, type Paint } from "./highlight";

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

/** The token holding some text on a line: the one that is it, else the first holding it. */
function token(line: Line, text: string) {
  const found = line.tokens.find((each) => each.text === text) ?? line.tokens.find((each) => each.text.includes(text));
  if (found === undefined) throw new Error(`no token holds ${JSON.stringify(text)} in ${JSON.stringify(line.tokens)}`);
  return found;
}

/** The classes the token holding some text carries. */
const classesOf = (line: Line, text: string) => token(line, text).classes;

/** What coloured the token holding some text: its capture, or the server's token type. */
const captureOf = (line: Line, text: string) => token(line, text).capture ?? "";

/** Every line's text, joined back from its tokens. */
const texts = (lines: Line[]) => lines.map((line) => line.tokens.map((each) => each.text).join(""));

beforeAll(async () => {
  await Promise.all(Object.keys(GRAMMARS).map((name) => loadGrammar(name)));
});

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
    expect(classesOf(at(lines, 3), 'f"""Hello')).toContain("sy-str");
    expect(carries(at(lines, 5), "sy-str")).toBe(true);
    expect(classesOf(at(lines, 5), "+")).toBe("");
    expect(classesOf(at(lines, 6), "return")).toBe("sy-ctl");
    expect(classesOf(at(lines, 7), '"""')).toBe("sy-str");
    expect(classesOf(at(lines, 9), "and welcome")).toBe("sy-str");
    expect(classesOf(at(lines, 10), "return")).toBe("sy-ctl");
    expect(classesOf(at(lines, 12), "def")).toBe("sy-kw");
    expect(classesOf(at(lines, 13), "42")).toBe("sy-num");
  });

  test("a block comment opened on one side does not reach into the other", () => {
    const lines = highlightedLines(TYPESCRIPT, "typescript");
    expect(texts(lines)).toEqual(TYPESCRIPT.split("\n"));
    expect(classesOf(at(lines, 3), "kept short")).toBe("sy-com");
    expect(classesOf(at(lines, 5), "const")).toBe("sy-kw");
    expect(classesOf(at(lines, 5), "`Hey ")).toBe("sy-str");
    expect(classesOf(at(lines, 6), "made casual")).toBe("sy-com");
    expect(classesOf(at(lines, 8), "closed here")).toBe("sy-com");
    expect(classesOf(at(lines, 9), "return")).toBe("sy-kw");
    expect(classesOf(at(lines, 12), "export")).toBe("sy-kw");
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
    expect(classesOf(at(lines, 4), "name who")).toBe("sy-com");
    expect(classesOf(at(lines, 8), "*/")).toBe("sy-com");
    expect(classesOf(at(lines, 9), "export")).toBe("sy-kw");
    expect(classesOf(at(lines, 9), "false")).toBe("sy-con");
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
    expect(classesOf(at(lines, 5), '"""Hello')).toBe("sy-str");
    expect(classesOf(at(lines, 6), 'there"""')).toBe("sy-str");
    expect(classesOf(at(lines, 6), "suffix")).toBe("");
    expect(classesOf(at(lines, 8), '"Hey "')).toBe("sy-str");
    expect(classesOf(at(lines, 10), "return")).toBe("sy-ctl");
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
    expect(classesOf(at(lines, 3), "opens our side")).toBe("sy-str");
    expect(classesOf(at(lines, 4), "then theirs")).toBe("sy-str");
    expect(classesOf(at(lines, 7), "git help merge")).toBe("sy-str");
    expect(classesOf(at(lines, 9), "1")).toBe("sy-num");
  });

  test("an unterminated or out-of-order conflict is highlighted as the file stands, never plain", () => {
    for (const text of [
      file("def before():", `${OPEN} HEAD`, "    return 1", SPLIT, "    return 2", "def after():", "    return 3"),
      file("def before():", `${OPEN} HEAD`, "    return 1", `${CLOSE} feat-x`, "def after():", "    return 3"),
    ]) {
      const lines = highlightedLines(text, "python");
      expect(texts(lines)).toEqual(text.split("\n"));
      expect(lines.every((line) => line.conflict === null)).toBe(true);
      expect(classesOf(at(lines, 1), "def")).toBe("sy-kw");
      expect(classesOf(at(lines, lines.length - 1), "def")).toBe("sy-kw");
    }
  });

  test("CRLF lines keep their carriage returns, and the conflict is read through them", () => {
    const text = PYTHON.split("\n").join("\r\n");
    const lines = highlightedLines(text, "python");
    expect(texts(lines)).toEqual(text.split("\n"));
    expect(at(lines, 2).conflict).toEqual({ side: "ours", marker: "open", label: "HEAD" });
    expect(at(lines, 8).conflict).toEqual({ side: "theirs", marker: "close", label: "feat-x" });
    expect(classesOf(at(lines, 6), "return")).toBe("sy-ctl");
    expect(classesOf(at(lines, 12), "def")).toBe("sy-kw");
  });

  test("a `# lup:` marker in a comment on a side is drawn in its kind's colour", () => {
    const text = file(`${OPEN} HEAD`, "x = 1", SPLIT, "x = 2  # lup: defer: settle which value wins", `${CLOSE} feat-x`);
    const lines = highlightedLines(text, "python");
    expect(classesOf(at(lines, 4), "lup: defer:")).toBe("sy-com mk mk-defer");
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
    expect(classesOf(at(read, 3), "1")).toBe("sy-num");
    expect(classesOf(at(read, read.length), "3")).toBe("sy-num");
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
    expect(classesOf(at(lines, 2), "two")).toBe("sy-str");
    expect(classesOf(at(lines, 3), "2")).toBe("sy-num");
    expect(lines.every((line) => line.conflict === null)).toBe(true);
  });

  test("past the size limit, or with no grammar, a document is plain", () => {
    const long = "x = 1\n".repeat(Math.ceil(HIGHLIGHT_LIMIT / 6) + 1);
    expect(at(highlightedLines(long, "python"), 1).tokens).toEqual([{ text: "x = 1", classes: "" }]);
    expect(at(highlightedLines("x = 1", null), 1).tokens).toEqual([{ text: "x = 1", classes: "" }]);
  });
});

describe("tree-sitter's captures, as Neovim reads them", () => {
  const MODEL = file(
    "from pydantic import BaseModel, Field",
    "",
    "",
    "@dataclass",
    "class Item(BaseModel):",
    '    name: str = Field(default="x")  # the name',
    "",
    "    def shout(self, count: int) -> str:",
    "        print(self.name.upper())",
    "        return self.name * count",
  );

  test("a capitalized call constructs, so `Field(...)` and a base class are drawn in the type colour", () => {
    const lines = highlightedLines(MODEL, "python");
    expect(captureOf(at(lines, 6), "Field")).toBe("constructor");
    expect(classesOf(at(lines, 6), "Field")).toBe("sy-ty");
    expect(captureOf(at(lines, 5), "BaseModel")).toBe("constructor");
    expect(captureOf(at(lines, 5), "Item")).toBe("type");
    expect(classesOf(at(lines, 1), "BaseModel")).toBe("sy-ty");
  });

  test("each kind of name takes its own colour: module, decorator, parameter, self, builtin type, call, method, property", () => {
    const lines = highlightedLines(MODEL, "python");
    expect([captureOf(at(lines, 1), "pydantic"), classesOf(at(lines, 1), "pydantic")]).toEqual(["module", "sy-ty"]);
    expect([captureOf(at(lines, 4), "dataclass"), classesOf(at(lines, 4), "dataclass")]).toEqual(["attribute", "sy-fn"]);
    expect([captureOf(at(lines, 8), "count"), classesOf(at(lines, 8), "count")]).toEqual(["variable.parameter", "sy-var"]);
    expect([captureOf(at(lines, 8), "self"), classesOf(at(lines, 8), "self")]).toEqual(["variable.builtin", "sy-kw"]);
    expect([captureOf(at(lines, 8), "int"), classesOf(at(lines, 8), "int")]).toEqual(["type.builtin", "sy-ty"]);
    expect([captureOf(at(lines, 8), "shout"), classesOf(at(lines, 8), "shout")]).toEqual(["function", "sy-fn"]);
    expect([captureOf(at(lines, 9), "print"), classesOf(at(lines, 9), "print")]).toEqual(["function.builtin", "sy-fn"]);
    expect([captureOf(at(lines, 9), "upper"), classesOf(at(lines, 9), "upper")]).toEqual(["function.method.call", "sy-fn"]);
    expect([captureOf(at(lines, 10), "name"), classesOf(at(lines, 10), "name")]).toEqual(["property", "sy-var"]);
    expect([captureOf(at(lines, 6), "# the name"), classesOf(at(lines, 6), "# the name")]).toEqual(["comment", "sy-com"]);
  });

  test("TypeScript reads JavaScript's query and then its own: types, builtin types, parameters, constants", () => {
    const lines = highlightedLines(file("const LIMIT = 3;", "export class Keymap {", "  of(name: string): Bound | undefined { return new Keymap(); }", "}"), "typescript");
    expect(captureOf(at(lines, 1), "LIMIT")).toBe("constant");
    expect(classesOf(at(lines, 1), "LIMIT")).toBe("sy-con");
    expect(classesOf(at(lines, 2), "Keymap")).toBe("sy-ty");
    expect(captureOf(at(lines, 3), "name")).toBe("variable.parameter");
    expect(captureOf(at(lines, 3), "string")).toBe("type.builtin");
    expect(captureOf(at(lines, 3), "of")).toBe("function.method");
    expect(classesOf(at(lines, 3), "Bound")).toBe("sy-ty");
  });

  test("Markdown reads its inline text with a grammar of its own", () => {
    const lines = highlightedLines(file("# Title", "", "Some *emphasis* and `code`."), "markdown");
    expect(classesOf(at(lines, 1), "Title")).toBe("sy-head");
    expect(classesOf(at(lines, 3), "emphasis")).toBe("sy-em");
    expect(classesOf(at(lines, 3), "code")).toBe("sy-str");
  });

  test("every grammar a file name maps to loads, its package's queries and the page's additions compiling", () => {
    for (const name of Object.keys(GRAMMARS)) {
      expect(grammarFailure(name)).toBe("");
      expect(grammarReady(name)).toBe(true);
    }
    const mapped = ["a.py", "a.pyi", "a.ts", "a.tsx", "a.mts", "a.js", "a.jsx", "a.json", "a.yml", "a.toml", "a.ini", "a.cfg", "a.md", "a.sh", "a.css", "a.html", "a.xml", "a.svg", "a.diff", "a.rs", "a.go", "Dockerfile", "Makefile"].map(languageFor);
    expect(mapped.every((name) => name !== null && Object.hasOwn(GRAMMARS, name))).toBe(true);
    expect(languageFor("src/a.tsx")).toBe("tsx");
    expect(languageFor("Makefile")).toBe("make");
    expect(languageFor("pyproject.toml")).toBe("toml");
    expect(languageFor("constructor")).toBeNull();
    expect(languageFor("notes.toString")).toBeNull();
  });

  test("a capture names its group, or its nearest parent's, and one no group claims draws plain", () => {
    expect(groupOf("keyword.import")).toBe("kw");
    expect(groupOf("keyword.return")).toBe("ctl");
    expect(groupOf("function.method.call")).toBe("fn");
    expect(groupOf("string.special.key")).toBe("key");
    expect(groupOf("variable")).toBe("");
    expect(groupOf("toString")).toBe("");
  });
});

describe("a language server's tokens laid over tree-sitter's", () => {
  const TOKENS = { types: ["variable", "function", "class", "parameter"], modifiers: ["declaration", "defaultLibrary"], data: [0, 0, 1, 2, 0, 1, 4, 1, 1, 2, 0, 2, 1, 0, 0] };

  test("its numbers are placed relative to the token before, each in the server's legend", () => {
    expect(semanticPaint("a = 1\nb = a\n", TOKENS)).toEqual([
      { start: 0, end: 1, group: "ty", name: "lsp.class" },
      { start: 10, end: 11, group: "fn", name: "lsp.function.defaultLibrary" },
    ]);
  });

  test("the server wins: a name tree-sitter called a constructor is drawn as the function the server says it is", () => {
    const text = "x = Field(default=1)";
    const paint: Paint[] = [{ start: 4, end: 9, group: "fn", name: "lsp.function" }];
    const alone = at(highlightedLines(text, "python"), 1);
    const over = at(highlightedLines(text, "python", paint), 1);
    expect([captureOf(alone, "Field"), classesOf(alone, "Field")]).toEqual(["constructor", "sy-ty"]);
    expect([captureOf(over, "Field"), classesOf(over, "Field")]).toEqual(["lsp.function", "sy-fn"]);
    expect(captureOf(over, "default")).toBe("variable.parameter");
  });

  test("a server's paint is for the document as it stands, so a conflicted one's versions never take it", () => {
    const text = file(`${OPEN} HEAD`, "x = Field(1)", SPLIT, "x = Field(2)", `${CLOSE} feat-x`);
    const lines = highlightedLines(text, "python", [{ start: 12, end: 17, group: "fn", name: "lsp.function" }]);
    expect(captureOf(at(lines, 2), "Field")).toBe("constructor");
  });
});
