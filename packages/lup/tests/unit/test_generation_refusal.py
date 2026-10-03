"""What a declaration that will not compile says to whoever ran generation.

Generation is reached from two places whose entire user-facing output is what
the command printed: the commit guard, and the regeneration `dev update` ends
in. A declaration refused there must not arrive as whatever its reader happens
to raise — a missing passage file as a `FileNotFoundError` traceback, an
invocation naming no skill as a hundred-line pydantic repr — because both are
declaration errors with a fix in the declaration. What is under test is that
each comes back as the two facts that fix takes.
"""

from pathlib import Path

import pytest
import sh
import typer
from pydantic import BaseModel, ValidationError, model_validator

import lup.devtools.dev.update as update
from lup.formats.banner import REGENERATE_COMMAND
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import generate_targets
from lup.harness.generate import NativeHarnessComposition, obstruction_at
from lup.harness.codescan.common import RuleSelection

MISSING_PASSAGE = "src/demo/harness/content/skills/update.passage.md"
"""A path a declaration names and nothing wrote, as the reader spells it."""


class Declared(BaseModel):
    """A declaration refusing one value, the way the harness refuses one.

    Stands in for `Harness`, whose own validator raises the case this is
    about: the shape that matters is a model validator raising, which
    pydantic delivers as one error with an empty location.
    """

    skill: str

    @model_validator(mode="after")
    def known(self) -> "Declared":
        if self.skill == "upstream":
            raise ValueError(
                "skill invocation refers to an unknown declaration: lup:upstream"
            )
        return self


def absent(root: Path, rules: RuleSelection | None = None) -> NativeHarnessComposition:
    """A target whose declaration names a passage file nothing wrote."""
    raise FileNotFoundError(2, "No such file or directory", MISSING_PASSAGE)


def test_a_refused_declaration_names_the_obstruction_and_the_model() -> None:
    """The pydantic reading: one line per refusal, and which model refused."""
    with pytest.raises(ValidationError) as refused:
        Declared(skill="upstream")

    obstruction = obstruction_at("claude", refused.value)

    assert obstruction.described() == [
        "claude: nothing generated, the declaration was refused",
        "  Value error, skill invocation refers to an unknown declaration: "
        "lup:upstream",
        "  declared in Declared",
    ]


def test_a_refused_field_carries_where_in_the_declaration_it_sits() -> None:
    """A field refusal is useless without its location, which pydantic has."""
    with pytest.raises(ValidationError) as refused:
        Declared.model_validate({"skill": 3})

    assert obstruction_at("codex", refused.value).obstruction == [
        "skill: Input should be a valid string"
    ]


def test_a_passage_nothing_wrote_names_the_file_rather_than_the_reader() -> None:
    """An errno and a path are what a missing file knows; the frames are noise."""
    obstruction = obstruction_at(
        "claude", FileNotFoundError(2, "No such file or directory", MISSING_PASSAGE)
    )

    assert obstruction.obstruction == ["No such file or directory"]
    assert obstruction.declaration == MISSING_PASSAGE


def test_a_refusal_naming_no_declaration_says_so_by_naming_none() -> None:
    """Its own words are then everything there is, and its class is not a where."""
    obstruction = obstruction_at("claude", ValueError("holds `{%`: a passage names"))

    assert obstruction.described() == [
        "claude: nothing generated, the declaration was refused",
        "  holds `{%`: a passage names",
    ]


def test_resolving_a_target_that_will_not_compile_refuses_with_what_stopped_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The boundary every command reaches a declaration through, refusing once.

    Nonzero, because a command that could not compile has not done what was
    asked of it — and with nothing on the way out but the obstruction and the
    one command that settles it, since that is the whole of what the commit
    guard's reader is handed.
    """
    targets = NativeTargets(builders={"claude": absent})

    with pytest.raises(typer.Exit) as stopped:
        targets.resolve("all", Path("/nowhere"))

    assert stopped.value.exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        "claude: nothing generated, the declaration was refused",
        "  No such file or directory",
        f"  declared in {MISSING_PASSAGE}",
        "error: the declaration above does not compile",
        f"→ fix it, then regenerate: `{REGENERATE_COMMAND}`",
    ]


def test_a_repository_writer_that_will_not_compile_refuses_the_same_way(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The generated half outside every native tree answers the same question.

    A writer is a callable the project declared and carries no label of its
    own, so the refusal names the half and leaves the declaration to the
    obstruction — which is the file, and the only part anybody acts on.
    """

    def refusing(root: Path | None = None, *, check: bool = False) -> Path:
        raise FileNotFoundError(2, "No such file or directory", MISSING_PASSAGE)

    with pytest.raises(typer.Exit) as stopped:
        generate_targets([], [refusing])

    assert stopped.value.exit_code == 1
    assert capsys.readouterr().err.splitlines()[:2] == [
        "repository artifacts: nothing generated, the declaration was refused",
        "  No such file or directory",
    ]


def test_an_update_relays_what_its_regeneration_refused_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path that reads this refusal through a subprocess, not a frame.

    `dev update` regenerates under the library it has just installed, which
    has to be a subprocess: the library imported in that process is the
    version the update exists to replace. So the refusal arrives as an exit
    status with the words on a captured stream, and those words are what the
    reader acts on — every other carrier has moved by then, and which
    declaration is the one remaining fact.
    """
    refused = (
        "claude: nothing generated, the declaration was refused\n"
        "  Value error, skill invocation refers to an unknown declaration: "
        "lup:upstream\n"
        "  declared in Harness\n"
    )

    def refusing(*words: str, **named: str) -> None:
        raise sh.ErrorReturnCode_1(REGENERATE_COMMAND, b"", refused.encode())

    monkeypatch.setattr(update, "uv", refusing)
    said: list[str] = []

    with pytest.raises(typer.Exit) as stopped:
        update.regenerated(tmp_path, said.append)

    assert stopped.value.exit_code == 1
    assert said[1:] == [
        "The native trees were not regenerated. Generation refused:",
        "  claude: nothing generated, the declaration was refused",
        "    Value error, skill invocation refers to an unknown declaration: "
        "lup:upstream",
        "    declared in Harness",
        "Every other carrier has moved and the merge has landed, so what is "
        f"left is the declaration named above. Fix it, then run `{REGENERATE_COMMAND}`.",
    ]
