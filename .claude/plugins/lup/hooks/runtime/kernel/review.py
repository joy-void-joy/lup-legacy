"""Literal shell inputs that a review can bind without executing the shell."""

from .decision import KernelDecision
from .lex import parse_shell
from .syntax import Command, verbatim, word_text
from typing import TypedDict


class Reviewed(TypedDict):
    """One ask put to the review queue: its verdict, and what the operator is told."""

    decision: KernelDecision
    notice: str
    """The line a runtime shows the person beside the refusal, empty on an allow."""


def single_command(command: str) -> Command | None:
    """A single foreground command, preserving its words and redirects."""
    tree = parse_shell(command)
    if isinstance(tree, KernelDecision):
        return None
    items = tree["items"]
    if len(items) != 1 or items[0]["terminator"] == "&":
        return None
    pipelines = items[0]["andor"]["pipelines"]
    if len(pipelines) != 1 or pipelines[0]["negated"]:
        return None
    commands = pipelines[0]["commands"]
    if len(commands) != 1 or commands[0]["kind"] != "simple":
        return None
    return commands[0]


def literal_input(command: str, executable: str) -> str | None:
    """One literal argument or quoted heredoc, with no adjacent shell effects."""
    invocation = single_command(command)
    if invocation is None:
        return None
    words = invocation["words"]
    if not words or word_text(words[0]) != executable:
        return None
    redirects = invocation["redirects"]
    if len(words) == 2 and not redirects and verbatim(words[1]):
        return word_text(words[1])
    if len(words) == 1 and len(redirects) == 1:
        redirect = redirects[0]
        if redirect["operator"] == "<<" and len(redirect["heredoc"]) == 1:
            heredoc = redirect["heredoc"][0]
            if heredoc["quoted"]:
                return heredoc["body"]
    raise ValueError(
        f"use one {executable} with a single-quoted argument or quoted heredoc"
    )
