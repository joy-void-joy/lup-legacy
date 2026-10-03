// The dashboard's one palette: every colour the page draws with, where it came
// from, and the backgrounds it is declared to sit on. The CSS custom properties
// are generated from this table (`paletteCss`), never typed by hand, and the
// palette's test fails if any text colour reads below 7:1 (WCAG AAA) on a
// background it sits on. `:contrast` shows the same table in the page.
//
// The colours come from two sources: Claude Code's colour-blind themes
// (`dark-daltonized`, `light-daltonized`, and its diff renderer's daltonized
// line colours, from @anthropic-ai/claude-code-linux-x64 2.1.285), and VS
// Code's high-contrast themes (`hc_black.json`, `hc_light.json` and the
// hcDark/hcLight workbench defaults, from microsoft/vscode main at
// afedf379e0fe234de0e46e97cabdc4b4485435f8). Where a sourced colour read below
// 7:1, it was moved along its own lightness, hue and saturation kept, until it
// reached 7:1; `source` keeps the value it started from.

export type Theme = "dark" | "light";

/** Where a token is drawn: a background, chrome drawn on the page, text that also sits on added and removed lines, or a statusline mode block. */
export type Reach = "ground" | "chrome" | "sign" | "text" | "mode";

/** One theme's value of a token: what ships, what the source said, and which source. */
export type Shade = { hex: string; source: string; from: string };

export type Token = {
  name: string;
  role: string;
  reach: Reach;
  dark: Shade;
  light: Shade;
};

const HC_DARK = "VS Code hcDark";
const HC_LIGHT = "VS Code hcLight";
const HCB = "hc_black.json";
const HCL = "hc_light.json";
const CC_DARK = "Claude Code dark-daltonized";
const CC_LIGHT = "Claude Code light-daltonized";
const CC_DIFF = "Claude Code diff renderer, daltonized";

/** A shade that ships as sourced. */
const kept = (hex: string, from: string): Shade => ({ hex, source: hex, from });
/** A shade moved along its lightness to reach 7:1, from the value its source gave. */
const moved = (hex: string, source: string, from: string): Shade => ({ hex, source, from });

export const PALETTE: Token[] = [
  { name: "bg", role: "the page", reach: "ground", dark: kept("#000000", `${HC_DARK} editor.background`), light: kept("#FFFFFF", `${HC_LIGHT} editor.background`) },
  { name: "float-bg", role: "floats, file headers", reach: "ground", dark: kept("#0C141F", `${HC_DARK} editorWidget.background`), light: kept("#FFFFFF", `${HC_LIGHT} editorWidget.background`) },
  { name: "add-bg", role: "added lines", reach: "ground", dark: kept("#001B29", `${CC_DIFF} addLine`), light: kept("#DBEDFF", `${CC_DIFF} addLine`) },
  { name: "del-bg", role: "removed lines", reach: "ground", dark: kept("#3D0100", `${CC_DIFF} deleteLine`), light: kept("#FFDCDC", `${CC_DIFF} deleteLine`) },
  { name: "border", role: "window and float borders", reach: "chrome", dark: kept("#6FC3DF", `${HC_DARK} contrastBorder`), light: kept("#0F4A85", `${HC_LIGHT} contrastBorder`) },
  { name: "focus", role: "focused window, cursor line", reach: "chrome", dark: kept("#F38518", `${HC_DARK} focusBorder, editor.lineHighlightBorder`), light: kept("#006BBD", `${HC_LIGHT} focusBorder`) },
  { name: "sel-bg", role: "visual selection", reach: "chrome", dark: kept("#F3F518", `${HC_DARK} editor.selectionBackground`), light: kept("#0F4A85", `${HC_LIGHT} editor.selectionBackground`) },
  { name: "sel-fg", role: "text on the selection", reach: "chrome", dark: kept("#000000", `${HC_DARK} editor.selectionForeground`), light: kept("#FFFFFF", `${HC_LIGHT} editor.selectionForeground`) },
  { name: "fg", role: "text", reach: "text", dark: kept("#FFFFFF", `${HC_DARK} editor.foreground`), light: kept("#292929", `${HC_LIGHT} foreground`) },
  { name: "muted", role: "secondary text", reach: "text", dark: kept("#B3B3B3", `${HC_DARK} descriptionForeground (foreground at 0.7)`), light: moved("#494949", "#696969", `${HC_LIGHT} descriptionForeground (foreground at 0.7)`) },
  { name: "link", role: "links, ids", reach: "text", dark: moved("#2FACFF", "#21A6FF", `${HC_DARK} textLink.foreground`), light: kept("#0F4A85", `${HC_LIGHT} textLink.foreground`) },
  { name: "ok", role: "approved, working, success, a conflict's ours", reach: "text", dark: moved("#53A9FF", "#3399FF", `${CC_DARK} success`), light: moved("#004E75", "#006699", `${CC_LIGHT} success`) },
  { name: "err", role: "declined, error", reach: "text", dark: moved("#FF7C7C", "#FF6666", `${CC_DARK} error`), light: moved("#822B2B", "#993333", `${CC_LIGHT} diffRemovedWord`) },
  { name: "warn", role: "pending, the judged extract, open notes, quiet", reach: "text", dark: kept("#FFCC00", `${CC_DARK} warning`), light: moved("#694102", "#895503", `${HC_LIGHT} editorWarning.foreground`) },
  { name: "info", role: "comments, idle, NORMAL", reach: "text", dark: kept("#99CCFF", `${CC_DARK} permission`), light: kept("#0F4A85", `${HC_LIGHT} contrastBorder`) },
  { name: "purple", role: "can't answer here, template markers, V-LINE, a conflict's ancestor", reach: "text", dark: moved("#B691FF", "#AF87FF", `${CC_DARK} autoAccept`), light: moved("#6700C2", "#8700FF", `${CC_LIGHT} autoAccept`) },
  { name: "cyan", role: "subagents, holds, deferred markers", reach: "text", dark: kept("#66CCCC", `${CC_DARK} cyan_FOR_SUBAGENTS_ONLY`), light: moved("#145062", "#185E73", `${HCL} support.class`) },
  { name: "orange", role: "rule exceptions, keys, lup, a conflict's theirs", reach: "text", dark: kept("#FF9933", `${CC_DARK} claude`), light: moved("#743A00", "#FF9933", `${CC_LIGHT} claude`) },
  { name: "add-sign", role: "gutter +", reach: "sign", dark: moved("#60A8CD", "#51A0C8", `${CC_DIFF} addDecoration`), light: kept("#24578A", `${CC_DIFF} addDecoration`) },
  { name: "del-sign", role: "gutter −", reach: "sign", dark: moved("#FF7373", "#FF6666", `${CC_DARK} error (the renderer's #DC5A5A reads 5.6:1)`), light: kept("#993333", `${CC_LIGHT} diffRemovedWord (the renderer's #CF222E reads 5.4:1)`) },
  { name: "chg-sign", role: "gutter ~", reach: "sign", dark: kept("#FFD370", `${HC_DARK} editorWarning.foreground`), light: moved("#7E4E03", "#895503", `${HC_LIGHT} editorWarning.foreground`) },
  { name: "s-kw", role: "syntax: keyword, storage", reach: "text", dark: moved("#6EAADC", "#569CD6", `${HCB} keyword`), light: kept("#0F4A85", `${HCL} keyword`) },
  { name: "s-ctl", role: "syntax: control flow", reach: "text", dark: moved("#CB92C6", "#C586C0", `${HCB} keyword.control`), light: moved("#911A0A", "#B5200D", `${HCL} keyword.control`) },
  { name: "s-str", role: "syntax: string", reach: "text", dark: moved("#D1977F", "#CE9178", `${HCB} string`), light: kept("#0F4A85", `${HCL} string`) },
  { name: "s-com", role: "syntax: comment", reach: "text", dark: moved("#89AF76", "#7CA668", `${HCB} comment`), light: moved("#494949", "#515151", `${HCL} comment`) },
  { name: "s-num", role: "syntax: number", reach: "text", dark: kept("#B5CEA8", `${HCB} constant.numeric`), light: moved("#075438", "#096D48", `${HCL} constant.numeric`) },
  { name: "s-fn", role: "syntax: function, decorator", reach: "text", dark: kept("#DCDCAA", `${HCB} entity.name.function`), light: moved("#5829B0", "#5E2CBC", `${HCL} entity.name.function`) },
  { name: "s-ty", role: "syntax: type, class", reach: "text", dark: kept("#4EC9B0", `${HCB} entity.name.type`), light: moved("#145062", "#185E73", `${HCL} entity.name.type`) },
  { name: "s-var", role: "syntax: variable, parameter", reach: "text", dark: kept("#9CDCFE", `${HCB} variable`), light: kept("#001080", `${HCL} variable`) },
  { name: "s-con", role: "syntax: True, False, None", reach: "text", dark: moved("#6EAADC", "#569CD6", `${HCB} constant.language`), light: kept("#0F4A85", `${HCL} constant.language`) },
  { name: "s-key", role: "syntax: JSON key", reach: "text", dark: kept("#D4D4D4", `${HCB} support.type.property-name`), light: moved("#044894", "#0451A5", `${HCL} support.type.property-name.json`) },
  { name: "mode-normal", role: "NORMAL block", reach: "mode", dark: kept("#99CCFF", `${CC_DARK} permission`), light: kept("#0F4A85", `${HC_LIGHT} contrastBorder`) },
  { name: "mode-insert", role: "INSERT block", reach: "mode", dark: kept("#3399FF", `${CC_DARK} success`), light: moved("#005E8E", "#006699", `${CC_LIGHT} success`) },
  { name: "mode-visual", role: "V-LINE block", reach: "mode", dark: kept("#AF87FF", `${CC_DARK} autoAccept`), light: moved("#7A00E7", "#8700FF", `${CC_LIGHT} autoAccept`) },
  { name: "mode-command", role: "COMMAND and FIND block", reach: "mode", dark: kept("#FFCC00", `${CC_DARK} warning`), light: moved("#7E4E03", "#895503", `${HC_LIGHT} statusBarItem.warningBackground`) },
];

/** The text a mode block carries, fixed per theme: black on the dark theme's light blocks, white on the light theme's dark ones. */
export const MODE_TEXT: Record<Theme, string> = { dark: "#000000", light: "#FFFFFF" };

/** One token's value in one theme. */
export function shade(name: string, theme: Theme): string {
  const token = PALETTE.find((each) => each.name === name);
  if (token === undefined) throw new Error(`the palette has no token ${name}`);
  return token[theme].hex;
}

/** The backgrounds a token is declared to sit on, by name: text sits on the page, floats, and added and removed lines; a gutter sign on the page and floats. */
export function grounds(token: Token): string[] {
  switch (token.reach) {
    case "text": return ["bg", "float-bg", "add-bg", "del-bg"];
    case "sign": return ["bg", "float-bg"];
    default: return [];
  }
}

/** sRGB relative luminance, as WCAG defines it. */
function luminance(hex: string): number {
  const value = Number.parseInt(hex.slice(1), 16);
  const [red = 0, green = 0, blue = 0] = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((channel) => {
    const unit = channel / 255;
    return unit <= 0.03928 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

/** The WCAG contrast ratio of two colours, 1 to 21. */
export function contrast(left: string, right: string): number {
  const [lighter = 0, darker = 0] = [luminance(left), luminance(right)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

/** How one token reads at worst on what it sits on, in one theme; null for a background or a border, which carry no text. */
export function worst(token: Token, theme: Theme, hex = token[theme].hex): number | null {
  if (token.reach === "mode") return contrast(hex, MODE_TEXT[theme]);
  const under = grounds(token);
  if (under.length === 0) return null;
  return Math.min(...under.map((ground) => contrast(hex, shade(ground, theme))));
}

function block(theme: Theme): string {
  const tokens = PALETTE.map((token) => `--${token.name}:${token[theme].hex};`).join("");
  const modes = PALETTE.filter((token) => token.reach === "mode").map((token) => `--${token.name}-fg:${MODE_TEXT[theme]};`).join("");
  return `${tokens}${modes}color-scheme:${theme}`;
}

/** The custom properties every rule in the stylesheet draws with: light on `:root`, dark where the system asks for it. */
export function paletteCss(): string {
  return `:root{${block("light")}}@media (prefers-color-scheme: dark){:root{${block("dark")}}}`;
}
