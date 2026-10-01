"""One shape for every message a person or an agent reads at an event.

A hook refusing a call, a question put to the person approving one, a command
ending in an error, a gate's finding: each tells its reader what was caught,
why, and the way through. Said in one shape they read alike wherever they
show up, and the way through is data. A command a message names is held as
the words a shell runs, so it is spelled the same everywhere it is shown, and
a check can ask the CLI that serves it whether it exists.

As text, a diagnostic reads:

    refused: `pip install` — changes packages outside this project's lockfile
    → `uv add requests`
    → or resubmit it with a first line `# lup: escalate[decision]: <why>`
    see docs/permissions.md

The first line is the verdict, the words that decided it, and why, in one
clause. Each arrow line is one way through. The last line, when there is one,
names the page that explains the rest, so the message itself stays short.

Standard library only: the policy kernel renders through this inside a hook
that imports nothing else.
"""

from collections.abc import Iterable, Sequence
from typing import Literal, TypedDict

type Verdict = Literal[
    "refused", "asks", "queued", "allowed", "deferred", "error", "warning"
]
"""The word a diagnostic opens with.

``refused``, ``asks``, ``queued``, ``allowed`` and ``deferred`` are the
policy's answers: ``queued`` a call held for the operator's answer, and
``deferred`` one left to the runtime's own mode. ``error`` and ``warning``
are a command's. Always lower case and never prefixed, so every message
starts the same way.
"""


class Step(TypedDict):
    """One way through: what to do, and the command that does it.

    ``says`` is what to do in plain words, empty where the command says it
    all. ``run`` is the command as the words a shell runs, empty for a step
    that runs nothing, such as asking the user to restart the session. A step
    that names a command holds it in ``run``, never in ``says``.
    """

    says: str
    run: list[str]


class Diagnostic(TypedDict):
    """What was caught, why, and the way through, for one event.

    ``what`` is the words that decided it — a flag, a path, a package, a
    session's name — shown in backticks, or empty where no short span does.
    ``why`` is one clause. ``rule`` is the stable id of the rule that caught
    it, for an audit or a page to group by, or empty; the text leaves it out.
    ``see`` is the page that explains the rest, or empty.
    """

    verdict: Verdict
    what: str
    why: str
    steps: list[Step]
    rule: str
    see: str


def diagnostic(
    verdict: Verdict,
    why: str,
    what: str = "",
    steps: Sequence[Step] = (),
    rule: str = "",
    see: str = "",
) -> Diagnostic:
    """One diagnostic, with every field it leaves out empty."""
    return Diagnostic(
        verdict=verdict, what=what, why=why, steps=list(steps), rule=rule, see=see
    )


def step(says: str = "", run: Sequence[str] = ()) -> Step:
    """One way through, with what it leaves out empty."""
    return Step(says=says, run=list(run))


def devtools(
    *words: str, program: Sequence[str] = ("uv", "run", "lup-devtools")
) -> list[str]:
    """A command of the development CLI, as the words that run it.

    Spelled through ``uv run``, because that is the spelling the policy lets
    run: uv checks the project's environment before it starts the command.
    """
    return [*program, *words]


def spelled(run: Sequence[str]) -> str:
    """A command as a reader types it, each word quoted only where a shell needs it.

    A word of letters, digits and ``@%+=:,./-_`` is written as it is. So is a
    placeholder in angle brackets, ``<name>``, which the reader replaces, and
    a ``$NAME`` or leading ``~`` the shell is meant to expand. Any other word
    goes in single quotes.
    """

    def plain(word: str) -> bool:
        """Whether a shell reads this word as written."""
        return word != "" and all(
            character.isalnum() or character in "@%+=:,./-_<>$~" for character in word
        )

    def quoted(word: str) -> str:
        """The word in single quotes, with each quote it holds closed and reopened."""
        inside = "".join(
            "'\\''" if character == "'" else character for character in word
        )
        return f"'{inside}'"

    return " ".join(word if plain(word) else quoted(word) for word in run)


def distinct(steps: Iterable[Step]) -> tuple[Step, ...]:
    """The ways through in order, each said once however often it was given."""
    seen = dict.fromkeys((through["says"], tuple(through["run"])) for through in steps)
    return tuple(step(says, run) for says, run in seen)


def way(through: Step) -> str:
    """One way through as its arrow line."""
    command = f"`{spelled(through['run'])}`" if through["run"] else ""
    said = ": ".join(part for part in (through["says"], command) if part)
    return f"→ {said}"


def stated(what: str, why: str) -> str:
    """The words that decided it, in backticks, and why: a first line after its verdict."""
    shown = f"`{what}`" if what else ""
    return " — ".join(part for part in (shown, why) if part)


def headline(said: Diagnostic) -> str:
    """The first line: the verdict, the words that decided it, and why."""
    return f"{said['verdict']}: {stated(said['what'], said['why'])}"


def rendered(said: Diagnostic) -> str:
    """The whole diagnostic as text: its first line, its ways through, its page."""
    lines = [
        headline(said),
        *(way(through) for through in said["steps"]),
        *([f"see {said['see']}"] if said["see"] else []),
    ]
    return "\n".join(lines)
