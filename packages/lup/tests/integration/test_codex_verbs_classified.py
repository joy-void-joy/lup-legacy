"""Every verb the installed Codex CLI lists is one the shell vocabulary judges.

The ``codex`` rule reads an unrecognized word as the prompt of an interactive
session, so a verb a new release adds — ``queue``, ``archive`` and ``delete``
each arrived that way — is judged as opening an agent until somebody writes
its row, whatever it actually does. Which verbs exist is a fact about the
installed binary, answered afresh by each release, so it is asked of the
binary here rather than pinned in a fixture.
"""

import shutil
import subprocess

import pytest

from lup.policy.vocabulary import default_vocabulary

pytestmark = pytest.mark.integration


def listed_verbs(help_text: str) -> list[str]:
    """The verbs a clap ``Commands:`` section names, continuation lines aside.

    A verb's row opens two spaces in; a description that wraps continues
    further in, and a blank line ends the section.
    """
    lines = help_text.splitlines()
    start = lines.index("Commands:") + 1
    section = lines[start : lines.index("", start)]
    return [
        # lup: ignore[string-split] — clap's help column, which no parser owns
        line.strip().partition(" ")[0]
        for line in section
        if line.startswith("  ") and not line.startswith("   ")
    ]


def test_every_listed_codex_verb_has_a_row() -> None:
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("no codex CLI on PATH")
    shown = subprocess.run(
        [binary, "--help"], capture_output=True, text=True, check=True, timeout=60
    )
    rule = next(rule for rule in default_vocabulary() if rule.name == "codex")
    declared = [subcommand.name for subcommand in rule.subcommands]

    listed = listed_verbs(shown.stdout)
    unjudged = [verb for verb in listed if verb not in declared]

    assert "exec" in listed, f"read no verbs out of `codex --help`: {listed}"
    assert unjudged == [], (
        f"codex lists {unjudged}, which the vocabulary reads as a prompt: "
        "give each a row in the codex rule saying what it does"
    )
