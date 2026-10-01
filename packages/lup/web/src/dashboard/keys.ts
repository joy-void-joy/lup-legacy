// The page's keys, read off one table: the action catalog the library declares
// (`lup.devtools.dashboard.keys`), compiled in as `schema/keymap.json`. The
// dispatcher, which-key, the `?` help and the key finder all read the keymap
// built here, so none of them can say a key does something it does not.
//
// A person's `[dashboard.keys]` reaches the page as the stream's `keys`: every
// action whose keys differ from lup's, checked by the server against the same
// catalog. A tab's `:map` lines are checked the same way, by the server, and
// the keymap is rebuilt from what it answers.
import catalogJson from "../../schema/keymap.json";
import type { DashboardAction, KeyEntry, KeymapCatalog, KeyPlace } from "../generated/views";

/** The library's action catalog, as generation wrote it from its typed source. */
export const CATALOG = catalogJson as KeymapCatalog;

export type KeyMode = DashboardAction["mode"];
export type Origin = "lup" | "config" | "tab";

/** One action as the dispatcher holds it: its catalog entry, the keys it runs on now, and who chose them. */
export type Bound = { action: DashboardAction; keys: string[]; origin: Origin };

/** One key sequence running one action. */
export type Binding = { bound: Bound; keys: string; tokens: string[] };

/** Where the page is, as a binding's scope and focus read it: what is in view, and whether the buffer has focus. */
export type Where = { places: KeyPlace[]; buffer: boolean };

/** The keys of one sequence as the dispatcher names them: `<…>` whole, any other character alone. */
export function keyTokens(keys: string): string[] {
  const tokens: string[] = [];
  for (let at = 0; at < keys.length;) {
    const end = keys[at] === "<" ? keys.indexOf(">", at) : -1;
    const stop = end > at + 1 ? end + 1 : at + 1;
    tokens.push(keys.slice(at, stop));
    at = stop;
  }
  return tokens;
}

function says(key: string, spoken: { key: string; says: string }[]): string {
  return spoken.find((each) => each.key === key)?.says ?? key;
}

/** A sequence as the help reads it: `Space am`, `Ctrl+Enter`, `Alt+↑`, `gd`. */
export function prettyKeys(keys: string): string {
  return keyTokens(keys).map((token) => {
    if (token === "<leader>") return "Space ";
    if (token.length === 1) return token;
    const parts = token.slice(1, -1).split("-");
    const name = token.endsWith("-->") ? "-" : parts.pop() ?? "";
    const held = parts.filter((part) => part !== "").map((part) => `${says(part, CATALOG.spoken_modifiers)}+`).join("");
    return `${held}${says(name, CATALOG.spoken_keys)}`;
  }).join("").trim();
}

/** What the page calls a named key the browser reports. */
const NAMED: Record<string, string> = {
  Enter: "Enter", Escape: "Esc", Delete: "Del", Backspace: "BS", ArrowUp: "Up", ArrowDown: "Down",
  ArrowLeft: "Left", ArrowRight: "Right", Tab: "Tab", PageUp: "PageUp", PageDown: "PageDown", Home: "Home", End: "End",
  F1: "F1", F2: "F2", F3: "F3", F4: "F4", F5: "F5", F6: "F6", F7: "F7", F8: "F8", F9: "F9", F10: "F10", F11: "F11", F12: "F12",
};

/** The keyboard as a key event carries it. */
export type Pressed = Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">;

/**
 * One key event as the keymap names it, or null for a key that is only a
 * modifier. Space is the leader; Cmd on macOS is Ctrl for Enter; a letter held
 * with Ctrl is `<C-x>`, one held with Alt is none of the page's.
 */
export function keyName(event: Pressed): string | null {
  if (["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead", "Unidentified"].includes(event.key)) return null;
  const named = NAMED[event.key];
  if (event.metaKey && named !== "Enter") return null;
  if (event.key === " " && !event.ctrlKey && !event.altKey) return "<leader>";
  if (named !== undefined) {
    const shift = event.shiftKey && (named === "Tab" || event.ctrlKey || event.metaKey || event.altKey) ? "S-" : "";
    return `<${event.ctrlKey || event.metaKey ? "C-" : ""}${event.altKey ? "A-" : ""}${shift}${named}>`;
  }
  if (event.key.length !== 1) return null;
  if (event.ctrlKey) return `<C-${event.key.toLowerCase()}>`;
  if (event.altKey) return null;
  return event.key;
}

/** The keymap in effect: lup's keys, with the person's and a tab's changes over them. */
export class Keymap {
  readonly bound: Bound[];

  constructor(changed: KeyEntry[] = [], readonly catalog: KeymapCatalog = CATALOG) {
    const mine = new Map(changed.map((entry) => [entry.action, entry]));
    this.bound = catalog.actions.map((action) => {
      const entry = mine.get(action.name);
      return entry === undefined ? { action, keys: action.keys, origin: "lup" } : { action, keys: entry.keys, origin: entry.origin };
    });
  }

  of(name: string): Bound | undefined {
    return this.bound.find((each) => each.action.name === name);
  }

  /** Every key sequence each action runs on now. */
  bindings(): Binding[] {
    return this.bound.flatMap((bound) => bound.keys.map((keys) => ({ bound, keys, tokens: keyTokens(keys) })));
  }

  /** Whether an action acts where the page is: its scope covers a place in view, in a window it acts in. */
  acts(action: DashboardAction, where: Where): boolean {
    const covered = this.catalog.scopes[action.scope] ?? [];
    return (action.focus === "any" || where.buffer) && where.places.some((place) => covered.includes(place));
  }

  /** The bindings of one mode that act where the page is. */
  acting(mode: KeyMode, where: Where): Binding[] {
    return this.bindings().filter((binding) => binding.bound.action.mode === mode && this.acts(binding.bound.action, where));
  }

  /** The action one exact key runs in a mode, where any does. */
  exact(mode: KeyMode, keys: string, where: Where): Bound | undefined {
    return this.acting(mode, where).find((binding) => binding.keys === keys)?.bound;
  }

  /** How an action's keys read, `unbound` where it has none. */
  spoken(name: string): string {
    const bound = this.of(name);
    if (bound === undefined) return "";
    return bound.keys.length > 0 ? bound.keys.map(prettyKeys).join(", ") : "unbound";
  }

  /** How a help row reads its keys: the action's, then each action shown beside it. */
  row(action: DashboardAction): string {
    return [action.name, ...action.also].map((name) => this.spoken(name)).filter((each) => each !== "").join(" / ");
  }

  /** Whether the person changed the keys of an action or any shown beside it. */
  yours(action: DashboardAction): boolean {
    return [action.name, ...action.also].some((name) => (this.of(name)?.origin ?? "lup") !== "lup");
  }
}

/** What one key pressed in Normal mode came to. */
export type Step =
  | { kind: "count" }
  | { kind: "pending"; exact: Bound | null }
  | { kind: "run"; bound: Bound; count: number; counted: boolean }
  | { kind: "unknown"; typed: string };

/**
 * Normal mode's keys in flight: a count, then a sequence the keymap answers.
 * A key that completes one binding and starts no other runs at once; one that
 * completes a binding and also starts a longer one waits `wait` for the rest,
 * then runs. Backspace takes the last key back.
 */
export class Sequencer {
  typed: string[] = [];
  count = "";

  /** The sequence as the statusline shows what awaits more. */
  shown(): string {
    return `${this.count}${this.typed.join("").replaceAll("<leader>", "␣")}`;
  }

  reset(): void {
    this.typed = [];
    this.count = "";
  }

  /** The next keys that continue what was typed, each with the action it runs or the group it opens. */
  continuations(bindings: Binding[]): Binding[] {
    return bindings.filter((binding) => binding.tokens.length > this.typed.length && this.typed.every((key, index) => binding.tokens[index] === key));
  }

  press(key: string, bindings: Binding[]): Step {
    if (this.typed.length === 0 && (/^[1-9]$/.test(key) || (this.count !== "" && key === "0"))) {
      this.count += key;
      return { kind: "count" };
    }
    if (key === "<BS>" && this.typed.length > 0) {
      this.typed.pop();
      return { kind: "pending", exact: null };
    }
    this.typed.push(key);
    const candidates = bindings.filter((binding) => this.typed.every((typed, index) => binding.tokens[index] === typed));
    if (candidates.length === 0) {
      const typed = this.typed.join("");
      this.reset();
      return { kind: "unknown", typed };
    }
    const exact = candidates.find((binding) => binding.tokens.length === this.typed.length);
    if (exact !== undefined && candidates.length === 1) return this.complete(exact.bound);
    return { kind: "pending", exact: exact?.bound ?? null };
  }

  /** Run what was typed: the binding it completed, with the count typed before it. */
  complete(bound: Bound): Step {
    const counted = this.count !== "";
    const count = Math.max(1, Number(this.count || "1"));
    this.reset();
    return { kind: "run", bound, count, counted };
  }
}
