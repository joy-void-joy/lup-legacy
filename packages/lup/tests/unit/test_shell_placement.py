"""Every reader of a command resolves its paths from where that command runs.

A path word was joined to the directory the session launched in, whatever the
command had done to the shell before reaching it. So `cd elsewhere && sed -i …
rel/path` was asked about because the host looked for a file that is not
there, while `cd packages && rm -rf lup` was answered as a deletion of `lup`
at the repository top -- one reading too conservative and one too permissive,
both from the same missing fact.
"""

from lup.policy.kernel.lex import (
    authored_writes,
    shell_patch_operands,
    shell_path_verb_targets,
    shell_sed_rewrites,
    shell_write_targets,
)
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


VOCABULARY = erase_shell_rules(default_vocabulary())


def rewritten(command: str) -> list[list[str]]:
    return [row["targets"] for row in shell_sed_rewrites(command, VOCABULARY)]


def test_a_rewrite_after_a_cd_names_the_file_the_command_would_reach() -> None:
    """The one reader the classifier judges a rewrite with follows the shell."""
    command = "cd src && sed -i 's/a/b/' mod.py"
    assert rewritten(command) == [["src/mod.py"]]
    assert shell_path_verb_targets(command, VOCABULARY) == ["src/mod.py"]


def test_a_deletion_after_a_cd_names_the_directory_it_stands_in() -> None:
    """The permissive half: `lup` at the top is not `packages/lup`."""
    assert shell_path_verb_targets("cd packages && rm -rf lup", VOCABULARY) == [
        "packages/lup"
    ]


def test_a_redirection_after_a_cd_lands_under_it() -> None:
    """A write's target is spelled from wherever the command carrying it runs."""
    assert shell_write_targets("cd tmp && printf 'x' > t.py") == ["tmp/t.py"]
    assert [write["path"] for write in authored_writes("cd tmp && echo x > t.py")] == [
        "tmp/t.py"
    ]


def test_a_patch_and_a_write_flag_follow_the_shell_too() -> None:
    """Every route a word reaches a file by is qualified, not just an operand."""
    assert shell_patch_operands("cd tmp && git apply fix.patch", VOCABULARY) == [
        "tmp/fix.patch"
    ]
    assert shell_write_targets("cd tmp && cat x > y") == ["tmp/y"]


def test_an_absolute_operand_after_a_cd_is_left_alone() -> None:
    """Where the shell stands does not bear on a word that names its own file."""
    assert rewritten("cd src && sed -i 's/a/b/' /etc/hosts") == [["/etc/hosts"]]


def test_a_command_standing_where_it_was_launched_keeps_its_spellings() -> None:
    """Nothing is normalized without a `cd`, so a refusal names what was typed."""
    assert rewritten("sed -i 's/a/b/' ./src/mod.py") == [["./src/mod.py"]]
    assert shell_write_targets("printf 'x' > ./tmp/t.py") == ["./tmp/t.py"]


def test_a_cd_climbs_as_the_shell_would() -> None:
    """`..` resolves against the directory, not against the launch root."""
    assert rewritten("cd src/inner && cd .. && sed -i 's/a/b/' mod.py") == [
        ["src/mod.py"]
    ]


def test_a_cd_nothing_here_can_read_leaves_the_words_after_it_unjudged() -> None:
    """A guess would put every later path in a directory nothing entered."""
    for move in ("cd $TARGET", "cd ~/work", "cd -", "cd", "pushd src", "popd"):
        assert shell_path_verb_targets(f"{move} && rm -rf tmp/x", VOCABULARY) == [], (
            move
        )
    # `pushd` and `popd` are themselves unclassified, so the command already
    # asks on its own terms; the `cd` spellings are the ones whose question has
    # to come from the directory they left behind: the path is one only the
    # run can name, and a deletion of it asks wherever the session runs.
    for move in ("cd $TARGET", "cd ~/work", "cd -", "cd"):
        for sandboxed in (False, True):
            verdict = decide_shell(
                f"{move} && rm -rf tmp/x",
                erase_shell_rules(default_vocabulary()),
                path_roles=SCRATCH,
                sandboxed=sandboxed,
            )
            assert verdict.effect == "ask", (move, sandboxed)
            assert (
                verdict.headline() == "asks: `rm` — deleting files requires approval"
            ), move


def test_a_subshell_move_reaches_nothing_after_it() -> None:
    """`(cd a && …)` stands for the rest of the subshell and no further."""
    assert rewritten("(cd src && sed -i 's/a/b/' mod.py)") == [["src/mod.py"]]
    assert shell_write_targets("(cd tmp; printf 'x' > a) ; printf 'y' > b") == [
        "tmp/a",
        "b",
    ]


def test_a_move_that_may_not_have_run_leaves_the_directory_unknown() -> None:
    """A loop body's `cd` decides nothing about the words after the loop."""
    verdict = decide_shell(
        "for n in 1; do cd src; done; rm -rf tmp/x",
        erase_shell_rules(default_vocabulary()),
        path_roles=SCRATCH,
        sandboxed=True,
    )
    assert verdict.effect == "ask"
    assert verdict.headline() == "asks: `rm` — deleting files requires approval"


def test_a_cd_that_failed_left_the_shell_where_it_stood() -> None:
    """What runs where a `cd` failed runs where the shell already was.

    `||` runs what follows where the command before it failed, `!` swaps the
    two, and an `if` runs its `else` where its condition failed: a failed
    `cd` moved nothing, so `cd a || rm x` removes the `x` beside it, which
    was read as `a/x` -- a scratch file standing in for a protected one.
    """
    assert shell_path_verb_targets("cd src || rm a", VOCABULARY) == ["a"]
    assert shell_path_verb_targets("! cd src && rm a", VOCABULARY) == ["a"]
    assert shell_path_verb_targets("true && cd src || rm a", VOCABULARY) == ["a"]
    assert shell_path_verb_targets(
        "if cd src; then rm a; else rm b; fi", VOCABULARY
    ) == ["src/a", "b"]
    assert shell_path_verb_targets("if ! cd src; then rm a; fi", VOCABULARY) == ["a"]


def test_a_cd_the_line_passes_unconditionally_is_taken_to_have_succeeded() -> None:
    """Past a `;`, a `cd` naming its directory is where the shell stands.

    Its failure is followed only where the line routes on it; the
    `# lup: defer:` note in `placed_andor` says what that leaves open.
    """
    assert shell_path_verb_targets("cd src; rm a", VOCABULARY) == ["src/a"]
    assert shell_path_verb_targets("cd src && true; rm a", VOCABULARY) == ["src/a"]
    assert shell_path_verb_targets("true && cd src; rm a", VOCABULARY) == []


def test_a_move_that_may_or_may_not_have_happened_leaves_the_directory_unknown() -> (
    None
):
    """Where the line itself may have skipped or undone a move, no directory is named.

    A chain whose later command failed after its `cd` ran routes `||` to
    either place, `time` may run the `cd` in a child, and a loop's second
    pass starts where its first left the shell. Each path after one is a
    path only the run can name, and a write to it asks.
    """
    for command in (
        "cd src && true || rm a",
        "time cd src && rm a",
        "while true; do cd src; rm a; done",
    ):
        assert shell_path_verb_targets(command, VOCABULARY) == [], command
        verdict = decide_shell(command, VOCABULARY, path_roles=SCRATCH, sandboxed=True)
        assert verdict.effect == "ask", command


def test_a_cd_in_a_process_of_its_own_moves_nothing() -> None:
    """A pipeline's commands and a backgrounded list each run in a child."""
    assert shell_path_verb_targets("cd src | rm a", VOCABULARY) == ["a"]
    assert shell_path_verb_targets("cd src & rm a", VOCABULARY) == ["a"]


def test_a_cd_run_through_command_or_builtin_moves_the_shell() -> None:
    """Both run the builtin in this same shell, so the move is the `cd`'s own."""
    assert shell_path_verb_targets("command cd src && rm a", VOCABULARY) == ["src/a"]
    assert shell_path_verb_targets("builtin cd src && rm a", VOCABULARY) == ["src/a"]


def test_a_move_inside_a_substitution_stays_inside_it() -> None:
    """The command in `$(…)` runs where the word does and leaves it there."""
    command = "cd src && echo $(cd inner && sed -i 's/a/b/' mod.py)"
    assert rewritten(command) == [["src/inner/mod.py"]]
    assert rewritten("echo $(cd src && true); sed -i 's/a/b/' mod.py") == [["mod.py"]]
