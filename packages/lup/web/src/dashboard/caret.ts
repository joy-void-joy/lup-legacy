// The buffer's cursor has a column as well as a line (decision 119). What a
// line holds is the text its row shows — its body, without the gutter, the
// line numbers or the virtual text an editor draws after a line — so the
// column, the motions over it and the caret drawn on it all read the same
// characters the operator sees.

/** The part of a row that holds its text. */
export const bodyOf = (row: Element | null | undefined): Element | null => row?.querySelector(".tx, .cmbox") ?? null;

/** The text nodes of a row's body, in order, leaving out the virtual text after it. */
function textNodes(body: Element): Text[] {
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => node.parentElement?.closest(".vt, textarea, .more") !== null ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) if (node instanceof Text) nodes.push(node);
  return nodes;
}

/** What a row's line holds, as the cursor moves over it. */
export function lineText(row: Element | null | undefined): string {
  const body = bodyOf(row);
  return body === null ? "" : textNodes(body).map((node) => node.data).join("");
}

/** The last column a line's cursor can stand on. */
export const lastColumn = (text: string) => Math.max(0, text.length - 1);

/** Where a pointer lands in a row, as a column of its line. */
export function columnAt(row: Element, x: number, y: number): number {
  const body = bodyOf(row);
  if (body === null) return 0;
  const located = document.caretPositionFromPoint?.(x, y);
  const legacy = located === undefined || located === null ? (document as Document & { caretRangeFromPoint?: (x: number, y: number) => Range | null }).caretRangeFromPoint?.(x, y) : null;
  const node = located?.offsetNode ?? legacy?.startContainer ?? null;
  const offset = located?.offset ?? legacy?.startOffset ?? 0;
  if (node === null || !body.contains(node)) return 0;
  let seen = 0;
  for (const each of textNodes(body)) {
    if (each === node) return seen + offset;
    seen += each.data.length;
  }
  return 0;
}

/** Where to draw the caret on a row: the character at a column, its box on screen, and the font it is set in. */
export function caretAt(row: Element, column: number): { char: string; rect: DOMRect; font: string } | null {
  const body = bodyOf(row);
  if (body === null) return null;
  let seen = 0;
  const nodes = textNodes(body);
  for (const node of nodes) {
    if (column < seen + node.data.length) {
      const range = document.createRange();
      range.setStart(node, column - seen);
      range.setEnd(node, column - seen + 1);
      const rects = range.getClientRects();
      const rect = rects[rects.length - 1] ?? range.getBoundingClientRect();
      const char = node.data[column - seen] ?? " ";
      return { char: char === "\n" ? " " : char, rect, font: getComputedStyle(node.parentElement ?? body).font };
    }
    seen += node.data.length;
  }
  // Past the end, or on an empty line: one cell after the last character, as an editor shows it.
  const last = nodes[nodes.length - 1];
  const range = document.createRange();
  if (last === undefined) range.selectNodeContents(body);
  else range.setStart(last, last.data.length);
  range.collapse(last === undefined);
  const rect = range.getBoundingClientRect();
  const font = getComputedStyle(last?.parentElement ?? body).font;
  return { char: " ", rect: new DOMRect(rect.left, rect.top, 0, rect.height || Number.parseFloat(getComputedStyle(body).lineHeight) || 18), font };
}

/** Vim's small words: a run of letters, digits and underscores, or a run of other non-blanks. */
const WORD = /[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]+/gu;

/** Each word of a line, as the columns it starts and ends on. */
export const words = (text: string) => [...text.matchAll(WORD)].map((found) => ({ start: found.index, end: found.index + found[0].length - 1 }));
