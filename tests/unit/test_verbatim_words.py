"""A `$` the shell's quoting holds is read as the character it is.

Every rule reads a command's words as strings, and reads a `$` standing in
one as an expansion nothing resolved. The grammar knows better than the
string: `'a$'`, `'$HOME'` and `\\$x` reach the program as those characters,
so read from the string alone, a pattern, a sed address, a git setting or a
path spelled with a dollar sign is a word that could become anything --
`rg '$x' src` refused as an argument that "could expand into a guarded flag".

So the fact rides in the string the rules read, without changing a character
of it, and every reader of an expansion asks it the same way. What the shell
does rewrite -- a parameter, `$'…'`, a brace expansion, a tilde -- keeps its
reading as an expansion.
"""

import pytest

from lup.harness.enforcement import semantic_policy_for
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.documents import file_steps
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.lex import command_segments, parse_shell
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.models import ShellCommand
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary
from lup_template.harness.catalog import declared_hook_set
from lup.policy.kernel.review import literal_input
from lup.policy.kernel.roles import spells_its_path
from lup.policy.kernel.syntax import VerbatimText, expands, verbatim_piece
from lup.policy.kernel.words import opaque_argument


def read(command: str) -> list[str]:
    """The first segment's words, as every rule reads them."""
    tree = parse_shell(command)
    assert not isinstance(tree, KernelDecision), command
    return command_segments(tree)[0]


@pytest.mark.parametrize(
    ("command", "position", "text"),
    [
        ("rg -e'foo$' src", 1, "-efoo$"),
        ("grep 'a$' f", 1, "a$"),
        ("sed -n '/x$/p' f", 2, "/x$/p"),
        ("echo '$HOME'", 1, "$HOME"),
        ("echo \\$HOME", 1, "$HOME"),
        ('echo "a$"', 1, "a$"),
        ("grep a$ f", 1, "a$"),
    ],
)
def test_a_dollar_the_quotes_hold_is_literal_text(
    command: str, position: int, text: str
) -> None:
    """The text is unchanged and the word says it expands into nothing."""
    word = read(command)[position]

    assert word == text
    assert isinstance(word, VerbatimText)
    assert not expands(word)


@pytest.mark.parametrize(
    ("command", "position", "text"),
    [
        ('rg -e"foo$X" src', 1, "-efoo$X"),
        ("rg -e'foo'$X src", 1, "-efoo$X"),
        ("rg $'\\x2d\\x2dpre=x' src", 1, "$\\x2d\\x2dpre=x"),
        ('echo $"x"', 1, "$x"),
        ("echo a{-rf,}", 1, "a{-rf,}"),
        ("echo x=~/a", 1, "x=~/a"),
        ("echo ~/a", 1, "~/a"),
        ("echo *.py", 1, "*.py"),
    ],
)
def test_what_the_shell_still_rewrites_keeps_its_reading(
    command: str, position: int, text: str
) -> None:
    """A parameter, `$'…'`, a brace, a tilde and a glob are not verbatim."""
    word = read(command)[position]

    assert word == text
    assert not isinstance(word, VerbatimText)


def test_every_reader_of_an_expansion_asks_the_same_question() -> None:
    """One spelling, two quotings, and the three readers agree about each."""
    quoted = read("echo '$x'")[1]
    expanded = read('echo "$x"')[1]

    assert quoted == expanded == "$x"
    assert not expands(quoted) and expands(expanded)
    assert not opaque_argument(quoted) and opaque_argument(expanded)
    assert spells_its_path(quoted) and not spells_its_path(expanded)


def test_a_piece_cut_from_a_word_is_verbatim_only_when_it_says_so() -> None:
    """A slice is a plain string, which is the reading every string had.

    Losing the fact costs a question that was not owed and never a grant, so
    a reader that keeps a piece verbatim does it on purpose.
    """
    word = read("sort --output='a$b' f")[1]

    assert not isinstance(word[len("--output=") :], VerbatimText)
    assert isinstance(verbatim_piece(word, word[len("--output=") :]), VerbatimText)
    assert not isinstance(verbatim_piece("$HOME", "HOME"), VerbatimText)


def test_the_review_readers_bind_only_what_reaches_the_program_as_written() -> None:
    """The reviewer's diff binds literal inputs through the same predicate.

    A brace expansion copies to two targets and `$'…'` rewrites its text, so
    neither is a literal to show a diff for.
    """
    rows = erase_shell_rules(default_vocabulary())
    assert [
        (step["action"], step["path"], step["source"])
        for step in file_steps("cp 'a$b' c", rows)
    ] == [("copy", "c", "a$b")]
    assert [step["action"] for step in file_steps("cp a{b,c} d", rows)] == ["run"]
    assert literal_input("apply_patch 'x'", "apply_patch") == "x"
    with pytest.raises(ValueError):
        literal_input("apply_patch $'x'", "apply_patch")


@pytest.mark.parametrize(
    "command",
    [
        "echo x > 'tmp/a$b'",
        "cat > 'tmp/a$b' <<'EOF'\nbody\nEOF",
        "printf 'x' | tee 'tmp/a$b'",
        "echo x > tmp/a\\$b",
    ],
)
def test_the_edit_gates_read_a_quoted_dollar_as_text_too(command: str) -> None:
    """A write carrying its content meets the edit gates, which agree with the shell.

    The shell kernel read `'tmp/a$b'` as the scratch file it names, and the
    edit gates, handed the same path, read the `$` as an expansion and judged
    a production file written whole -- so the command asked through them.
    """
    policy = semantic_policy_for(declared_hook_set())

    assert policy.decide(ShellCommand(command=command)).effect == "allow"


def test_an_expansion_the_shell_still_performs_keeps_its_question() -> None:
    """Unquoted, the `$` is a parameter, and the path is only known at run time."""
    policy = semantic_policy_for(declared_hook_set())

    assert policy.decide(ShellCommand(command="echo x > tmp/$B")).effect == "ask"


def test_an_edited_path_is_literal_wherever_the_gate_is_reached_from() -> None:
    """No shell expands a native edit's path, so its `$` never names another file."""
    decided = decide_edit(
        "tmp/a$b.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=[PathRoleRow(root="**/tmp", role="scratch")],
    )

    assert decided.effect == "allow"
