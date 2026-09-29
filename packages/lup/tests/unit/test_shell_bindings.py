"""Every reader of a command expands its variables from one binding pass.

Three readers resolved `$NAME` apart: the classifier's segment walk expanded a
reference anywhere in a word, the redirection rows only a word that was the
whole reference, and the host readers that stat targets and produce rewritten
documents not at all. Each disagreement was an approval question about a path
the classifier had already read, and one reading was unsound -- a name
rebound by a substitution or a `read` kept its earlier literal.
"""

from lup.policy.kernel.lex import (
    authored_writes,
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


def test_a_rewrite_through_a_variable_names_the_file_to_every_reader() -> None:
    """The host produces the document for the target the classifier judges."""
    command = "S=src.py; sed -i 's/a/b/' $S"
    assert shell_path_verb_targets(command, VOCABULARY) == ["src.py"]
    assert [row["targets"] for row in shell_sed_rewrites(command, VOCABULARY)] == [
        ["src.py"]
    ]


def test_a_reference_inside_a_word_expands_for_a_redirection() -> None:
    """`> $P/t.py` is a write under `$P`, not a word spelling no path."""
    assert shell_write_targets("P=tmp; printf 'x' > $P/t.py") == ["tmp/t.py"]
    assert shell_write_targets("P=tmp; printf 'x' > ${P}/t.py") == ["tmp/t.py"]
    verdict = decide_shell(
        "P=tmp; printf 'x' > $P/t.py",
        erase_shell_rules(default_vocabulary()),
        path_roles=SCRATCH,
    )
    assert verdict.effect == "allow"


def test_a_substitution_inherits_the_bindings_where_it_stands() -> None:
    """The command inside `$(…)` is expanded from the values around it."""
    command = "S=src.py; echo $(sed -i 's/a/b/' $S)"
    assert [row["targets"] for row in shell_sed_rewrites(command, VOCABULARY)] == [
        ["src.py"]
    ]


def test_a_rebinding_nothing_here_ran_leaves_the_reference_unexpanded() -> None:
    """The earlier literal is not what the name holds afterwards."""
    for rebinding in ("X=$(hostname)", "read X", "export X=elsewhere", "unset X"):
        command = f"X=tmp/a; {rebinding}; cat > $X"
        assert shell_write_targets(command) == ["$X"], rebinding


def test_an_assignment_that_may_not_stand_expands_nowhere() -> None:
    """A loop, a branch, a subshell, a pipe or an eval decides what ran."""
    for construct in (
        "for n in 1; do X=/etc/p; done",
        "if true; then X=/etc/p; fi",
        "case y in y) X=/etc/p;; esac",
        "( X=/etc/p )",
        "X=/etc/p | cat",
        "eval true",
        ": ${X:=/etc/p}",
    ):
        command = f"X=tmp/a; {construct}; cat > $X"
        assert shell_write_targets(command) == ["$X"], construct


def test_a_construct_that_assigns_nothing_leaves_the_binding_standing() -> None:
    """Case patterns and loops around unrelated names change nothing."""
    command = (
        "S=src.py; case y in y) true;; esac; for n in 1; do :; done; sed -i 's/a/b/' $S"
    )
    assert shell_path_verb_targets(command, VOCABULARY) == ["src.py"]


def test_a_command_prefix_assignment_still_binds_nothing() -> None:
    """`VAR=x cmd $VAR` expands from the value the shell already held."""
    assert shell_path_verb_targets("S=src.py sed -i 's/a/b/' $S", VOCABULARY) == ["$S"]
    assert shell_write_targets("B=tmp/x; B=tmp/y cat > $B") == ["tmp/x"]


def test_a_reference_the_shell_never_expands_is_never_expanded() -> None:
    """Single quotes and a quoted heredoc keep `$S` as the characters it is.

    Words reached the binding pass with their quoting already removed, so the
    `$S` inside a sed script was expanded into it and the script judged was
    one the command never runs.
    """
    command = "S=src.py; sed -i 's/$S/x/' $S"
    assert shell_sed_rewrites(command, VOCABULARY) == [
        {"scripts": ["s/$S/x/"], "options": [], "targets": ["src.py"]}
    ]
    written = authored_writes("P=tmp; cat > $P/f.txt <<'EOF'\n$P\nEOF")
    assert written == [{"path": "tmp/f.txt", "content": "$P\n", "append": False}]


def test_a_literal_binding_left_in_quotes_raises_no_opaque_gate() -> None:
    """A name bound to a literal is not opaque because a quoted `$` spells it."""
    verdict = decide_shell(
        "S=tmp/a.txt; printf '%s' '$S' > $S",
        erase_shell_rules(default_vocabulary()),
        path_roles=SCRATCH,
    )
    assert verdict.effect == "allow", verdict.reason


def test_a_value_that_would_split_or_may_be_skipped_binds_nothing() -> None:
    """Whitespace splits a reference into more words; `&&` may skip the assignment."""
    assert shell_write_targets('X="tmp/a b"; cat > $X') == ["$X"]
    assert shell_write_targets("X=tmp/a; true && X=/etc/p; cat > $X") == ["$X"]
