"""The manifest migration is a protected write; its probe cannot write."""

import pytest

from lup.policy.kernel.effects import declare
from lup.policy.models import ShellCommand
from lup.policy.rules import ShellPolicy
from lup.policy.shell_rules import ShellCommandRule
from lup.policy.vocabulary import (
    default_vocabulary,
    devtools_rules,
    runner_target_rules,
)


@pytest.mark.parametrize(
    "runner",
    [
        "uv run lup-devtools",
        "uv run --no-sync lup-devtools",
        "uv --directory /checkout run lup-devtools",
    ],
)
@pytest.mark.parametrize(
    ("arguments", "effect"),
    [
        ("", "ask"),
        (" --dry-run", "allow"),
        (" --dry-run=false", "ask"),
    ],
)
def test_migration_requires_review_except_for_its_literal_probe(
    runner: str, arguments: str, effect: str
) -> None:
    policy = ShellPolicy(default_vocabulary(), runner_targets=runner_target_rules())

    decision = policy.decide(
        ShellCommand(command=f"{runner} dev migrate pyright-environment{arguments}")
    )

    assert decision.effect == effect, decision.reason
    if effect == "ask":
        assert "pyproject.toml" in decision.reason


def test_migration_keeps_a_direct_console_script_refusal() -> None:
    """The writing form is refused; its dry run is a probe, as every probe row's is."""
    direct = ShellCommandRule(
        name="lup-devtools",
        effects=[declare("runs_declared_target")],
        refuses="Use uv run so the project environment is selected.",
        subcommands=devtools_rules(),
    )
    policy = ShellPolicy(
        [*default_vocabulary(), direct], runner_targets=runner_target_rules()
    )

    decision = policy.decide(
        ShellCommand(command="lup-devtools dev migrate pyright-environment")
    )

    assert decision.effect == "deny"


def test_a_probe_does_not_release_a_denied_neighbor() -> None:
    policy = ShellPolicy(default_vocabulary(), runner_targets=runner_target_rules())

    decision = policy.decide(
        ShellCommand(
            command="uv run lup-devtools dev migrate pyright-environment --dry-run && python -c 'print(1)'"
        )
    )

    assert decision.effect == "deny"


@pytest.mark.parametrize("arguments", ["", " --dry-run", " --dry-run=false"])
def test_an_unclassified_runner_wrapper_remains_denied(arguments: str) -> None:
    policy = ShellPolicy(default_vocabulary(), runner_targets=runner_target_rules())

    decision = policy.decide(
        ShellCommand(
            command=f"uv run env lup-devtools dev migrate pyright-environment{arguments}"
        )
    )

    assert decision.effect == "deny"


def test_other_migration_queries_keep_their_existing_admission() -> None:
    policy = ShellPolicy(default_vocabulary(), runner_targets=runner_target_rules())

    decision = policy.decide(
        ShellCommand(command="uv run lup-devtools dev migrate pending HEAD")
    )

    assert decision.effect == "allow"
