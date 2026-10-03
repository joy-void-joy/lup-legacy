"""A spawned agent goes out named, so what lists, messages or stops it says what it is for.

A runtime shows a subagent by the name it was spawned with and otherwise by
its type, and a type is generic by construction: a session with three
general-purpose subagents running has three rows saying the same thing. The
name is also the address a message or a stop takes, so a spawn without one
is one nobody reaches except by the id the runtime minted.

The rule is presence and spelling, and both are settled here rather than
asked of the caller. A name given in the declared shape goes as given, since
which words make a good name is the caller's judgement. A name outside it is
normalized rather than refused, because leaving the spelling to the runtime
was measured to fail quietly — one rejects a name it dislikes with no hook
record at all — and a refusal only teaches the caller the shape by retry.
A spawn given no name takes one read the same way out of its description,
because the schema one runtime shows the model lists no name to pass.
The declaration names the characters rather than this module, since the safe
set is a property of the runtimes a project runs on.

Only a spawn with nothing to read a name from is refused. Every other is
deferred rather than allowed, because this kernel grants nothing it was not
asked to grant: the runtime's own permissions still settle a call it says
nothing more about, and the name rides beside that deferral as a rewrite of
the call's arguments.

A name read out of a description is a summary of what was asked, not what
anybody would call the work, so the caller is told afterwards to choose its
own: once the spawn has gone out, rather than as a refusal before it, which
was the friction the reading exists to spare.

Whether to spawn at all is the project's declared refusals, asked before the
name: the one place a project says it has decided against a call. A runtime
spelling a spawn that takes no name is judged the same way and asked for none.
"""

from collections.abc import Sequence

from .decision import KernelDecision
from .rows import RefusedToolRow, SpawnNameRow
from .tools import TOOL_ESCALATE_HINT, decide_tool, escalated_reason


def spawn_name(given: str, description: str, row: SpawnNameRow | None) -> str:
    """The name a spawn goes out under, or ``""`` where none can be read.

    A given name of the declared shape wins as given. Anything else — a name
    outside the shape, a blank one, none at all — is read into the shape:
    lowercased, each run of characters other than ASCII letters and digits
    joined by the first mark ``punctuation`` lists, and cut at a word to fit
    the limit, so it opens with a letter or digit by construction. The given
    name is read first and the description after it, so a name with any
    letter or digit in it is still the caller's words.

    ``None`` for the row is a project that requires no name, whose spawns
    go out exactly as written.

    Reserved words a runtime refuses whatever their shape — Claude Code's
    ``main``, ``user``, ``system`` — are left to that runtime's validator,
    which says so where the caller reads it rather than failing quietly.
    """
    if row is None:
        return given
    joiner = next(iter(row["punctuation"]), "")

    def alphanumeric(character: str) -> bool:
        """A letter or digit in ASCII, which is what both validators read.

        Narrower than ``str.isalnum``, deliberately: a name of letters no
        runtime here would accept is not made acceptable by Python agreeing
        that they are letters.
        """
        return character.isascii() and character.isalnum()

    def spelled(named: str) -> bool:
        """Whether the name is already the shape the declaration accepts."""
        return (
            0 < len(named) <= row["limit"]
            and alphanumeric(named[0])
            and all(
                alphanumeric(character) or character in row["punctuation"]
                for character in named
            )
        )

    def normalized(text: str) -> str:
        """The text read into the declared shape, whole words while they fit."""
        words = "".join(
            character if alphanumeric(character) else " " for character in text.lower()
        ).split()
        named = ""
        for word in words:
            longer = f"{named}{joiner}{word}" if named else word
            if len(longer) > row["limit"]:
                break
            named = longer
        # A first word longer than the limit is cut: the limit is the
        # runtime's contract, and a name it rejects is no name at all.
        return named or next((word[: row["limit"]] for word in words), "")

    stripped = given.strip()
    if spelled(stripped):
        return stripped
    return normalized(given) or normalized(description)


def passing(field: str, row: SpawnNameRow) -> str:
    """How a caller passes a name, opening with the key it goes under.

    ``field`` is the key the runtime reads the name from, passed by the host
    half that read it: one declaration serves every runtime and each spells
    the key its own way, and the schema a runtime shows the model may not
    list it at all — Claude Code 2.1.280 and 2.1.285 show an `Agent` tool
    with no `name` and take one regardless — so a sentence that only says
    "pass a name" leaves the caller to guess which argument, and the guess it
    makes is the description.
    """
    return (
        f"pass the name as `{field}` in the same call, beside the agent type"
        " — the runtime takes that key whether or not the tool schema it showed"
        f" lists it: {row['recovery']}"
    )


def spawn_notice(
    named: str, description: str, row: SpawnNameRow | None, field: str
) -> str:
    """What a spawn's caller is told once it has gone out, or ``""`` for nothing.

    Said where the spawn went out under the name its description reads into,
    which is the name a spawn given none takes. Whether one was given is no
    longer on the call by then: the runtime hands the hook the call as it
    ran, rewrite included — measured on Claude Code 2.1.285, a spawn the
    model sent with no name reached `PostToolUse` carrying the one its
    `PreToolUse` rewrite gave it. A caller who passed the description's own
    words chose nothing the reading would not have, so the same sentence
    serves them.

    A runtime whose spawn carries no description reads no name out of one,
    so nothing a caller sends there is told anything; ``None`` for the row,
    or a blank notice, is a project that says nothing.
    """
    if row is None or not row["notice"] or not named:
        return ""
    if named != spawn_name("", description, row):
        return ""
    return (
        f"{row['notice']}: this one went out as `{named}`, read from its"
        f" description. Next time, {passing(field, row)}."
    )


def decide_spawn(
    given: str,
    description: str,
    values: list[str],
    row: SpawnNameRow | None,
    field: str,
    *,
    tool: str = "",
    refused: Sequence[RefusedToolRow] = (),
) -> KernelDecision:
    """The verdict on one spawn: deferred under a name, refused where none can be read.

    The refusal table is asked first, under the runtime's name for the spawn
    (``tool``): a spawn's own judgement is only of its name, so a project
    that decided against spawning states it there, and a row naming some
    other subject of the tool leaves the name to be judged.

    ``None`` for the row is a project that requires no name, which leaves the
    call to the runtime. An escalation marker among the call's inputs turns
    the refusal into the approval question the caller asked for, the way a
    refused tool's does. The recovery opens with ``field``, the key the
    runtime reads the name from (:func:`passing`).

    An empty ``field`` is a spawn that takes no name. Codex 0.159.2's
    `multi_agent_v1` `spawn_agent` lists none, accepts a `task_name` passed
    anyway and ignores it, and answers under a nickname it generates, so
    asking for a name would only send the caller after an argument that
    changes nothing.
    """
    declined = decide_tool(tool, values, list(refused))
    if declined is not None and declined.effect != "defer":
        return declined
    if row is None:
        return KernelDecision("defer", "no spawn name is required here")
    if not field:
        return KernelDecision(
            "defer",
            "this spawn takes no name, so it goes out under the nickname the"
            " runtime gives it",
        )
    named = spawn_name(given, description, row)
    if named:
        return KernelDecision("defer", f"the spawn goes out named {named!r}")
    recovery = passing(field, row)
    why = escalated_reason(values)
    if why:
        return KernelDecision(
            "ask", f"escalated ({why}): {row['reason']}", recovery=recovery
        )
    return KernelDecision(
        "deny", row["reason"], recovery=f"{recovery} {TOOL_ESCALATE_HINT}"
    )
