"""A session cannot accept replacement policy or silently rewrite launch authority."""

from pathlib import Path

import pytest

from lup.harness.enforcement import declared_path_rules
from lup.policy.models import EditBatch, EditChange, ShellCommand
from lup.policy.rules import EditPolicy, ShellPolicy
from lup_template.harness.catalog import declared_hook_set


@pytest.mark.parametrize(
    "prefix", ["", "env -u LUP_BOUNDARY_NONCE ", "LUP_BOUNDARY_NONCE= ", "command "]
)
def test_operator_refresh_cannot_be_called_by_a_requesting_session(prefix: str) -> None:
    hooks = declared_hook_set()
    policy = ShellPolicy(
        hooks.resolved_shell_rules(),
        runner_targets=list(hooks.runner_targets),
        sandbox_active=True,
    )
    decision = policy.decide(
        ShellCommand(
            command=prefix
            + "uv run lup-devtools harness policy-refresh --nonce example --repository /example"
        )
    )

    assert decision.effect == "deny"
    assert decision.hard
    assert "operator" in decision.recovery.lower()


@pytest.mark.parametrize(
    "path",
    [
        ".lup/preflight/session.json",
        ".lup/policy-snapshots/digest/runtime/policy_data.py",
        ".lup/questions.jsonl",
        ".lup/review-claims/review-id",
        ".lup/review-stage-claims/review-id/stage-digest",
    ],
)
def test_launch_authority_writes_remain_protected(path: str) -> None:
    policy = EditPolicy(declared_path_rules(declared_hook_set()))

    decision = policy.decide(
        EditBatch(
            changes=[
                EditChange(path=Path(path), before="value = 1\n", after="value = 2\n")
            ]
        )
    )

    assert decision.effect == "ask"
    assert "protected" in decision.rule


@pytest.mark.parametrize(
    "command",
    [
        "git rm README.md",
        "git --no-pager rm README.md",
        "git -P rm README.md",
        "git --literal-pathspecs rm README.md",
        "git -c color.ui=false rm README.md",
        "cd docs && git rm ../README.md",
        "git rm --cached README.md",
        "rm README.md tmp/x",
        "rm -r .",
        "rm *.md",
    ],
)
def test_a_capture_does_not_settle_deleting_a_human_owned_file(
    command: str, tmp_path: Path
) -> None:
    """Ownership is the question, and a checkout snapshot answers cost."""
    hooks = declared_hook_set()
    policy = ShellPolicy(
        hooks.resolved_shell_rules(),
        path_rules=declared_path_rules(hooks),
        runner_targets=list(hooks.runner_targets),
        recovered=True,
    )

    decision = policy.decide(ShellCommand(command=command, cwd=tmp_path))

    assert decision.effect == "ask"
    assert "captured and restorable" not in decision.reason


def test_a_capture_still_settles_deleting_what_nobody_owns(tmp_path: Path) -> None:
    hooks = declared_hook_set()
    policy = ShellPolicy(
        hooks.resolved_shell_rules(),
        path_rules=declared_path_rules(hooks),
        runner_targets=list(hooks.runner_targets),
        recovered=True,
    )

    decision = policy.decide(ShellCommand(command="git rm docs/x.md", cwd=tmp_path))

    assert decision.effect == "allow"
