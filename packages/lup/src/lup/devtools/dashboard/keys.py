"""The dashboard's keys: every action the page answers, lup's keys for each, and a person's own.

Every binding the page dispatches is one action with a stable name. The page's
keymap, its help, which-key and its key finder are generated from the catalog
here (`keymap.json`, compiled beside the view schema), so none of them can
disagree with what a key does. A person rebinds actions by those names in
``[dashboard.keys]`` of their lup config, and each entry is checked against
the same catalog: by the dashboard, which hands every tab the keys in effect
and what it refused, and by `dashboard keys`, offline.

Keys are written in Vim's notation, as the help shows them: ``<C-d>`` is
Ctrl+d, ``<A-Del>`` Alt+Delete, ``<leader>fa`` Space then f then a, ``gd`` g
then d. A value replaces the action's keys, and ``[]`` unbinds it.

A bad entry refuses only itself and says why, in three parts — what was
written, why it cannot apply, and the way through — so an action renamed in a
later lup costs that one binding rather than every launch. Two actions
conflict when they share a key in modes and places that meet; the override
that made the conflict is refused and the action keeps lup's keys, never
shadowed.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.devtools.dashboard.live import Feature
from lup.formats.banner import COMMENT_FREE, REGENERATE_COMMAND
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.providers.user_config import UserConfigFile
from lup.workspace.paths import project_root

type KeyMode = Literal["normal", "any", "box"]
"""Where a binding acts: Normal mode; any mode, a box being typed in included
(a chord); or the empty note box the page landed in, which navigates until
the first character typed."""

type KeyPlace = Literal[
    "review", "member", "you", "repo", "inbox", "thread", "empty", "setup"
]
"""What the page has in view: a review, an agent, the operator's own row, a
repository's page, the inbox, a discussion, nothing yet, or a repository's setup."""

type KeyScope = Literal[
    "anywhere", "review", "buffer", "inbox", "notsetup", "notreview"
]
"""Where a binding acts, as the places it covers (:attr:`KeymapCatalog.scopes`)."""

type KeyFocus = Literal["any", "buffer"]
"""Which window a binding acts in: wherever focus is, or only the buffer."""

type KeyStanding = Literal["decided", "changed", "new"]
"""How lup's key stands against the page before the revamp: one the operator
chose, one that moved, or one that is new."""

type KeyOrigin = Literal["config", "tab"]
"""Where a binding came from: the person's config, or one tab's ``:map``."""


class DashboardAction(BaseModel, frozen=True):
    """One action the page answers: its name, lup's keys for it, and where it acts."""

    name: str
    """The stable name a person's ``[dashboard.keys]`` rebinds."""

    keys: list[str]
    """lup's keys, each a sequence in Vim's notation."""

    description: str
    group: str
    """The id of the help section it is listed under."""

    mode: KeyMode = "normal"
    scope: KeyScope = "anywhere"
    focus: KeyFocus = "any"
    stands: KeyStanding = "new"
    needs: Feature | None = None
    """The supervision the dashboard's server must serve for it to run, which
    the stream says it does (``served``); a page meeting a server that does
    not refuses it, naming the route."""

    also: list[str] = []
    """Actions shown on this one's help row, as a pair or a set of directions."""

    hidden: bool = False
    """Shown on another action's row (its ``also``) rather than a row of its own."""

    note: str = ""
    was: str = ""
    """The page's earlier key for it, where it moved."""


class KeymapGroup(BaseModel, frozen=True):
    """One section of the help, which-key and the key finder."""

    id: str
    title: str


class ReservedKey(BaseModel, frozen=True):
    """A key the browser keeps for itself, so the page never sees it."""

    keys: str
    does: str
    """What the browser does with it instead, as a refusal says."""


class SpokenKey(BaseModel, frozen=True):
    """How the help reads one key name or modifier: ``Up`` as ``↑``, ``C`` as ``Ctrl``."""

    key: str
    says: str


def action(
    name: str,
    keys: str | list[str],
    description: str,
    group: str,
    **settings: str | bool | list[str],
) -> DashboardAction:
    """One catalog entry, its keys given as one sequence or a list of them."""
    return DashboardAction.model_validate(
        {
            "name": name,
            "keys": [keys] if isinstance(keys, str) else keys,
            "description": description,
            "group": group,
            **settings,
        }
    )


class KeymapCatalog(BaseModel, frozen=True):
    """Every action the page answers, and the vocabulary its keys are checked with.

    Constructed bare, it is lup's own keymap: the page's every action with
    its keys before a person's. A person's ``[dashboard.keys]`` replaces keys
    by action name; a project serving the page with actions of its own
    passes a catalog naming them.
    """

    groups: list[KeymapGroup] = [
        KeymapGroup(id="answer", title="answer (any mode: the box or Normal)"),
        KeymapGroup(id="move", title="move"),
        KeymapGroup(id="box", title="the note box you land in, while it is empty"),
        KeymapGroup(id="search", title="search"),
        KeymapGroup(id="agent", title="act on an agent (Space a)"),
        KeymapGroup(id="you", title="you as a peer (Space p): one level down"),
        KeymapGroup(id="view", title="windows, views and tabs"),
        KeymapGroup(id="comment", title="comment and write"),
        KeymapGroup(id="editor", title="the editor"),
        KeymapGroup(id="find", title="find"),
        KeymapGroup(id="ui", title="toggles and tools"),
        KeymapGroup(id="help", title="help"),
    ]
    scopes: dict[KeyScope, list[KeyPlace]] = {
        "anywhere": [
            "review",
            "member",
            "you",
            "repo",
            "inbox",
            "thread",
            "empty",
            "setup",
        ],
        "review": ["review"],
        "buffer": ["member", "you", "repo", "inbox", "thread"],
        "inbox": ["inbox"],
        "notsetup": ["review", "member", "you", "repo", "inbox", "thread", "empty"],
        "notreview": ["member", "you", "repo", "inbox", "thread", "empty", "setup"],
    }
    named: list[str] = [
        "Enter", "Esc", "Del", "BS", "Up", "Down", "Left", "Right", "Tab",
        "PageUp", "PageDown", "Home", "End",
        *(f"F{number}" for number in range(1, 13)),
    ]  # fmt: skip
    """The keys written by name inside ``<…>``, as ``<Enter>`` or ``<C-PageDown>``."""

    typeless: list[str] = ["Tab", *(f"F{number}" for number in range(1, 13))]
    """The named keys a box never types, so they can act while one has focus."""

    reserved: list[ReservedKey] = [
        ReservedKey(keys="<C-w>", does="closes the tab"),
        ReservedKey(keys="<C-t>", does="opens a tab"),
        ReservedKey(keys="<C-n>", does="opens a window"),
        ReservedKey(keys="<C-q>", does="quits Firefox"),
        ReservedKey(keys="<C-Tab>", does="switches tabs"),
        ReservedKey(keys="<C-S-Tab>", does="switches tabs"),
    ]
    spoken_keys: list[SpokenKey] = [
        SpokenKey(key="Up", says="↑"),
        SpokenKey(key="Down", says="↓"),
        SpokenKey(key="Left", says="←"),
        SpokenKey(key="Right", says="→"),
        SpokenKey(key="Del", says="Delete"),
        SpokenKey(key="BS", says="Backspace"),
        SpokenKey(key="PageDown", says="PgDn"),
        SpokenKey(key="PageUp", says="PgUp"),
    ]
    spoken_modifiers: list[SpokenKey] = [
        SpokenKey(key="C", says="Ctrl"),
        SpokenKey(key="A", says="Alt"),
        SpokenKey(key="S", says="Shift"),
    ]
    actions: list[DashboardAction] = [
        # answering: the chords, from anywhere
        action("answer.approve", "<C-Enter>", "approve, from anywhere: the note box, a line comment, or Normal mode (Cmd+Enter on macOS)", "answer", mode="any", stands="decided"),
        action("answer.decline", "<A-Del>", "decline, from anywhere (the forward Delete key only; Alt+Backspace still deletes a word)", "answer", mode="any", stands="decided"),
        action("answer.send", "<A-Enter>", "send the note and line comments without deciding; beside an agent, send the message box", "answer", mode="any", stands="decided"),
        action("review.previous.typing", "<A-Up>", "previous or next review, from inside a box too; your draft stays with its review (in the inbox, the messages)", "move", mode="any", stands="decided", also=["review.next.typing"]),
        action("review.next.typing", "<A-Down>", "next review, from inside a box too", "move", mode="any", stands="decided", hidden=True),
        # the empty note box the page landed in: it navigates until you type
        action("box.next", "j", "next or previous review, landing in its box", "box", mode="box", also=["box.previous"]),
        action("box.previous", "k", "previous review, landing in its box", "box", mode="box", hidden=True),
        action("box.down", "<Down>", "scroll the diff a line down or up, the box keeping focus", "box", mode="box", also=["box.up"]),
        action("box.up", "<Up>", "scroll the diff a line up", "box", mode="box", hidden=True),
        action("box.halfdown", ["<C-d>", "<PageDown>"], "scroll the diff half a page down or up", "box", mode="box", also=["box.halfup"]),
        action("box.halfup", ["<C-u>", "<PageUp>"], "scroll the diff half a page up", "box", mode="box", hidden=True),
        # moving in Normal mode
        action("down", ["j", "<Down>"], "down or up where focus is: the tree's next row, opening it (a review, an agent); the buffer's next line; the context's next item", "move", stands="changed", was="j/k: the next and previous review from anywhere in Normal mode", also=["up"]),
        action("up", ["k", "<Up>"], "up where focus is", "move", stands="changed", hidden=True),
        action("focus.next", "<Tab>", "move focus on: the tree, the buffer, the box, the context, and round again (Shift+Tab back)", "move", mode="any", also=["focus.previous"]),
        action("focus.previous", "<S-Tab>", "move focus back", "move", mode="any", hidden=True),
        action("agent.next", ")", "next or previous agent in the tree", "move", scope="notsetup", also=["agent.previous"]),
        action("agent.previous", "(", "previous agent in the tree", "move", scope="notsetup", hidden=True),
        action("go.asker", "gs", "go to the agent that asked (from a review), or to its parent session (from an agent)", "move", scope="notsetup"),
        action("go.review", "gr", "go to a review the agent parked", "move", scope="buffer"),
        action("inbox", ["gi", "<leader>pi"], "your inbox: everything addressed to you", "move"),
        action("you", ["gu", "<leader>pu"], "your own row: what you said, hold and posted", "move"),
        # search
        action("search", "/", "search the focused window: incremental, smartcase; Enter keeps the match, Esc goes back", "search", was="/: find a line, fuzzy (now Space f /)"),
        action("search.next", ";", "next or previous match of the last search", "search", also=["search.previous"]),
        action("search.previous", ",", "previous match of the last search", "search", hidden=True),
        # acting on an agent
        action("agent.write", "<leader>am", "write to it (c does the same beside it)", "agent"),
        action("agent.reply", "<leader>ar", "reply in the thread of its last message", "agent", needs="reply-thread"),
        action("agent.wake", "<leader>aw", "wake it to read its mailbox", "agent", needs="bare-wake"),
        action("agent.nudge", "<leader>an", "interrupt its turn with the box's words (or the standard ones)", "agent", needs="interrupt"),
        action("agent.parent", "<leader>ap", "ask its parent session about it", "agent"),
        action("agent.transcript", ["T", "<leader>at"], "its whole transcript, live", "agent", scope="notreview", needs="transcript"),
        action("agent.rename", "<leader>aR", "rename it", "agent", needs="rename"),
        action("agent.stop", "<leader>ax", "stop it (twice confirms)", "agent", needs="stop"),
        action("message.reply", "r", "reply in the thread of the message under the cursor", "agent", scope="buffer", needs="reply-thread"),
        action("messages.earlier", "E", "load earlier messages (an older page of the mail record)", "agent", scope="buffer", stands="changed", note="was the Load earlier messages button"),
        # the operator as a peer
        action("peer.describe", "<leader>pd", "describe yourself: what you are on (:describe)", "you", needs="describe"),
        action("peer.lock", "<leader>pl", "lock the file under the cursor, or a path you type (:lock)", "you", needs="claims"),
        action("peer.release", "<leader>pL", "release a path you hold (:release)", "you", needs="claims"),
        action("peer.notice", "<leader>pn", "post a standing notice (:notice)", "you", needs="notices"),
        action("peer.broadcast", "<leader>pb", "broadcast to every working member (:broadcast)", "you"),
        action("peer.redirect", "<leader>pr", "redirect an agent: refuse its next call with your words (:redirect)", "you", needs="redirect"),
        # windows, views and tabs
        action("tree.all", "<leader>ta", "tree: every agent, only those that need you, or only the reviews waiting (as the old queue)", "view", also=["tree.attention", "tree.reviews"]),
        action("tree.attention", "<leader>tt", "tree: only the agents that need you", "view", hidden=True),
        action("tree.reviews", "<leader>tr", "tree: only the reviews waiting", "view", hidden=True),
        action("tree.stopped", "<leader>ts", "show or fold the stopped agents (folded by default; za on a repository's stopped row opens its own)", "view"),
        action("tab.next", "gt", "next or previous tab ({n}gt goes to tab n)", "view", also=["tab.previous"]),
        action("tab.previous", "gT", "previous tab", "view", hidden=True),
        action("tree.toggle", "<leader>e", "show or hide the agents tree (below 1280 px it takes the context's place)", "view", stands="changed", note="the tree replaces the queue"),
        action("context.toggle", "<leader>o", "show or hide the context window (below 1280 px it takes the tree's place)", "view"),
        action("split.vertical", "<leader>wv", "split: before | after side by side", "view", scope="review"),
        action("split.below", "<leader>ws", "split: a second window below", "view", scope="review"),
        action("split.close", ["<leader>wq", "<leader>wo"], "close the split", "view", scope="review"),
        action("window.next", "<leader>ww", "next window", "view"),
        action("window.left", "<leader>wh", "window to the left, right, below (the box) or above", "view", also=["window.right", "window.below", "window.above"]),
        action("window.right", "<leader>wl", "window to the right", "view", hidden=True),
        action("window.below", "<leader>wj", "window below (the box)", "view", hidden=True),
        action("window.above", "<leader>wk", "window above", "view", hidden=True),
        action("view.diff", "<leader>vd", "this window shows the diff", "view", scope="review"),
        action("view.before", "<leader>vb", "this window shows the file before", "view", scope="review"),
        action("view.after", "<leader>va", "this window shows the file after", "view", scope="review"),
        action("view.raw", "<leader>vu", "this window shows the unified diff as text", "view", scope="review"),
        action("close", "q", "close the float or the split", "view"),
        # comment and write
        action("delete", "x", "delete the draft comment here; beside an agent or in the inbox, mark read, release a hold, or withdraw a notice", "comment", scope="notsetup"),
        action("inbox.readall", "X", "mark every message to you read", "comment", scope="inbox", needs="inbox-read"),
        action("box", "c", "back into the box: the note on a review, the message box beside an agent", "comment", stands="decided"),
        action("escape", "<Esc>", "leave the box (Ctrl+[ too), cancel visual mode, close a float", "comment", stands="decided"),
        action("comment.line", ["i", "a", "o"], "comment on this line (a draft comment, GitHub-style)", "comment", scope="review"),
        action("open", "<Enter>", "comment here, open a fold, open what the cursor is on, or reply to a message", "comment"),
        action("visual", ["V", "v"], "visual line mode: pick a range with j/k, then gc, c or Enter comments on it", "comment", scope="review"),
        action("undo", "u", "restore the draft comment you deleted", "comment", scope="review"),
        # the editor
        action("left", ["h", "<Left>"], "the cursor a character left or right", "editor", focus="buffer", also=["right"]),
        action("right", ["l", "<Right>"], "a character right", "editor", focus="buffer", hidden=True),
        action("word.next", "w", "the start of the next word, the start of the previous one, or the end of this one", "editor", focus="buffer", also=["word.back", "word.end"]),
        action("word.back", "b", "the start of the previous word", "editor", focus="buffer", hidden=True),
        action("word.end", "e", "the end of the word", "editor", focus="buffer", hidden=True),
        action("line.start", "0", "the start of the line, its first non-blank, or its end", "editor", focus="buffer", also=["line.first", "line.end"]),
        action("line.first", "^", "the first non-blank of the line", "editor", focus="buffer", hidden=True),
        action("line.end", "$", "the end of the line", "editor", focus="buffer", hidden=True),
        action("page.halfdown", ["<C-d>", "<PageDown>"], "half a page down or up", "editor", also=["page.halfup"]),
        action("page.halfup", ["<C-u>", "<PageUp>"], "half a page up", "editor", hidden=True),
        action("top", "gg", "top or bottom of the window; {n}G or {n}gg goes to line n of the file", "editor", also=["bottom"]),
        action("bottom", "G", "bottom of the window", "editor", hidden=True),
        action("change.next", "}", "next or previous change or step in a review; the next section anywhere else", "editor", also=["change.previous"]),
        action("change.previous", "{", "previous change, step or section", "editor", hidden=True),
        action("file.next", "]", "next or previous file", "editor", scope="review", stands="decided", also=["file.previous"]),
        action("file.previous", "[", "previous file", "editor", scope="review", stands="decided", hidden=True),
        action("exception.next", "n", "next or previous rule exception", "editor", scope="review", stands="decided", also=["exception.previous"]),
        action("exception.previous", "p", "previous rule exception", "editor", scope="review", stands="decided", hidden=True),
        action("marker.next", "m", "next or previous `# lup:` marker, opening the whole file where it stands outside a hunk", "editor", scope="review", stands="decided", also=["marker.previous"]),
        action("marker.previous", "M", "previous `# lup:` marker", "editor", scope="review", stands="decided", hidden=True),
        action("file.whole", "f", "whole file in context, changes marked in place", "editor", scope="review", stands="decided"),
        action("review.full", "F", "full operation: automatic files and existing exceptions too", "editor", scope="review", stands="changed", note="was the Full operation button"),
        action("judged", "g?", "jump to what the policy asks about (the lines marked ?); again for the next part", "editor", scope="review", stands="changed", was="gd, which now goes to a definition"),
        action("fold.toggle", "za", "fold or unfold here (a file, a gap, the allowed steps, an agent or a stopped row in the tree)", "editor", also=["fold.open", "fold.close"]),
        action("fold.open", "zo", "unfold here", "editor", scope="review", hidden=True),
        action("fold.close", "zc", "fold here", "editor", scope="review", hidden=True),
        action("fold.all", "zR", "unfold everything, or fold every file", "editor", scope="review", also=["fold.none"]),
        action("fold.none", "zM", "fold every file", "editor", scope="review", hidden=True),
        action("hover", "K", "what is this: on code, the name under the cursor as its language server describes it (type, signature, docs); on anything else, what is attached to the line, file, step, message, agent or tree row", "editor", stands="changed", note="on code it asks the language server"),
        action("definition", "gd", "go to the definition of the name under the cursor, shown read-only in the buffer; Ctrl+o comes back", "editor", scope="review", focus="buffer"),
        action("references", "gr", "list every use of the name under the cursor in the context; Enter on one opens it", "editor", scope="review", focus="buffer"),
        action("jump.back", "<C-o>", "back to where gd or a reference jumped from", "editor", scope="review"),
        action("context.full", "I", "full context: a review's tool input and record, or an agent's whole row", "editor", stands="changed", note="was Details, stacked above the editor"),
        # find
        action("find.review", ["<leader><leader>", "<leader>fr"], "find a review, pending or answered", "find"),
        action("find.agent", ["<leader>fa", "<leader>aa"], "find an agent, in every repository", "find"),
        action("find.inbox", "<leader>fi", "find a message to you", "find"),
        action("find.thread", "<leader>ft", "find a discussion", "find"),
        action("find.history", "<leader>fh", "find in History", "find"),
        action("find.file", "<leader>ff", "find a file in this review", "find", scope="review"),
        action("find.line", "<leader>f/", "find a line in this buffer, fuzzy", "find"),
        action("find.message", "<leader>fm", "find any message", "find"),
        action("find.command", "<leader>fc", "find a command, the peer verbs included", "find"),
        action("find.key", "<leader>fk", "find a key", "find"),
        action("find.marker", "<leader>fx", "find a marker or exception", "find", scope="review"),
        # toggles and tools
        action("ui.wrap", "<leader>uw", "wrap long lines on or off", "ui"),
        action("ui.numbers", "<leader>un", "line numbers on or off", "ui"),
        action("ui.advance", "<leader>ua", "advance after a decision on or off", "ui", stands="changed", note="was the checkbox"),
        action("ui.dismiss", "<leader>ud", "dismiss the notices", "ui"),
        action("link", "<leader>y", "copy a link to this review", "ui", scope="review", stands="changed", note="was the Copy link button"),
        action("reconnect", "<leader>R", "reconnect", "ui", stands="changed", note="was the Reconnect button"),
        action("messages", "<leader>m", ":messages, what this page said", "ui"),
        # help
        action("help", ["?", "<leader>?"], "every key (this help)", "help", stands="decided"),
        action("cmdline", ":", "the command line, with Tab completion", "help"),
    ]  # fmt: skip

    def action(self, name: str) -> DashboardAction | None:
        """The action of that name, where the catalog has one."""
        return next((each for each in self.actions if each.name == name), None)

    def nearest(self, name: str) -> str:
        """The action whose name is fewest edits from *name*, for a misspelling."""
        return min(
            (each.name for each in self.actions),
            key=lambda known: edits(name, known),
            default="",
        )

    def kept(self, keys: str) -> ReservedKey | None:
        """The browser's own use of *keys*, where it keeps them."""
        return next((each for each in self.reserved if each.keys == keys), None)

    def says(self, key: str, spoken: list[SpokenKey]) -> str:
        """How the help reads *key*, by *spoken*; the key itself where it names none."""
        return next((each.says for each in spoken if each.key == key), key)

    def pretty(self, keys: str) -> str:
        """A sequence as the help reads it: ``Space am``, ``Ctrl+Enter``, ``Alt+↑``, ``gd``."""
        return "".join(self.spoken(token) for token in key_tokens(keys)).strip()

    def spoken(self, token: str) -> str:
        """One key of a sequence as the help reads it."""
        match token:
            case "<leader>":
                return "Space "
            case str() if len(token) > 1:
                key = NamedKey.read(token[1:-1])
                held = "".join(
                    f"{self.says(each, self.spoken_modifiers)}+"
                    for each in key.modifiers
                )
                return f"{held}{self.says(key.name, self.spoken_keys)}"
            case _:
                return token


def edits(left: str, right: str) -> int:
    """How many single-character edits turn *left* into *right*."""
    row = list(range(len(right) + 1))
    for at, char in enumerate(left, start=1):
        previous, row[0] = row[0], at
        for column, other in enumerate(right, start=1):
            kept = row[column]
            row[column] = min(
                row[column] + 1, row[column - 1] + 1, previous + (char != other)
            )
            previous = kept
    return row[len(right)]


class KeyNotationError(ValueError):
    """A key sequence that does not read: what is wrong with it, said exactly."""


def key_tokens(keys: str) -> list[str]:
    """The keys of one sequence as the dispatcher names them: ``<…>`` whole, any other character alone."""

    def walk() -> Iterator[str]:
        at = 0
        while at < len(keys):
            end = keys.find(">", at) if keys[at] == "<" else -1
            stop = end + 1 if end > at + 1 else at + 1
            yield keys[at:stop]
            at = stop

    return list(walk())


class NamedKey(BaseModel, frozen=True):
    """One ``<…>`` key read apart: the modifiers held, and the key's own name."""

    modifiers: list[str]
    name: str

    @classmethod
    def read(cls, inner: str) -> "NamedKey":
        """The key inside ``<…>``: each one-letter prefix ending in ``-`` is a modifier held."""
        match inner[:2], inner[2:]:
            case (str() as prefix, str() as rest) if prefix.endswith("-") and rest:
                within = cls.read(rest)
                return within.model_copy(
                    update={"modifiers": [prefix[0], *within.modifiers]}
                )
            case _:
                return cls(modifiers=[], name=inner)


def read_keys(written: str, catalog: KeymapCatalog) -> str:
    """One key sequence as written in a config, in the page's canonical spelling.

    Raises :class:`KeyNotationError` saying exactly what does not read: a
    modifier the page does not know, a key with no such name, a letter held
    with more than Ctrl, or a key the browser keeps for itself.
    """
    if written == "":
        raise KeyNotationError("an empty key; [] unbinds an action")
    canonical = "".join(
        read_token(token, written, catalog) for token in key_tokens(written)
    )
    kept = next(
        (found for token in key_tokens(canonical) if (found := catalog.kept(token))),
        None,
    )
    if kept is not None:
        raise KeyNotationError(
            f"{catalog.pretty(kept.keys)}: Firefox keeps it for itself (it "
            f"{kept.does}), so the page never sees it"
        )
    return canonical


def read_token(token: str, written: str, catalog: KeymapCatalog) -> str:
    """One key of a sequence, read: Space is the leader, a ``<…>`` key is read whole."""
    match token:
        case " ":
            return "<leader>"
        case "<":
            raise KeyNotationError(f"{written!r} opens < and never closes it")
        case str() if len(token) == 1:
            return token
        case _:
            return named_key(token[1:-1], catalog)


def named_key(inner: str, catalog: KeymapCatalog) -> str:
    """One ``<…>`` key, read: the leader, Ctrl with a letter, or a named key with its modifiers."""
    if inner in ("leader", "Space"):
        return "<leader>"
    key = NamedKey.read(inner)
    modifiers = key.modifiers
    repeated = any(modifiers.count(each) > 1 for each in modifiers)
    unknown = any(each not in ("C", "A", "S") for each in modifiers)
    if unknown or repeated or (len(key.name) > 1 and "-" in key.name):
        raise KeyNotationError(
            f"<{inner}>: modifiers are C- (Ctrl), A- (Alt) and S- (Shift), "
            "each once, before the key"
        )
    if len(key.name) == 1:
        return ctrl_key(inner, key)
    if key.name not in catalog.named:
        names = ", ".join(each for each in catalog.named if not each.startswith("F"))
        raise KeyNotationError(
            f"<{inner}>: no key is called {key.name}; the names are {names} and F1–F12"
        )
    ordered = [each for each in ("C", "A", "S") if each in modifiers]
    if ordered == ["S"] and key.name != "Tab":
        raise KeyNotationError(
            f"<{inner}>: Shift alone does not change {key.name} for the page; "
            f"<{key.name}> is the same key"
        )
    return f"<{''.join(f'{each}-' for each in ordered)}{key.name}>"


def ctrl_key(inner: str, key: NamedKey) -> str:
    """A letter or sign held with Ctrl alone, the one modifier the page reads with one."""
    if "C" not in key.modifiers:
        raise KeyNotationError(
            f"<{inner}>: a letter or sign is written bare (x, X, ?); only Ctrl "
            f"combines with one, as <C-{key.name.lower()}>"
        )
    if len(key.modifiers) > 1:
        raise KeyNotationError(
            f"<{inner}>: the page reads Ctrl with a letter, not with Alt or Shift as well"
        )
    return f"<C-{key.name.lower()}>"


def fits(entry: DashboardAction, keys: str, catalog: KeymapCatalog) -> str:
    """Why *keys* cannot run *entry* in the mode it acts in; nothing where it can.

    An action that works while a box is typed in takes keys that never type:
    one key held with Ctrl or Alt, Tab, or a function key. A box key is one
    keystroke.
    """
    tokens = key_tokens(keys)
    held = (
        NamedKey.read(tokens[0][1:-1])
        if len(tokens) == 1 and len(tokens[0]) > 1
        else None
    )
    typeless = held is not None and (
        any(each in ("C", "A") for each in held.modifiers)
        or held.name in catalog.typeless
    )
    if entry.mode == "any" and not typeless:
        return (
            f"{entry.name} works while you type, so each of its keys is one that never "
            "types: held with Ctrl or Alt (like <C-Enter>), Tab, or a function key; "
            f"{catalog.pretty(keys)} would type"
        )
    if entry.mode == "box" and len(tokens) != 1:
        return (
            f"{entry.name} acts in the empty note box, one keystroke at a time; "
            f"{catalog.pretty(keys)} is a sequence"
        )
    return ""


class KeyEntry(BaseModel, frozen=True):
    """One binding a person's table or a tab's ``:map`` made, applied or refused.

    A refusal says what was written, why it cannot apply, and the way
    through, each its own part, so a reader can act on it without parsing.
    """

    action: str
    keys: list[str]
    origin: KeyOrigin
    what: str = ""
    why: str = ""
    way: str = ""


class KeyReport(BaseModel, frozen=True):
    """What the person's bindings did: applied, refused, and the keys that wait."""

    applied: list[KeyEntry] = []
    refused: list[KeyEntry] = []
    waits: list[str] = []
    """Keys that start a longer one where both act, so each runs only after
    the wait for the rest."""


class KeyBindings(BaseModel, frozen=True):
    """The keys in effect for a tab: where they were read, what differs from lup's, and the report."""

    source: str = ""
    """The config the person's bindings were read from; empty where none was."""

    unread: str = ""
    """Why the config could not be read, where it could not: lup's keys stand."""

    changed: list[KeyEntry] = []
    """Every action whose keys differ from lup's, with the keys it runs on
    and where that came from."""

    report: KeyReport = KeyReport()


class KeyLine(BaseModel, frozen=True):
    """One line a tab tried: ``:map {action} {keys}``, or ``:unmap {keys}`` where no action is named."""

    action: str = ""
    keys: list[str]


class KeyTry(BaseModel, frozen=True):
    """The lines one tab tried, in order, to be checked over the person's own."""

    lines: list[KeyLine] = []


class Binding(BaseModel, frozen=True):
    """One key sequence running one action, as the dispatcher reads it."""

    action: DashboardAction
    keys: str


class Clash(BaseModel, frozen=True):
    """Two actions on one key where both act."""

    first: Binding
    second: Binding


class Held(BaseModel, frozen=True):
    """The keys one action runs on now, and who chose them: lup where the origin is empty."""

    keys: list[str]
    origin: KeyOrigin | None = None


class Bound:
    """The keys each action runs on while bindings are applied: lup's, then the person's, then a tab's."""

    def __init__(self, catalog: KeymapCatalog) -> None:
        self.catalog = catalog
        self.held = {each.name: Held(keys=list(each.keys)) for each in catalog.actions}
        self.applied: list[KeyEntry] = []
        self.refused: list[KeyEntry] = []

    def take(self, name: str, listed: list[str], origin: KeyOrigin) -> None:
        """Bind *listed* to the action *name*, or refuse it saying why."""
        entry = self.catalog.action(name)
        if entry is None:
            nearest = self.catalog.nearest(name)
            way = f"did you mean {nearest}?" if nearest else ""
            self.refuse(
                name, listed, origin, what=name, why="no action has that name", way=way
            )
            return
        try:
            keys = [read_keys(each, self.catalog) for each in listed]
        except KeyNotationError as unreadable:
            self.refuse(
                name, listed, origin, what=", ".join(listed), why=str(unreadable)
            )
            return
        misfit = next(
            (reason for each in keys if (reason := fits(entry, each, self.catalog))), ""
        )
        if misfit:
            self.refuse(name, listed, origin, what=", ".join(listed), why=misfit)
            return
        self.bind(name, list(dict.fromkeys(keys)), origin)

    def unmap(self, written: str, origin: KeyOrigin) -> None:
        """Take *written* off whatever action holds it."""
        try:
            keys = read_keys(written, self.catalog)
        except KeyNotationError as unreadable:
            self.refuse("", [written], origin, what=written, why=str(unreadable))
            return
        holder = next(
            (name for name, held in self.held.items() if keys in held.keys), None
        )
        if holder is None:
            way = "`:map` lists what each key runs"
            self.refuse(
                "", [written], origin, what=written, why="no action runs on it", way=way
            )
            return
        kept = [each for each in self.held[holder].keys if each != keys]
        self.bind(holder, kept, origin)

    def bind(self, name: str, keys: list[str], origin: KeyOrigin) -> None:
        """*name* runs on *keys* now, as *origin* chose."""
        self.held[name] = Held(keys=keys, origin=origin)
        others = [each for each in self.applied if each.action != name]
        self.applied = [*others, KeyEntry(action=name, keys=keys, origin=origin)]

    def refuse(
        self,
        name: str,
        keys: list[str],
        origin: KeyOrigin,
        *,
        what: str,
        why: str,
        way: str = "",
    ) -> None:
        """Record why *keys* cannot run *name*; the action keeps what it had."""
        refusal = KeyEntry(
            action=name, keys=keys, origin=origin, what=what, why=why, way=way
        )
        self.refused.append(refusal)

    def bindings(self) -> list[Binding]:
        """Every key sequence each action runs on now."""
        return [
            Binding(action=entry, keys=keys)
            for entry in self.catalog.actions
            for keys in self.held[entry.name].keys
        ]

    def meet(self, left: DashboardAction, right: DashboardAction) -> bool:
        """Whether two actions act in a mode, a window and a place in common."""
        modes = left.mode == right.mode or "any" in (left.mode, right.mode)
        windows = left.focus == right.focus or "any" in (left.focus, right.focus)
        places = self.catalog.scopes[right.scope]
        return (
            modes
            and windows
            and any(place in places for place in self.catalog.scopes[left.scope])
        )

    def conflict(self) -> Clash | None:
        """Two actions on one key where both act, the first such pair; nothing where there is none."""
        bound = self.bindings()
        return next(
            (
                Clash(first=left, second=right)
                for at, left in enumerate(bound)
                for right in bound[at + 1 :]
                if left.action.name != right.action.name
                and left.keys == right.keys
                and self.meet(left.action, right.action)
            ),
            None,
        )

    def settle(self) -> None:
        """Refuse the override behind each conflict until none is left.

        The refused action keeps lup's keys, which can surface another
        conflict, so this runs until the bindings agree. lup's own side
        never gives way; where both sides were overridden, the one later in
        the catalog does.
        """
        names = [each.name for each in self.catalog.actions]

        def standing(binding: Binding) -> int:
            overridden = self.held[binding.action.name].origin is not None
            return int(overridden) * len(names) + names.index(binding.action.name)

        for clash in iter(self.conflict, None):
            other, loser = sorted((clash.first, clash.second), key=standing)
            name = loser.action.name
            origin = self.held[name].origin or "config"
            self.refuse(
                name,
                self.held[name].keys,
                origin,
                what=self.catalog.pretty(loser.keys),
                why=f"it is {other.action.name}'s where both act",
                way=f'unbind it there ("{other.action.name}" = []) or pick another key',
            )
            self.applied = [each for each in self.applied if each.action != name]
            self.held[name] = Held(keys=list(loser.action.keys))

    def waits(self) -> list[str]:
        """Each Normal-mode key that starts a longer one where both act."""
        bound = [each for each in self.bindings() if each.action.mode == "normal"]
        pretty = self.catalog.pretty
        return list(
            dict.fromkeys(
                f"{pretty(short.keys)} ({short.action.name}) also starts "
                f"{pretty(long.keys)} ({long.action.name}): it runs after a "
                "700 ms wait for the rest"
                for short in bound
                for long in bound
                if starts(long.keys, short.keys)
                and self.meet(short.action, long.action)
            )
        )

    def changed(self) -> list[KeyEntry]:
        """Every action whose keys differ from lup's, and who chose them."""
        return [
            KeyEntry(action=entry.name, keys=held.keys, origin=held.origin)
            for entry in self.catalog.actions
            if (held := self.held[entry.name]).origin is not None
            and held.keys != entry.keys
        ]


def starts(longer: str, shorter: str) -> bool:
    """Whether *shorter* is a key sequence *longer* begins with, and not all of it."""
    long, short = key_tokens(longer), key_tokens(shorter)
    return len(short) < len(long) and long[: len(short)] == short


def effective(
    overrides: Mapping[str, list[str]],
    tried: Sequence[KeyLine] = (),
    *,
    source: str = "",
    unread: str = "",
    catalog: KeymapCatalog | None = None,
) -> KeyBindings:
    """The keys in effect: lup's, then the person's *overrides*, then what one tab *tried*.

    Every entry is checked against *catalog*, lup's own where none is
    named; one that cannot apply is refused and reported, and the rest take
    effect.
    """
    bound = Bound(catalog or KeymapCatalog())
    for name, listed in overrides.items():
        bound.take(name, listed, "config")
    for line in tried:
        if line.action:
            bound.take(line.action, line.keys, "tab")
            continue
        for each in line.keys:
            bound.unmap(each, "tab")
    bound.settle()
    report = KeyReport(
        applied=bound.applied, refused=bound.refused, waits=bound.waits()
    )
    return KeyBindings(
        source=source, unread=unread, changed=bound.changed(), report=report
    )


class ConfigStamp(BaseModel, frozen=True):
    """What a config file's status says of its content: its identity, size and times."""

    inode: int = 0
    size: int = 0
    modified: int = 0
    changed: int = 0
    present: bool = False

    @classmethod
    def of(cls, path: Path) -> "ConfigStamp":
        """The stamp *path* carries now, or the stamp of no file."""
        try:
            status = path.stat()
        except OSError:
            return cls()
        return cls(
            inode=status.st_ino,
            size=status.st_size,
            modified=status.st_mtime_ns,
            changed=status.st_ctime_ns,
            present=True,
        )


class DashboardKeys:
    """A person's keys as ``[dashboard.keys]`` says now, read again only where the file moved.

    Asked by the stream on every look, so a saved file reaches every open tab
    with nothing to reload; a look costs one stat. A config lup cannot read
    binds nothing of the person's: lup's keys stand, and ``unread`` says why.
    """

    def __init__(
        self,
        config: UserConfigFile | None = None,
        catalog: KeymapCatalog | None = None,
    ) -> None:
        self.config = config if config is not None else UserConfigFile()
        self.catalog = catalog or KeymapCatalog()
        self.stamp: ConfigStamp | None = None
        self.read = KeyBindings()

    def __call__(self) -> KeyBindings:
        stamp = ConfigStamp.of(self.config.path())
        if self.stamp != stamp:
            self.stamp, self.read = stamp, self.tried([])
        return self.read

    def tried(self, lines: Sequence[KeyLine]) -> KeyBindings:
        """The person's keys with a tab's *lines* tried over them, as the config reads now."""
        path = self.config.path()
        try:
            keys = self.config.load().dashboard.keys
        except (OSError, ValueError) as unreadable:
            return effective(
                {},
                lines,
                source=str(path),
                unread=str(unreadable),
                catalog=self.catalog,
            )
        source = str(path) if path.is_file() else ""
        return effective(keys, lines, source=source, catalog=self.catalog)


def keymap_json(catalog: KeymapCatalog | None = None) -> str:
    """The catalog as the page compiles it in, lup's own where none is named."""
    dumped = (catalog or KeymapCatalog()).model_dump(mode="json")
    return json.dumps(dumped, indent=2, ensure_ascii=False) + "\n"


def write_keymap(
    destination: Path,
    root: Path | None = None,
    *,
    catalog: KeymapCatalog | None = None,
    check: bool = False,
) -> Path:
    """Write or verify the keymap the page's table is compiled from."""
    artifact = Artifact(
        path=destination,
        content=keymap_json(catalog),
        semantic_id="web.dashboard-keymap",
        banner=COMMENT_FREE.compiled_from(__name__),
    )
    return write_generated_file(
        artifact, root or project_root(), REGENERATE_COMMAND, check=check
    )
