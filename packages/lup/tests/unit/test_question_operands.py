"""A question leads with the operands the decision turns on.

Whoever approves reads the question's first line and answers yes or no: the
words of the call that decided it, then why. What they weigh is the operand:
which packages a `--with` installs, which path a write lands on, which file a
rule protects. A question that named the category and left the operand out --
"external code", "a protected path" -- put the one word that decided it
somewhere the approver could not read it.

These pin the shape at the three seams that were measured saying the least,
so a rewrite that reaches for the category again fails here rather than in
somebody's approval prompt.
"""

from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.edit import protected_path_reason
from lup.policy.kernel.rows import PathRuleKind, PathRuleRow, ShellRuleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def verdict(command: str) -> KernelDecision:
    return decide_shell(command, rows())


def rule(kind: PathRuleKind, value: str, reason: str) -> PathRuleRow:
    return PathRuleRow(
        kind=kind, value=value, reason=reason, recovery=[], allow_autonomous=False
    )


def test_a_uv_run_question_names_what_it_installs() -> None:
    """Both spellings of the flag, read back as one."""
    asked = verdict("uv run --with requests --with=httpx python script.py")

    assert asked.effect == "ask"
    assert asked.headline() == (
        "asks: `uv run --with requests --with httpx` — fetches what it names and"
        " runs its code"
    )


def test_a_requirements_file_and_an_env_file_are_each_named() -> None:
    """Both operands are named, and each flag's effect is said.

    A requirements file is code fetched and run; an env file is a secrets
    file loaded into the environment, so it is not put as external code.
    """
    asked = verdict("uv run --with-requirements r.txt --env-file .env pytest")

    assert asked.effect == "ask"
    assert asked.subject == "uv run --with-requirements r.txt --env-file .env"
    assert asked.reason == (
        "fetches what it names and runs its code; loads a secrets file into the"
        " process environment"
    )


def test_a_uv_add_question_names_the_packages_past_the_valued_flags() -> None:
    """`--package lup-agents` names where the dependency goes, not what it is."""
    asked = verdict('uv add --package lup-agents "mcp>=2.1.1,<3" jinja2 --dev')

    assert asked.effect == "ask"
    assert asked.headline() == (
        "asks: `uv add mcp>=2.1.1,<3 jinja2` — fetches what it adds and runs its"
        " build code"
    )


def test_a_uv_sync_question_says_it_resolves_anew_and_names_the_pinned_route() -> None:
    asked = verdict("uv sync --all-extras")

    assert asked.effect == "ask"
    assert asked.subject == "uv sync"
    assert asked.reason.startswith("resolves every dependency anew")
    assert [through["run"] for through in asked.recovery] == [
        ["uv", "sync", "--frozen"]
    ]
    assert verdict("uv sync --frozen").effect == "allow"


def test_a_path_rule_states_its_reason_and_leaves_the_path_to_the_subject() -> None:
    stated = protected_path_reason(
        "README.md", rule("exact", "README.md", "is human-authored")
    )

    assert stated == "is human-authored"


def test_a_categorical_reason_is_said_as_the_rule_says_it() -> None:
    stated = protected_path_reason(
        "sync.json", rule("subtree", "sync.json", "protected path requires approval")
    )

    assert stated == "protected path requires approval"


def test_a_path_under_a_protected_root_says_which_root() -> None:
    """The root is the one thing the path alone does not tell the approver."""
    stated = protected_path_reason(
        "packages/lup/src/lup/policy/kernel/words.py",
        rule(
            "subtree", "packages/lup/src/lup/policy", "protected path requires approval"
        ),
    )

    assert stated == (
        "protected path requires approval, as it is under `packages/lup/src/lup/policy`"
    )
