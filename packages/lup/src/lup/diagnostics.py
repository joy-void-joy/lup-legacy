"""How a command ends on an error, or says a warning: one diagnostic on stderr.

Every command line the library serves — the devtools CLI, a provider's
installer, a tool server's entry point — stops the same way, in the shape
:mod:`lup.policy.kernel.diagnostic` declares for every message read at an
event, so an error reads the same whether a hook or a command said it.

Here rather than beside the devtools helpers, because a provider and a tool
server end commands too and import nothing of the devtools CLI; and outside
the kernel, which is standard library only, because this prints through
Typer. It imports the kernel's model and Typer, and nothing else of lup.
"""

from collections.abc import Sequence
from typing import NoReturn

import typer

from lup.policy.kernel.diagnostic import Diagnostic, Step, diagnostic, rendered


class Refusal(typer.Exit):
    """A command's error exit, carrying the diagnostic it printed.

    An exit rather than an error the CLI renders itself, because Typer draws
    its own errors in a box, where a command meant to be copied picks up the
    border. Raised by :func:`refuse`, which prints first, so whoever catches
    one reads what was said as data in ``said``; one nobody catches, outside
    a Typer app, reads as the diagnostic it carries.
    """

    said: Diagnostic

    def __init__(self, said: Diagnostic, code: int = 1) -> None:
        super().__init__(code)
        self.said = said

    def __str__(self) -> str:
        return rendered(self.said)


def refuse(
    why: str,
    what: str = "",
    steps: Sequence[Step] = (),
    see: str = "",
    code: int = 1,
) -> NoReturn:
    """End the command: print what was caught, why and the ways through, then exit.

    The text is :func:`~lup.policy.kernel.diagnostic.rendered`, which a hook's
    refusal goes through too. ``what`` is the words that decided it, each step
    holds the command it names as the words that run it, and ``code`` is the
    exit status.
    """
    said = diagnostic("error", why, what=what, steps=steps, see=see)
    typer.echo(rendered(said), err=True)
    raise Refusal(said, code)


def warn(why: str, what: str = "", steps: Sequence[Step] = (), see: str = "") -> None:
    """Say something the reader should know on stderr, in the same shape, and carry on."""
    typer.echo(
        rendered(diagnostic("warning", why, what=what, steps=steps, see=see)),
        err=True,
    )
