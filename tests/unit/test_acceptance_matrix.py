"""The product contract's own scenarios, run against the composed policy.

Every row of the overhaul's acceptance matrix that this layer can answer,
spelled once here so the contract is a suite rather than a table somebody
compares by eye. Rows about provider delivery, host transport, and crash
recovery live with the components that own them; what is here is what the
semantic policy decides.

The profile matters and is stated per case rather than assumed. Most of the
matrix describes the default *contained* profile, where odd local work is
settled inside; the uncontained rows are the ones that read differently, and
running both is how a change to one is stopped from silently moving the other.
"""

from pathlib import Path

import pytest

from lup.harness.models import HookSet
from lup.policy.boundary import depends_on
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.rows import PathRoleRow, ShellRuleRow
from lup.policy.kernel.semantics import UnjudgedAmbient
from lup.policy.kernel.shell import decide_shell
from lup.policy.profiles import compile_boundary, depended_on, measured
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary, gh_rule, git_rule

PRODUCTION = [PathRoleRow(root="src", role="production")]


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def contained(command: str, recovered: bool = False) -> KernelDecision:
    """One command under the default contained profile.

    Spelled without ``sandboxed``, which is what makes these rows about
    containment. Setting both left every row carried by the native sandbox
    and the containment fact inert beside it — so the profile these rows
    describe was never the one they exercised, and a container reaching the
    row named for it would have passed the suite either way.
    """
    return decide_shell(
        command,
        rows(),
        contained=True,
        inside_placement=True,
        escapable=False,
        recovered=recovered,
    )


def exposed(command: str, unjudged_ambient: UnjudgedAmbient = "ask") -> KernelDecision:
    """The same command with no containment beneath it."""
    return decide_shell(command, rows(), unjudged_ambient=unjudged_ambient)


@pytest.mark.parametrize(
    ("command", "effect"),
    [
        # Suspicious local inspection is contained, not reviewed. The
        # operation observes the contained environment, and a host path that
        # is not there fails inside with a diagnostic naming the escalation
        # that would reach it.
        ("cat /etc/passwd", "allow"),
        ("ls /proc", "allow"),
        ("find / -name '*.key'", "allow"),
        # Ordinary Git collaboration, unguarded at a classified destination.
        ("git push origin HEAD", "allow"),
        ("git push origin refs/heads/feature", "allow"),
        ("git tag v1.2.3", "allow"),
        # What removes or replaces history keeps its question in both
        # spellings — as a flag, and as refspec grammar.
        ("git push --force origin main", "ask"),
        ("git push +main:main", "ask"),
        ("git push origin :refs/heads/main", "ask"),
        ("git push --delete origin feat", "ask"),
        ("git push --mirror origin", "ask"),
        # Compensable GitHub collaboration.
        ("gh pr create --fill", "allow"),
        ("gh pr edit 12 --title x", "allow"),
        ("gh pr comment 12 --body x", "allow"),
        ("gh pr close 12", "allow"),
        ("gh pr reopen 12", "allow"),
        ("gh pr review 12 --comment --body x", "allow"),
        ("gh issue comment 3 --body x", "allow"),
        ("gh issue close 3", "allow"),
        ("gh issue reopen 3", "allow"),
        ("gh issue create --title x", "allow"),
        # Landing a request is the workflow's last step, and a leased force
        # onto a feature branch replaces only what this checkout last saw.
        ("gh pr merge 12", "allow"),
        ("git merge feat", "allow"),
        ("git push --force-with-lease origin feat", "allow"),
        # Execution, attestation, publication, repository security, and the
        # deletion nested inside an otherwise allowed operation, and a report
        # filed on somebody else's tracker.
        ("gh issue create --repo o/r --title x", "ask"),
        ("gh pr merge 12 --admin", "ask"),
        ("git push --force-with-lease origin main", "ask"),
        ("gh pr review 12 --approve", "ask"),
        ("gh pr review 12 --request-changes --body x", "ask"),
        ("gh pr close 12 --delete-branch", "ask"),
        ("gh api -X POST repos/o/r/issues", "ask"),
        ("gh release create v1", "ask"),
        ("gh secret set TOKEN", "ask"),
        ("gh repo edit --visibility public", "ask"),
        ("gh workflow run deploy.yml", "ask"),
        # Deliberately unreadable execution stays refused: a person shown the
        # readable half would be approving the other half unseen.
        ('eval "$COMMAND"', "deny"),
        ("source setup.sh", "deny"),
    ],
)
def test_the_matrix_rows_a_contained_session_answers(command: str, effect: str) -> None:
    """Each row of the contract that the shell policy settles on its own."""
    assert contained(command).effect == effect, command


def test_a_legible_unlisted_operation_is_settled_inside(tmp_path: Path) -> None:
    """The practical default for odd local work.

    Contained, everything it can affect is confined, so it runs there rather
    than costing a question — and it is Lup's permission rather than a
    handoff, so the placement holds however permissive the session's mode is.
    """
    settled = contained("frobnicate --weird")

    assert (settled.effect, settled.sandbox) == ("allow", "inside")


def test_an_unlisted_ambient_operation_follows_the_declared_policy() -> None:
    """Uncontained, the profile answers: visible by default, or handed over."""
    assert exposed("frobnicate --weird").effect == "ask"
    assert exposed("frobnicate --weird", unjudged_ambient="defer").effect == "defer"


def test_a_safe_outer_command_cannot_erase_a_nested_question() -> None:
    """Joined, one line is one act as far as the person deciding is concerned."""
    assert contained("ls && git push --delete origin feat").effect == "ask"


def test_a_marker_with_no_reason_authorizes_nothing() -> None:
    """The whole content of the request is what it says to whoever answers."""
    refused = contained("# lup: escalate[decision]:\nrm -rf /")

    assert (refused.effect, refused.hard) == ("deny", True)


def test_decision_escalation_moves_an_overrideable_refusal() -> None:
    """A rule's judgement is what somebody with more context may overrule.

    Both of these are refusals a person can be argued out of: work nobody
    classified, and a shape this repository refuses on reviewability grounds.
    Inline execution is deliberately in the second group rather than made an
    invariant — there are shapes for which it is the right answer, and a
    refusal nobody can argue with sends the agent to work around it instead.
    """
    unlisted = exposed("# lup: escalate[decision]: I need it\nfrobnicate --weird")
    inline = exposed('# lup: escalate[decision]: I need it\neval "$COMMAND"')

    assert unlisted.effect == "ask"
    assert inline.effect == "ask"
    assert "escalated (I need it)" in inline.reason


def test_a_policy_invariant_produces_the_same_refusal_when_escalated() -> None:
    """What no approval moves, because approval does not change a shape.

    Every verdict the lattice can reach is wrong for a generated tree: an
    allow writes something the next generation overwrites, and a question puts
    a decision to somebody whose only correct answer is "edit the source".
    """
    refused = decide_edit(
        ".claude/plugins/lup/hooks/runtime/policy_data.py",
        "a = 1\n",
        "a = 2\n",
        path_exists=True,
        path_rules=[],
        antipattern_rows=[],
    )

    assert (refused.effect, refused.hard) == ("deny", True)


def test_sandbox_escalation_asks_before_the_operation_crosses() -> None:
    """The crossing is reviewed however ordinary the operation reads inside."""
    asked = decide_shell(
        "# lup: escalate[sandbox]: the host holds the key\ncat /etc/passwd",
        rows(),
        sandboxed=True,
        contained=True,
        escapable=True,
    )

    assert (asked.effect, asked.sandbox) == ("ask", "outside")


def test_the_combined_marker_is_the_only_route_past_a_refusal_to_the_host() -> None:
    """Two requests composed by their order, neither knowing about the other."""
    both = decide_shell(
        "# lup: escalate[decision,sandbox]: needed\nfrobnicate --weird",
        rows(),
        sandboxed=True,
        contained=True,
        escapable=True,
    )

    assert (both.effect, both.sandbox) == ("ask", "outside")


def test_a_production_full_write_asks_and_a_supervisor_may_answer() -> None:
    """A quality checkpoint: what is reviewed is how the code reads."""
    decided = decide_edit(
        "src/app/service.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=PRODUCTION,
        operation="create",
    )

    assert (decided.effect, decided.reviewer) == ("ask", "supervisor_allowed")


def test_an_edit_above_the_size_gate_is_indistinguishable_from_no_lup() -> None:
    """The one verdict under which the session behaves as if Lup were absent."""
    decided = decide_edit(
        "src/app/service.py",
        "a = 1\n",
        "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n",
        path_exists=True,
        path_rules=[],
        antipattern_rows=[],
        path_roles=PRODUCTION,
    )

    assert (decided.effect, decided.abstention) == ("defer", "provider_native")


def test_a_recoverable_loss_settles_to_a_permission_and_an_unrecoverable_one_asks() -> (
    None
):
    """The axis that makes the effect a function of the session, and its limit.

    `git reset --hard` destroys working-tree content a capture holds, so a
    session carrying that capture has no permanent loss to ask about.
    `git clean -fdx` destroys exactly what the capture leaves out, which is
    why it is the one neighbour that keeps asking.
    """
    held = decide_shell(
        "git reset --hard", rows(), sandboxed=True, contained=True, recovered=True
    )
    unheld = decide_shell(
        "git clean -fdx", rows(), sandboxed=True, contained=True, recovered=True
    )

    assert held.effect == "allow"
    assert unheld.effect == "ask"


def test_recovery_never_discharges_a_question_it_has_nothing_to_do_with() -> None:
    """A capture answers local loss and nothing travelling beside it."""
    remote = decide_shell(
        "git push --delete origin feat",
        erase_shell_rules([git_rule(), gh_rule()]),
        sandboxed=True,
        contained=True,
        recovered=True,
    )

    assert remote.effect == "ask"


def test_a_missing_required_capability_stops_the_launch() -> None:
    """Matrix row: a session that cannot deliver its own boundary does not open.

    Blocked on this package until a launch measured anything. The profile
    promising containment and the runtime whose containment failed to start
    read identically from configuration, so what separates them is evidence —
    and without it every placement below was a claim.
    """
    hooks = HookSet(
        id="hooks.matrix",
        policy_ids=[],
        boundary_capabilities=[
            depends_on("inside_placement", "inside placement", contained_only=True)
        ],
    )
    boundary = compile_boundary(hooks, contained=True)

    absent = measured(boundary, depended_on(hooks, contained=True), [])

    assert not absent.launchable()
    assert "inside_placement" in absent.diagnosis()


def test_a_missing_optional_capability_blocks_only_its_dependants() -> None:
    """Matrix row: the launch opens and the operations needing it are refused.

    No reviewer approves a channel into existence, so an operation that needs
    one is capability-blocked rather than asked about — which sends the agent
    to the missing channel instead of to argue with a rule.
    """
    hooks = HookSet(
        id="hooks.matrix",
        policy_ids=[],
        boundary_capabilities=[depends_on("host_executor", required=False)],
    )
    boundary = compile_boundary(hooks, contained=True)

    preflight = measured(boundary, depended_on(hooks, contained=True), [])

    assert preflight.launchable()
    assert preflight.blocked() == ["host_executor"]


def test_a_crossing_with_no_channel_rests_on_a_person_or_is_refused() -> None:
    """Matrix row, in the half this package settles: what carries `outside`.

    Two answers, and which one applies is a fact about the session rather than
    about the operation. A person at a keyboard can be shown the exact command
    and run it themselves, so the crossing is a question. A headless session
    has neither a channel nor a person, and a question whose every answer
    leaves the operation with nowhere to go is one nobody should be shown — so
    it is capability-blocked with a typed cause, naming the missing channel
    rather than a rule the agent would go and reshape.

    Both rest on the same measurement: `host_executor` is declared with no
    probe while nothing carries a request, so `escapable` is false here
    however permissive the runtime's own per-call escape is. Package C is what
    turns the first answer into an unprompted allow.
    """
    asked = decide_shell(
        "# lup: escalate[sandbox]: the host toolchain\nuv run lup-devtools dev check",
        rows(),
        sandboxed=True,
        contained=True,
        escapable=False,
        interactive=True,
    )
    trapped = decide_shell(
        "# lup: escalate[sandbox]: the host toolchain\nuv run lup-devtools dev check",
        rows(),
        sandboxed=True,
        contained=True,
        escapable=False,
        interactive=False,
    )

    assert asked.effect == "ask"
    assert asked.sandbox == "outside"
    assert trapped.effect == "deny"
    assert trapped.cause == "capability"
    assert trapped.capability == "host_executor"


def test_a_worker_has_a_route_past_a_gate() -> None:
    """A worker's escalation marker reaches whoever supervises its run.

    A worker session is non-interactive, which is not the same as alone: it
    holds a mailbox reaching whoever supervises the run, so an escalated
    question is parked for them rather than refused for want of anyone to ask.
    """
    worker = decide_shell(
        "# lup: escalate[decision]: I need to clean my own scratch\nrm -rf tmp/x",
        rows(),
        interactive=False,
        relayed=True,
    )

    assert worker.effect == "ask"
    assert "I need to clean my own scratch" in worker.reason


def test_a_worker_can_remove_what_it_created() -> None:
    """Local loss a capture puts back is settled rather than asked.

    A worker that copies files into its own worktree to read them has to be
    able to remove them again: a question to a human about files it made
    itself parks a run on nothing. The capture is what makes settling it
    honest.
    """
    with_capture = decide_shell(
        "rm scratch.txt",
        rows(),
        interactive=False,
        relayed=True,
        recovered=True,
        contained=True,
        sandboxed=True,
    )

    assert with_capture.effect == "allow"


def test_a_judged_question_is_not_run_because_nobody_can_be_asked() -> None:
    """A judged question nobody can answer is refused, never run.

    The OS boundary confines an operation; it does not review it. Reading a
    sandboxed host as licence to run every guarded command would turn each
    deliberate question, an escalation marker's included, into a run.
    """
    headless = decide_shell(
        "git push --delete origin feat",
        rows(),
        sandboxed=True,
        contained=True,
        interactive=False,
    )

    assert headless.effect == "deny"
    assert "nobody who could approve it is reachable" in headless.reason


def test_an_unprompted_crossing_needs_a_measured_channel() -> None:
    """Reaching the host unprompted needs a declared executor, measured at launch.

    A profile without one refuses rather than emitting a key it hopes the
    runtime honours.
    """
    # The offered table declares no crossing at all, which is the other half
    # of the answer: nothing in a general-purpose toolchain needs the host,
    # so there is no unprompted escape to go untested.
    assert all(row["sandbox"] != "outside" for row in rows())


def test_a_read_only_builtin_is_not_refused() -> None:
    """`command -v` only reports a path, so it is allowed."""
    assert decide_shell("command -v python3", rows()).effect == "allow"
