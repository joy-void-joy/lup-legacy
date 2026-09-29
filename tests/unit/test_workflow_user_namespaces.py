"""A generated gate lifts the runner's refusal of unprivileged user namespaces.

GitHub's ubuntu-24.04 image restricts them through AppArmor, so a test that
needs a mount namespace of its own skips on the runner instead of running.
What is pinned here is that declaring the need renders the step that lifts
it, ahead of the gate, with the reason beside it — and that declaring nothing
renders no such step.
"""

from lup.devtools.dev.workflow import WorkflowSpec
from lup_template.harness.catalog import WORKFLOW

LIFTED = "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"


def test_declaring_the_need_lifts_the_restriction_before_the_gate() -> None:
    rendered = WorkflowSpec(user_namespaces=True).document().text()

    assert LIFTED in rendered
    assert rendered.index(LIFTED) < rendered.index("uv run lup-devtools dev check")


def test_the_step_says_why_it_is_there() -> None:
    """A sysctl on a CI runner reads as a hack unless the file says what for."""
    spec = WorkflowSpec(user_namespaces=True)
    (step,) = spec.namespace_steps()
    reason = "".join(f"      # {line}\n" for line in step.comment.splitlines())

    assert "AppArmor" in step.comment
    assert f"{reason}      - name: {step.name}\n" in spec.document().text()


def test_declaring_nothing_renders_no_sysctl() -> None:
    assert "sysctl" not in WorkflowSpec().document().text()


def test_this_project_runs_its_namespace_tests_on_the_runner() -> None:
    """The mount-namespace tests are this repository's, so its gate lifts it."""
    assert LIFTED in WORKFLOW.document().text()
