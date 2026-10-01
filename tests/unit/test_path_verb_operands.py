"""Which of a path verb's operands it writes, read by the verb's own grammar.

`-m` in `install -m 644 README.md tmp/x` takes a value, and to a reader that
does not know so, a flag it does not know makes every operand a target -- so
the file `install` only reads would be judged as one it writes, asking
"README.md is human-authored".
"""

import pytest

from lup.harness.enforcement import declared_role_rows
from lup.policy.kernel.shell import decide_shell
from lup.policy.kernel.words import path_verb_operands, written_targets
from lup.policy.rules import human_owned_path_rule, path_rule_row
from lup.policy.shell_rules import erase_shell_rules
from lup_template.harness.catalog import declared_hook_set

SHELL_ROWS = erase_shell_rules(declared_hook_set().resolved_shell_rules())
PATH_ROLES = declared_role_rows(list(declared_hook_set().path_roles))
README = [path_rule_row(human_owned_path_rule("README.md"))]
README = [path_rule_row(human_owned_path_rule("README.md"))]


@pytest.mark.parametrize(
    ("words", "written"),
    [
        (["install", "-m", "644", "README.md", "tmp/x"], ["tmp/x"]),
        (
            ["install", "--mode=644", "-o", "me", "-g", "us", "README.md", "tmp/x"],
            ["tmp/x"],
        ),
        (["install", "-m644", "-D", "README.md", "tmp/x"], ["tmp/x"]),
        (["install", "-t", "tmp", "README.md", "LICENSE"], ["tmp"]),
        (["install", "--target-directory=tmp", "README.md"], ["tmp"]),
        # Every operand a directory it makes, or a flag nothing models: the
        # positions stop meaning what they read, and every operand is named.
        (["install", "-d", "a", "b"], ["a", "b"]),
        (["install", "-s", "README.md", "tmp/x"], ["README.md", "tmp/x"]),
    ],
)
def test_install_is_read_by_its_own_grammar(
    words: list[str], written: list[str]
) -> None:
    assert written_targets(words) == written


def test_the_directory_install_names_is_placed_where_it_was_spelled() -> None:
    """`--target-directory=tmp` names the path after its `=`, in its own word."""
    (named,) = path_verb_operands(["install", "--target-directory=tmp", "a"])["named"][
        -1:
    ]

    assert (named["at"], named["prefix"], named["path"]) == (
        1,
        "--target-directory=",
        "tmp",
    )


@pytest.mark.parametrize(
    ("command", "effect"),
    [
        ("install -m 644 README.md tmp/x", "allow"),
        ("install -t tmp README.md", "allow"),
        ("install -m 644 tmp/x README.md", "ask"),
    ],
)
def test_installing_a_human_owned_file_out_reads_it(command: str, effect: str) -> None:
    decided = decide_shell(
        command,
        SHELL_ROWS,
        path_roles=PATH_ROLES,
        path_rules=README,
        existing_targets=["README.md"],
    )

    assert decided.effect == effect, decided.reason
