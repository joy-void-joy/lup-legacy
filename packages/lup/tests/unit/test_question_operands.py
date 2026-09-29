"""A question leads with the operands the decision turns on.

Whoever approves reads the reason and nothing else, and answers yes or no.
What they weigh is the operand: which packages a `--with` installs, which path
a write lands on, which file a rule protects. A reason that named the category
and left the operand out -- "external code", "a protected path" -- put the
one word that decided the question somewhere the approver could not read it.

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
        kind=kind, value=value, reason=reason, recovery="", allow_autonomous=False
    )


def test_a_uv_run_question_names_what_it_installs() -> None:
    """Both spellings of the flag, read back as one."""
    asked = verdict("uv run --with requests --with=httpx python script.py")

    assert asked.effect == "ask"
    assert asked.reason == (
        "uv run fetches and runs external code: --with requests --with httpx"
    )


def test_a_requirements_file_and_an_env_file_are_each_named() -> None:
    """Both operands are named, each beside what its flag does.

    A requirements file is code fetched and run; an env file is a secrets
    file loaded into the environment, so it is not put as external code.
    """
    asked = verdict("uv run --with-requirements r.txt --env-file .env pytest")

    assert asked.effect == "ask"
    assert asked.reason == (
        "uv run fetches and runs external code: --with-requirements r.txt;"
        " uv run --env-file .env loads a secrets file into the process environment"
    )


def test_a_uv_add_question_names_the_packages_past_the_valued_flags() -> None:
    """`--package lup-agents` names where the dependency goes, not what it is."""
    asked = verdict('uv add --package lup-agents "mcp>=2.1.1,<3" jinja2 --dev')

    assert asked.effect == "ask"
    assert (
        asked.reason
        == "uv add fetches and runs the build code of mcp>=2.1.1,<3, jinja2"
    )


def test_a_uv_sync_question_says_it_resolves_anew_and_names_the_pinned_route() -> None:
    asked = verdict("uv sync --all-extras")

    assert asked.effect == "ask"
    assert asked.reason.startswith("uv sync resolves every dependency anew")
    assert asked.reason.endswith("— `uv sync --all-extras`")
    assert "--frozen" in asked.recovery
    assert verdict("uv sync --frozen").effect == "allow"


def test_a_path_rule_whose_reason_names_the_path_is_not_named_twice() -> None:
    """`README.md matches the rule README.md: README.md is ...` said it three times."""
    stated = protected_path_reason(
        "README.md", rule("exact", "README.md", "README.md is human-authored")
    )

    assert stated == "README.md is human-authored"


def test_a_categorical_reason_leads_with_the_path() -> None:
    stated = protected_path_reason(
        "sync.json", rule("subtree", "sync.json", "protected path requires approval")
    )

    assert stated == "sync.json: protected path requires approval"


def test_a_path_under_a_protected_root_says_which_root() -> None:
    """The root is the one thing the path alone does not tell the approver."""
    stated = protected_path_reason(
        "packages/lup/src/lup/policy/kernel/words.py",
        rule(
            "subtree", "packages/lup/src/lup/policy", "protected path requires approval"
        ),
    )

    assert stated == (
        "packages/lup/src/lup/policy/kernel/words.py is under"
        " packages/lup/src/lup/policy: protected path requires approval"
    )
