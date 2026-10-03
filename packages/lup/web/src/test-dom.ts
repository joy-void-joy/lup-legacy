// The browser every test runs in. A surface mounts into a document and reads
// a window, and the sandbox has neither, so happy-dom's are registered on the
// global object before any test file loads — `bunfig.toml` names this file as
// the suite's preload. React is told this is an `act` environment, so a state
// update landing outside one is reported rather than silently deferred. A
// frame a page embeds is its own server's page, which no test serves, so frames
// are placed and never loaded. The dashboard's grammars are read from the
// packages they ship in, as the build reads them, since what Vite makes of a
// WASM is a chunk no test has.
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { setGrammarSource } from "./dashboard/highlight";

GlobalRegistrator.register({
  url: "http://localhost/",
  settings: { disableIframePageLoading: true },
});
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const modules = new URL("../node_modules/", import.meta.url);
const bytes = async (path: string) => new Uint8Array(await Bun.file(new URL(path, modules)).arrayBuffer());
setGrammarSource({
  runtime: () => bytes("web-tree-sitter/web-tree-sitter.wasm"),
  wasm: (name) => bytes(`tree-sitter-wasm/out/${name}/tree-sitter-${name}.wasm`),
  query: (path) => Bun.file(new URL(`tree-sitter-wasm/out/${path}.scm`, modules)).text(),
});
