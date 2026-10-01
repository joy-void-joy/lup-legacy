"""Every message read at an event takes the one diagnostic shape.

A command ending on an error goes through `refuse`, which prints what was
caught, why and the ways through, so no error exit is a bare string. A way
through holds the command it names as the words that run it, so the command is
spelled one way everywhere and the CLI that serves it can be asked whether it
exists; prose naming one is the shape this holds the tree away from.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.dev.commands import CommandSurface
from lup.devtools.dev.documented import WrittenCommand, written_commands
from lup_template.devtools.main import cli

ROOT = Path(__file__).resolve().parents[2]

SOURCES = (ROOT / "packages/lup/src/lup", ROOT / "src/lup_template")
"""Every tree whose commands a person or an agent runs."""

# lup: ignore[library-default] — the exception types a CLI renders itself,
# each the shape a bare-string refusal takes
RENDERED_ERRORS = ("BadParameter", "UsageError", "ClickException")


def modules() -> Iterator[tuple[Path, ast.Module]]:
    """Every module of the trees above, parsed."""
    for source in SOURCES:
        for path in sorted(source.rglob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def called(node: ast.expr) -> str:
    """The last name a call is spelled with: `typer.echo` is `echo`."""
    match node:
        case ast.Call(func=ast.Name(id=name)) | ast.Call(func=ast.Attribute(attr=name)):
            return name
    return ""


def failing_exit(node: ast.stmt) -> bool:
    """Whether a statement raises an exit with a failing status."""
    match node:
        case ast.Raise(
            exc=ast.Call(args=[ast.Constant(value=int(code))]) as raised
        ) if code:
            return called(raised) in ("Exit", "SystemExit")
        case ast.Raise(exc=ast.Call(keywords=keywords) as raised):
            return called(raised) == "Exit" and any(
                keyword.arg == "code"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value != 0
                for keyword in keywords
            )
    return False


def printed_error(node: ast.stmt) -> bool:
    """Whether a statement prints to stderr through Typer."""
    match node:
        case ast.Expr(value=ast.Call(keywords=keywords) as call):
            return called(call) in ("echo", "secho") and any(
                keyword.arg == "err"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in keywords
            )
    return False


def bare_refusals(tree: ast.Module) -> Iterator[int]:
    """Lines where an error exit is built from a bare string.

    A raised error the CLI renders itself, a `SystemExit` handed a message, and
    a print to stderr followed straight away by a failing exit -- the three
    spellings `refuse` replaces.
    """
    for node in ast.walk(tree):
        match node:
            case ast.Raise(exc=ast.Call() as raised) if (
                called(raised) in RENDERED_ERRORS
            ):
                yield node.lineno
            case ast.Raise(
                exc=ast.Call(
                    func=ast.Name(id="SystemExit"),
                    args=[ast.Constant(value=str()) | ast.JoinedStr()],
                )
            ):
                yield node.lineno
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and all(
                isinstance(statement, ast.stmt) for statement in value
            ):
                yield from (
                    later.lineno
                    for earlier, later in zip(value, value[1:])
                    if printed_error(earlier) and failing_exit(later)
                )


def test_no_error_exit_is_a_bare_string() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}"
        for path, tree in modules()
        for line in bare_refusals(tree)
    ]
    assert not found, (
        "end the command with lup.devtools.utils.refuse(why, what=..., steps=[...]),"
        " which prints the one diagnostic shape:\n  " + "\n  ".join(found)
    )


@pytest.fixture(scope="module")
def surface() -> CommandSurface:
    """Every command the composed CLI serves, read off the app."""
    return CommandSurface.of(cli())


def prose_commands(text: str, surface: CommandSurface) -> list[str]:
    """Backticked spans in prose that spell a command of this CLI.

    The executable named outright, or two words or more the CLI answers to:
    `dev comments` is one, and `git worktree add`, git's own, is not.
    """
    return [
        span
        for span in text.split("`")[1::2]
        if "lup-devtools" in span.split()
        or (
            len(words := WrittenCommand(file="", line=0, spelled=span).command_words())
            > 1
            and surface.admits(words)
        )
    ]


def prose_of(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Each literal a way through says, and each reason a verdict gives."""
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Name(id="step"), args=[ast.Constant(value=str(says)), *_]
            ):
                yield node.lineno, says
            case ast.Call(
                func=ast.Name(id="KernelDecision"),
                args=[_, ast.Constant(value=str(reason)), *_],
            ):
                yield node.lineno, reason


def test_no_way_through_names_a_command_in_prose(surface: CommandSurface) -> None:
    named = [
        f"{path.relative_to(ROOT)}:{line}: {span}"
        for path, tree in modules()
        for line, text in prose_of(tree)
        for span in prose_commands(text, surface)
    ]
    assert not named, (
        "hold the command in the step's run, as devtools(...) words, so it is"
        " spelled one way and checked against the CLI:\n  " + "\n  ".join(named)
    )


def test_a_command_a_diagnostic_names_is_read_off_its_words() -> None:
    structured = [
        mention
        for mention in written_commands()
        if mention.file.endswith("policy/kernel/roles.py")
    ]

    assert (
        WrittenCommand(
            file="packages/lup/src/lup/policy/kernel/roles.py",
            line=structured[0].line,
            spelled="harness generate all",
        )
        in structured
    )


def test_a_refusal_prints_its_shape_and_exits_failing(tmp_path: Path) -> None:
    """`run monitor` on a path holding no run, which read as 0/0 landed and waited."""
    missing = tmp_path / "nowhere"

    result = CliRunner().invoke(cli(), ["run", "monitor", str(missing), "--once"])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        f"error: `{missing}` — is not a directory, so it holds no run",
        "→ pass the run directory the launch printed",
    ]
