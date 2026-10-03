// One workspace, one Vite build per surface. Which surface is being built
// arrives as Vite's own `--mode`, and where its bundle lands as `--outDir`,
// because the Python writer that materialises `lup.web`'s package data runs
// this once per surface into a directory it owns — the config declares the
// shape and never chooses a destination of its own.
import { defineConfig, parseAst } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

/** The surfaces whose page is also exported as one self-contained file. */
const EXPORTED = ["explorer"];

/** The suffix that imports a file's bytes as base64 text. */
const BASE64 = "?base64";

/**
 * A file imported as `<file>?base64` becomes a module whose default export
 * is its bytes in base64: how a WASM reaches a bundle that holds text alone.
 * Imported dynamically, each is a chunk of its own, fetched only when asked
 * for.
 */
export function base64Files(): Plugin {
  return {
    name: "lup:base64",
    enforce: "pre",
    async resolveId(source, importer) {
      if (!source.endsWith(BASE64)) return null;
      const found = await this.resolve(source.slice(0, -BASE64.length), importer, { skipSelf: true });
      return found === null ? null : `${found.id}${BASE64}`;
    },
    async load(id) {
      if (!id.endsWith(BASE64)) return null;
      const bytes = await readFile(id.slice(0, -BASE64.length));
      return `export default ${JSON.stringify(bytes.toString("base64"))};`;
    },
  };
}

/** One emitted file by name, with the text it holds. */
export interface Emitted {
  fileName: string;
  text: string;
}

/**
 * A script escaped for inlining, proven to parse as it did.
 *
 * A bundle served from `assets/` is a file the browser fetches; an export
 * carries it inside a `<script>` element, where the HTML tokenizer reads the
 * text before JavaScript does: `</script` ends the element wherever it
 * falls, string literal or not, and `<!--` puts the tokenizer into a state
 * where a later `</script>` may not. The escapes are the HTML
 * specification's own for script content — `<\/script` and `<\!--`, matched
 * without regard to case as the tokenizer matches them — applied once, here,
 * so the served bundle and the exported page carry the same bytes.
 *
 * The invariant: inside a JavaScript string, template literal, regular
 * expression or comment a backslash before `/` or `!` is an identity
 * escape, so the program means what it meant; anywhere else those sequences
 * are not JavaScript at all, so a rewrite that reached code is a syntax
 * error rather than a silent change. The parse after the rewrite turns that
 * into a build failure naming the file and the position. What a parse cannot
 * see, and Vite never emits: the raw text of a tagged template, and the body
 * of a `u`-flagged regular expression, where `\!` is refused when it runs.
 */
export function escapedScript(code: string, fileName: string): string {
  const escaped = code.replace(/<(?=\/script|!--)/gi, "<\\");
  try {
    parseAst(escaped, null, fileName);
  } catch (failure) {
    const message = failure instanceof Error ? failure.message : String(failure);
    throw new Error(
      `${fileName} does not parse once escaped for inlining, so a \`</script\` or ` +
        `\`<!--\` sits outside a literal:\n${message}`,
    );
  }
  return escaped;
}

/**
 * A stylesheet escaped for inlining: `</style` cannot end the element it
 * sits in. CSS escapes `/` in its strings and comments the same way and has
 * no grammar of its own that `</style` could belong to, so nothing is parsed.
 */
export function escapedStyle(css: string): string {
  return css.replace(/<(?=\/style)/gi, "<\\");
}

/** `text` as a regular expression matching exactly it. */
function literal(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The tag Vite wrote for one emitted file: its element, the attribute naming the file, and how it closes. */
function tagFor(element: string, attribute: string, fileName: string, closing: string): RegExp {
  return new RegExp(`<${element}\\b[^>]*\\s${attribute}="\\./${literal(fileName)}"[^>]*${closing}`);
}

/**
 * `text` with the one match of `pattern` — `what`, to a reader — replaced,
 * spliced rather than passed to `String.replace`, whose replacement string
 * reads `$` sequences, which a minified bundle is full of.
 */
function replacedOnce(text: string, pattern: RegExp, what: string, replacement: string): string {
  const flags = pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`;
  const found = [...text.matchAll(new RegExp(pattern.source, flags))];
  const [only] = found;
  if (found.length !== 1 || only === undefined) {
    throw new Error(`the page holds ${what} ${found.length} times, so the export template cannot derive from it`);
  }
  return text.slice(0, only.index) + replacement + text.slice(only.index + only[0].length);
}

/** `text` inside a Jinja raw block, so a `{{`, `{%` or `{#` in a bundle is text to the template engine. */
function raw(text: string): string {
  if (/\{%-?\s*endraw/.test(text)) {
    throw new Error("the bundle's text closes a Jinja raw block, which the export template cannot carry");
  }
  return `{% raw %}${text}{% endraw %}`;
}

/**
 * The export template: the built page itself, with each script and
 * stylesheet Vite linked carried inline instead and the mount element
 * carrying the log. The one Jinja expression is `{{ log }}` in that
 * attribute, entity-escaped by the render; everything the bundle wrote sits
 * in raw blocks, so the page is Vite's skeleton and nothing is authored
 * elsewhere.
 */
export function exportTemplate(page: string, scripts: Emitted[], styles: Emitted[]): string {
  let template = page;
  for (const script of scripts) {
    template = replacedOnce(
      template,
      tagFor("script", "src", script.fileName, "></script>"),
      `the script tag loading ${script.fileName}`,
      `<script type="module">${raw(script.text)}</script>`,
    );
  }
  for (const style of styles) {
    template = replacedOnce(
      template,
      tagFor("link", "href", style.fileName, ">"),
      `the link tag loading ${style.fileName}`,
      `<style>${raw(style.text)}</style>`,
    );
  }
  return replacedOnce(
    template,
    /<div id="root"><\/div>/,
    'the mount element <div id="root"></div>',
    '<div id="root" data-lup-export="{{ log }}"></div>',
  );
}

/**
 * Every emitted script and stylesheet made safe to inline — the script
 * proven to parse — and, where the surface exports, `export.html.j2`
 * emitted beside `index.html`. The served page keeps no placeholder.
 *
 * The rewrite runs in `generateBundle`, on the text about to be written,
 * because minification runs after `renderChunk` and a minifier re-prints a
 * string literal with the escapes it prefers — `\!` among the ones it drops
 * — so an escape applied any earlier is gone before it reaches the disk.
 */
export function inlineSafe(exporting: boolean): Plugin {
  return {
    name: "lup:inline-safe",
    enforce: "post",
    generateBundle(_options, bundle) {
      const scripts: Emitted[] = [];
      const styles: Emitted[] = [];
      for (const output of Object.values(bundle)) {
        if (output.type === "chunk") {
          output.code = escapedScript(output.code, output.fileName);
          scripts.push({ fileName: output.fileName, text: output.code });
        } else if (output.fileName.endsWith(".css")) {
          if (typeof output.source !== "string") {
            throw new Error(`${output.fileName} is not text, which a text-only bundle cannot carry`);
          }
          output.source = escapedStyle(output.source);
          styles.push({ fileName: output.fileName, text: output.source });
        }
      }
      if (!exporting) return;
      const page = bundle["index.html"];
      if (page === undefined || page.type !== "asset" || typeof page.source !== "string") {
        throw new Error("the surface exports, but its build emitted no index.html to derive the export template from");
      }
      this.emitFile({
        type: "asset",
        fileName: "export.html.j2",
        source: exportTemplate(page.source, scripts, styles),
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const surface = mode === "production" || mode === "development" ? "explorer" : mode;
  return {
    root: resolve(__dirname, "src", surface),
    // Relative asset URLs, so a bundle serves from any prefix and an exported
    // single file needs no origin at all.
    base: "./",
    plugins: [react(), base64Files(), inlineSafe(EXPORTED.includes(surface))],
    build: {
      outDir: resolve(__dirname, "out", surface),
      emptyOutDir: true,
      // Text only: the Python side materialises bundles as text artifacts
      // with an ownership manifest, and a binary asset would fail that gate.
      // Small assets are inlined as data URLs by this limit; nothing here
      // ships a large one.
      assetsInlineLimit: 1 << 20,
      sourcemap: false,
      // A surface that exports is one script and one stylesheet, so its
      // export can carry it whole: a chunk loaded by relative URL has no
      // server to load it from once the page is a file. A served surface
      // splits what it imports dynamically into chunks of their own — the
      // dashboard's grammars, each a WASM carried inline as base64 text —
      // fetched from its `assets/` only once the page asks for one.
      rollupOptions: { output: { codeSplitting: !EXPORTED.includes(surface) } },
    },
  };
});
