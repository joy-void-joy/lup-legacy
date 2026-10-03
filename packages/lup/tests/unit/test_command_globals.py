"""A global in front of a subcommand is consumed before anything reads the command.

A reader matching a subcommand where it is written — `git`, then `restore` —
answers `None` about a command it does model once a global stands in front
of it. The row matches anyway, because the matcher steps over these globals
to find the subcommand, so every reader its verdict is relaxed or tightened
by has to step over them too: otherwise `git -C . restore <human-authored
file>` is allowed where the same restore without the flag asks. `-C .`
changes nothing about what the command does, which is what makes that a
bypass rather than a mistake.

The same flag is also the one that moves where an operand resolves, so both
halves are read here: what the command runs as, and where it runs it.
"""

from pathlib import Path

from lup.policy.kernel.lex import (
    command_directory,
    command_words_read,
    shell_flag_write_targets,
    shell_path_verb_targets,
    shell_write_targets,
)
from lup.policy.models import ShellCommand
from lup.policy.rules import ShellPolicy
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

ROWS = erase_shell_rules(default_vocabulary())


def test_a_global_no_longer_hides_the_subcommand_from_its_readers() -> None:
    """The reader that names what a restore writes sees the same command either way."""
    assert shell_path_verb_targets("git restore notes.md", ROWS) == ["notes.md"]
    assert shell_path_verb_targets("git -C . restore notes.md", ROWS) == ["notes.md"]
    assert shell_path_verb_targets("git --git-dir=.git restore notes.md", ROWS) == [
        "notes.md"
    ]


def test_the_flag_that_names_a_directory_moves_where_an_operand_resolves() -> None:
    """`-C` runs git as though it had started there, so its operands do too."""
    assert shell_path_verb_targets("git -C ../other restore src/mod.py", ROWS) == [
        "../other/src/mod.py"
    ]
    assert command_directory(["git", "-C", "../other", "restore", "x"], ROWS) == (
        "../other"
    )


def test_a_global_that_consumes_a_word_moves_no_operand() -> None:
    """`--git-dir` names a repository and `--work-tree` a tree; neither is a cwd."""
    assert command_directory(["git", "--git-dir=.git", "restore", "x"], ROWS) == ""
    assert shell_path_verb_targets("git --work-tree . restore notes.md", ROWS) == [
        "notes.md"
    ]


def test_the_same_spelling_after_a_subcommand_is_left_alone() -> None:
    """`git commit -C` reuses a message, which is why the boundary is read."""
    words = ["git", "commit", "-C", "HEAD"]
    assert command_words_read(words, ROWS) == words
    assert command_directory(words, ROWS) == ""


def test_a_global_the_row_asks_about_stays_where_it_is() -> None:
    """The question `-c` raises is the row's to raise, so it is not consumed."""
    words = ["git", "-c", "core.pager=less", "restore", "notes.md"]
    assert command_words_read(words, ROWS) == words


def test_a_directory_nothing_can_read_leaves_a_named_path_unjudged() -> None:
    """A guess would put the operand in a directory the command never entered."""
    assert command_directory(["git", "-C", "$TARGET", "restore", "x"], ROWS) is None
    assert shell_path_verb_targets("git -C $TARGET restore notes.md", ROWS) == []


def test_a_command_naming_no_path_is_not_made_unjudgeable_by_one() -> None:
    """The verb behind the flag answers as it always did.

    Both halves of the refusal are needed, and only the unreadable half is
    about the directory: `git -C "$W" add -A` stages whatever is in a checkout
    nobody here can name, and staging is reversible wherever it happens. A
    directory nothing can read makes a *path* unresolvable, not a command.
    """
    policy = ShellPolicy(default_vocabulary())
    for command in ('git -C "$W" add -A', "git -C $W commit -m x"):
        assert policy.decide(ShellCommand(command=command)).effect == "allow", command


def test_a_redirection_is_opened_where_the_shell_stands(tmp_path: Path) -> None:
    """The shell opens it before the command runs, so `-C` has not happened yet."""
    assert shell_write_targets("git -C other log > out.txt") == ["out.txt"]
    assert shell_write_targets("cd tmp && git -C other log > out.txt") == [
        "tmp/out.txt"
    ]
    assert tmp_path.exists()


def test_both_spellings_of_one_restore_reach_one_verdict(tmp_path: Path) -> None:
    """Which is the whole of it: inserting `-C .` must decide nothing."""
    (tmp_path / "notes.md").write_text("body\n", encoding="utf-8")
    policy = ShellPolicy(default_vocabulary())

    def decided(command: str) -> tuple[str, str]:
        verdict = policy.decide(ShellCommand(command=command, cwd=tmp_path))
        return verdict.effect, verdict.reason

    assert decided("git restore notes.md") == decided("git -C . restore notes.md")
    assert decided("git restore notes.md") == decided(
        "git --git-dir=.git restore notes.md"
    )


def test_a_write_flag_resolves_from_the_directory_the_command_names() -> None:
    """The flag reader reads what every other path reader reads.

    Taking the placement directly rather than the segment leaves this one
    reader seeing the subcommand a word further along than the classifier
    does, and resolving its operand from a `cd` alone — so a `-C` that moves
    where the write lands would be read by the verb reader beside it and not
    by this one, and the two would name different files for one command.
    """
    assert shell_flag_write_targets("sort -o out.txt f", ROWS) == ["out.txt"]
    assert shell_flag_write_targets("cd tmp && sort -o out.txt f", ROWS) == [
        "tmp/out.txt"
    ]
