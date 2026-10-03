"""What a capture can answer about a directory, and where it cannot.

The rule guarding a whole-directory delete takes part in the settlement layer,
so a snapshot that captured the tree discharges it. Untracked work inside a
directory being restored by nothing is true of `git stash create`, and is
exactly why `lup.devtools.dev.undo` does not use one: that module captures
tracked content *and* untracked files, and names ``rm -rf src/`` as the case
it exists for.

Reading the requirement off the *targets* is the other half: asserting
`boundary_wide` on the rule would settle ``rm -rf /etc/ssl`` as "captured and
restorable", the defect `verb_loss_scope` exists to prevent, reached one rule
sideways.
"""

from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.words import asks_before_removing_a_directory

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


def asked(command: str, targets: list[str]):
    """The rule's own verdict about one command naming one directory."""
    return asks_before_removing_a_directory(
        command.split(), SCRATCH, directory_targets=targets
    )


def test_a_directory_inside_the_checkout_is_one_a_capture_can_put_back() -> None:
    decision = asked("rm -rf src", ["src"])

    assert decision is not None
    assert decision.effect == "ask"
    assert decision.checkpoint == "boundary_wide"
    assert decision.purpose == "unrecovered_local_mutation"


def test_a_directory_outside_it_is_not_and_says_so_in_the_axis() -> None:
    """No snapshot of this checkout has ever held `/etc/ssl`.

    The purpose stays, because the loss is still the local kind. What changes
    is the requirement, and that is what stops the settlement row discharging
    a question against a capture that never covered the path.
    """
    decision = asked("rm -rf /etc/ssl", ["/etc/ssl"])

    assert decision is not None
    assert decision.checkpoint == "unrecoverable"


def test_a_scratch_root_keeps_its_own_grant() -> None:
    """Disposable by declaration, so the rule never reaches a verdict."""
    assert asked("rm -rf tmp/build", ["tmp/build"]) is None


def test_a_move_is_not_described_as_a_removal() -> None:
    """`mv` takes the directory off the path it was at and destroys nothing.

    Both verbs are guarded for the same reason -- what the directory holds is
    unbounded by the command -- and a reader who is told a move removes things
    learns to discount the reason rather than the verb.
    """
    decision = asked("mv src elsewhere", ["src"])

    assert decision is not None
    assert "moving" in decision.reason
    assert "deleting" not in decision.reason
