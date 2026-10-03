import { describe, expect, test } from "bun:test";
import { HANDLERS } from "./actions";
import { COMMANDS } from "./commands";
import { CATALOG, Keymap, keyName, keyTokens, prettyKeys, Sequencer } from "./keys";

describe("the keymap", () => {
  test("every action the catalog declares has a handler, and every handler is a catalog action", () => {
    const declared = CATALOG.actions.map((action) => action.name).sort();
    expect(Object.keys(HANDLERS).sort()).toEqual(declared);
  });

  test("every action named beside another in the help exists, and every one is shown somewhere", () => {
    const names = new Set(CATALOG.actions.map((action) => action.name));
    const shown = new Set(CATALOG.actions.filter((action) => !action.hidden).flatMap((action) => [action.name, ...action.also]));
    for (const action of CATALOG.actions) {
      for (const also of action.also) expect(names.has(also)).toBe(true);
      expect(shown.has(action.name)).toBe(true);
    }
  });

  test("lup's own keys conflict nowhere: no two actions share a key in a mode and a place where both act", () => {
    const keymap = new Keymap();
    const clashes = keymap.bindings().flatMap((left, at, all) => all.slice(at + 1).filter((right) => {
      if (left.bound.action.name === right.bound.action.name || left.keys !== right.keys) return false;
      const modes = left.bound.action.mode === right.bound.action.mode || [left.bound.action.mode, right.bound.action.mode].includes("any");
      const windows = left.bound.action.focus === right.bound.action.focus || [left.bound.action.focus, right.bound.action.focus].includes("any");
      const places = CATALOG.scopes[right.bound.action.scope] ?? [];
      return modes && windows && (CATALOG.scopes[left.bound.action.scope] ?? []).some((place) => places.includes(place));
    }).map((right) => `${left.keys}: ${left.bound.action.name} and ${right.bound.action.name}`));
    expect(clashes).toEqual([]);
  });

  test("command names are each one command's, aliases included", () => {
    const spelled = COMMANDS.flatMap((command) => [command.name, ...(command.alias ?? [])]);
    expect(new Set(spelled).size).toBe(spelled.length);
  });

  test("a sequence reads as the dispatcher names its keys, and as the help speaks it", () => {
    expect(keyTokens("<leader>fa")).toEqual(["<leader>", "f", "a"]);
    expect(keyTokens("<C-Enter>")).toEqual(["<C-Enter>"]);
    expect(keyTokens("gd")).toEqual(["g", "d"]);
    expect(prettyKeys("<leader>am")).toBe("Space am");
    expect(prettyKeys("<C-Enter>")).toBe("Ctrl+Enter");
    expect(prettyKeys("<A-Up>")).toBe("Alt+↑");
    expect(prettyKeys("<A-Del>")).toBe("Alt+Delete");
    expect(prettyKeys("<leader><leader>")).toBe("Space Space");
  });

  test("a key event is named as the keymap writes keys: Space is the leader, Cmd+Enter is Ctrl+Enter", () => {
    const pressed = (key: string, held: Partial<KeyboardEvent> = {}) => keyName({ key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...held });
    expect(pressed(" ")).toBe("<leader>");
    expect(pressed("Enter", { metaKey: true })).toBe("<C-Enter>");
    expect(pressed("Delete", { altKey: true })).toBe("<A-Del>");
    expect(pressed("d", { ctrlKey: true })).toBe("<C-d>");
    expect(pressed("J", { shiftKey: true })).toBe("J");
    expect(pressed("ArrowUp", { altKey: true })).toBe("<A-Up>");
    expect(pressed("Tab", { shiftKey: true })).toBe("<S-Tab>");
    expect(pressed("Shift", { shiftKey: true })).toBeNull();
    expect(pressed("x", { altKey: true })).toBeNull();
  });

  test("the person's changes replace an action's keys, and say whose they are", () => {
    const keymap = new Keymap([{ action: "agent.next", keys: ["<A-Right>", ")"], origin: "config", what: "", why: "", way: "" }, { action: "tree.stopped", keys: [], origin: "tab", what: "", why: "", way: "" }]);
    expect(keymap.of("agent.next")?.keys).toEqual(["<A-Right>", ")"]);
    expect(keymap.of("agent.next")?.origin).toBe("config");
    expect(keymap.spoken("tree.stopped")).toBe("unbound");
    expect(keymap.spoken("help")).toBe("?, Space ?");
    const row = CATALOG.actions.find((action) => action.name === "agent.next");
    if (row === undefined) throw new Error("agent.next is a catalog action");
    expect(keymap.yours(row)).toBe(true);
    expect(keymap.row(row)).toBe("Alt+→, ) / (");
  });

  test("a binding acts only in the places its scope covers", () => {
    const keymap = new Keymap();
    expect(keymap.exact("normal", "]", { places: ["review"], buffer: true })?.action.name).toBe("file.next");
    expect(keymap.exact("normal", "]", { places: ["member"], buffer: true })).toBeUndefined();
    expect(keymap.exact("normal", "x", { places: ["inbox"], buffer: true })?.action.name).toBe("delete");
    expect(keymap.exact("any", "<C-Enter>", { places: ["member"], buffer: true })?.action.name).toBe("answer.approve");
    expect(keymap.exact("box", "j", { places: ["review"], buffer: true })?.action.name).toBe("box.next");
    expect(keymap.exact("normal", "r", { places: ["thread"], buffer: true })?.action.name).toBe("message.reply");
  });

  test("focus decides where keys act: the editor's motions only while the buffer has it, j/k wherever it is", () => {
    const keymap = new Keymap();
    const buffer = { places: ["review" as const], buffer: true };
    const tree = { places: ["review" as const], buffer: false };
    expect(keymap.exact("normal", "w", buffer)?.action.name).toBe("word.next");
    expect(keymap.exact("normal", "w", tree)).toBeUndefined();
    expect(keymap.exact("normal", "$", buffer)?.action.name).toBe("line.end");
    expect(keymap.exact("normal", "j", tree)?.action.name).toBe("down");
    expect(keymap.exact("any", "<Tab>", tree)?.action.name).toBe("focus.next");
    expect(keymap.exact("any", "<S-Tab>", buffer)?.action.name).toBe("focus.previous");
  });

  test("0 after a count is part of it, and alone goes to the line's start", () => {
    const sequencer = new Sequencer();
    const bindings = new Keymap().acting("normal", { places: ["review"], buffer: true });
    const alone = sequencer.press("0", bindings);
    expect(alone.kind === "run" && alone.bound.action.name).toBe("line.start");
    expect(sequencer.press("1", bindings).kind).toBe("count");
    expect(sequencer.press("0", bindings).kind).toBe("count");
    const down = sequencer.press("j", bindings);
    expect(down.kind === "run" && down.bound.action.name === "down" && down.count).toBe(10);
  });

  test("Normal mode reads a count, then a sequence; a key starting nothing is unknown", () => {
    const sequencer = new Sequencer();
    const bindings = new Keymap().acting("normal", { places: ["review"], buffer: true });
    expect(sequencer.press("3", bindings).kind).toBe("count");
    const step = sequencer.press("}", bindings);
    expect(step.kind === "run" && step.bound.action.name === "change.next" && step.count === 3 && step.counted).toBe(true);
    expect(sequencer.press("g", bindings)).toEqual({ kind: "pending", exact: null });
    expect(sequencer.shown()).toBe("g");
    const done = sequencer.press("d", bindings);
    expect(done.kind === "run" && done.bound.action.name === "definition").toBe(true);
    sequencer.press("g", bindings);
    const asked = sequencer.press("?", bindings);
    expect(asked.kind === "run" && asked.bound.action.name === "judged").toBe(true);
    expect(sequencer.press("<leader>", bindings).kind).toBe("pending");
    expect(sequencer.press("w", bindings).kind).toBe("pending");
    expect(sequencer.press("<BS>", bindings).kind).toBe("pending");
    expect(sequencer.typed).toEqual(["<leader>"]);
    expect(sequencer.continuations(bindings).some((binding) => binding.bound.action.name === "find.agent")).toBe(true);
    sequencer.reset();
    expect(sequencer.press("Q", bindings)).toEqual({ kind: "unknown", typed: "Q" });
  });
});
