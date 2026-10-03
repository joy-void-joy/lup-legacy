"""A write the measured boundary does not cover reaches a reviewer.

The gap no mount table can close. A contained launch enumerates its siblings
when the container starts and punches a read-only overlay over each; the mount
namespace is fixed from then on, so a worktree cut *afterwards* gets the
writable base with no overlay and there is no remount to make. The judgement is
what is left — the same arrangement the lease already relies on for the shared
administrative directory it deliberately mounts writable.
"""

from pathlib import Path

from lup.policy.assets.host import unleased_write_targets
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.lex import shell_path_verb_targets
from lup.policy.kernel.roles import (
    is_session_scratch_target,
    is_temporary_root_target,
)
from lup.policy.kernel.rows import PathRoleRow, ShellRuleRow
from lup.policy.kernel.settlement import SettlementFacts, settle
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

MEASURED = {"writable_roots": ["/repo/tree/mine", "/repo/.git"]}
SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


VOCABULARY = erase_shell_rules(default_vocabulary())


def rows() -> list[ShellRuleRow]:
    """The offered vocabulary, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def judged(command: str, unleased: list[str] | None = None) -> KernelDecision:
    """One command in a contained session, with what falls outside its lease."""
    return decide_shell(
        command,
        rows(),
        sandboxed=True,
        contained=True,
        path_roles=SCRATCH,
        existing_targets=[],
        unleased_targets=unleased,
    )


def test_a_target_inside_the_lease_is_not_reported() -> None:
    """The ordinary case, and the one that must stay silent.

    Reporting a leased write would put a question in front of every write in
    the checkout, which buries the ones that mean something.
    """
    assert unleased_write_targets(["notes.md"], MEASURED, Path("/repo/tree/mine")) == []


def test_a_sibling_cut_after_the_container_started_is_reported() -> None:
    """The gap itself: a writable base with no overlay over it."""
    reported = unleased_write_targets(
        ["/repo/tree/other/notes.md"], MEASURED, Path("/repo/tree/mine")
    )

    assert reported == ["/repo/tree/other/notes.md"]


def test_no_measurement_reports_nothing_rather_than_everything() -> None:
    """A session that could not read its lease knows of no writable roots.

    Treating that as "every target is unleased" would ask about every write in
    the checkout — a question per command, teaching nobody anything. The
    boundary is already unmeasured, which every other row reads fail-closed;
    this one has nothing to add to it.
    """
    assert unleased_write_targets(["/anywhere/x"], {}, Path("/repo")) == []


def test_a_write_outside_the_lease_asks_where_it_would_have_allowed() -> None:
    """The row's whole point, stated against a command that otherwise allows."""
    leased = judged("mkdir -p tmp/build")
    unleased = judged("mkdir -p tmp/build", ["/repo/tree/other/tmp"])

    assert leased.effect == "allow"
    assert unleased.effect == "ask"
    assert unleased.purpose == "unrecovered_local_mutation"


def test_the_question_names_the_rule_and_the_path_it_is_about() -> None:
    """An ask that names no rule is one nobody can write a case for.

    The verdict this row overrides is reached by the vocabulary finding
    nothing to say, so it carries no id of its own and the row supplies one
    — and names the path, because "outside the boundary" without it sends a
    reviewer looking for which of several operands was meant.
    """
    verdict = judged("touch tmp/x", ["/repo/tree/other/tmp"])

    assert verdict.rule == "unleased-write"
    # The path leads: the allow this row overrides says every segment is
    # safe, which is true and decides nothing for whoever reads the question.
    assert verdict.subject == "/repo/tree/other/tmp"
    assert verdict.headline().startswith("asks: `/repo/tree/other/tmp` — ")


def test_a_read_outside_the_lease_is_not_a_question() -> None:
    """Only writes. Reading a sibling worktree is ordinary work.

    The lease mounts siblings read-only on purpose rather than hiding them,
    so a read there is exactly what the boundary intends to permit.
    """
    assert judged("cat notes.md").effect == "allow"
    assert judged("ls tmp").effect == "allow"


def acted_on_outside_the_lease(command: str) -> list[str]:
    """The targets the hook hands the row: acted-on operands, resolved."""
    return unleased_write_targets(
        shell_path_verb_targets(command, VOCABULARY), MEASURED, Path("/repo/tree/mine")
    )


def test_a_sed_that_only_prints_a_file_outside_the_lease_is_a_read() -> None:
    """`sed -n 1,80p` over a read-only mount asked, while `head` did not.

    Composed the way the hook composes it -- the command's acted-on operands,
    resolved against the lease, handed to the row -- because the row was right
    about every target it was given, and the defect was which targets reached
    it: a sed that prints named its file as acted on, and a file under a
    read-only mount is exactly the path no writable root contains.
    """
    printed = "sed -n 1,80p /mnt/findings/verify.py"
    rewritten = "sed -i 's/a/b/' /mnt/findings/verify.py"

    assert acted_on_outside_the_lease(printed) == []
    assert judged(printed, acted_on_outside_the_lease(printed)).effect == "allow"
    assert acted_on_outside_the_lease(rewritten) == ["/mnt/findings/verify.py"]


def test_a_judged_refusal_is_not_reopened_by_an_unleased_target() -> None:
    """Asking about a deny would offer to overturn somebody's answer.

    The row reads ``allow`` and ``defer`` alone: a refusal is a judgement that
    already happened, and an ask already reaches a reviewer who is shown the
    operation and can see where it points.
    """
    refused = settle(
        SettlementFacts(
            KernelDecision("deny", "a rule decided against this", rule="shell:x"),
            contained=True,
            inside_placement=True,
            unleased=["/repo/tree/other/x"],
        )
    )

    assert refused.effect == "deny"
    assert refused.rule == "shell:x"


def test_an_unleased_write_outranks_a_capture_of_this_session() -> None:
    """A proven capture settles a loss to a permission, over what it holds.

    The undo store covers the checkout this session runs in. A tree outside
    the measured boundary is not in it, so a reference that would discharge a
    question about local loss must not discharge this one — which is why the
    row is read before ``recovered-loss`` rather than after it.
    """
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            contained=True,
            inside_placement=True,
            checkpoint="complete",
            unleased=["/repo/tree/other/x"],
        )
    )

    assert settled.effect == "ask"
    assert settled.rule == "unleased-write"


def test_the_session_scratchpad_is_not_a_target_the_lease_answers_for() -> None:
    """The harness's own root, uncovered in the direction that makes it safe.

    A lease enumerates what a launch mounted from the host, so a path that is
    not one of those reads as uncovered — and the scratchpad is uncovered
    because it is container-private, not because it escapes. It holds nothing
    a capture was meant to protect, and the role layer already says so: an
    edit to the same path allows, and only the measured layer disagreed.

    What that cost was two friction reports that both named the wrong thing.
    `sed -n … > $TMPDIR/x` and `uv run python $TMPDIR/s.py` each asked, and
    the reason named the redirect, so the reader read their own verb as
    having been classified a write.

    The lease is right about the path and the row decides what it means: the
    host lists every uncovered target, and the row says which roots are the
    harness's own, at every placement, so no caller's list can differ there.
    """
    scratchpad = "/tmp/claude-1000/session/scratchpad/out.txt"
    assert is_session_scratch_target(scratchpad)
    assert is_session_scratch_target("$TMPDIR/out.txt")
    assert not is_session_scratch_target("/repo/tree/other/x")
    # The path is genuinely outside the lease: asking the lease gives the
    # wrong answer, correctly, and the row is what answers for it.
    assert unleased_write_targets([scratchpad], MEASURED, Path("/repo")) == [scratchpad]
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            sandbox_confined=True,
            unleased=[scratchpad, "$TMPDIR/out.txt"],
        )
    )
    assert settled.effect == "allow"


def test_the_temporary_root_is_the_launchs_own_where_the_launch_is_a_container() -> (
    None
):
    """`/tmp` inside a container is uncovered the way the scratchpad is.

    A lease enumerates what came from the host, so the machine's temporary
    root reads as outside it — and inside a measured container that is the
    direction that makes it safe: this launch's own directory, gone with it,
    holding nothing a capture was meant to protect. The same argument the
    scratchpad won, one root wider.
    """
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            contained=True,
            inside_placement=True,
            unleased=["/tmp/cs.txt"],
        )
    )

    assert settled.effect == "allow"


def test_the_same_path_uncontained_is_the_operators_own_and_still_asks() -> None:
    """The exception is spelled against the measurement, never against the path.

    A native sandbox confines one call at a time on the operator's own
    filesystem, so a temporary root it leaves writable holds other programs'
    files — the same word, and the opposite fact. Which is why
    `container_private` is the half asked for here and `bounded` is not.
    """
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            sandbox_confined=True,
            unleased=["/tmp/cs.txt"],
        )
    )

    assert settled.effect == "ask"
    assert settled.rule == "unleased-write"


def test_a_target_that_is_still_the_leases_business_asks_and_names_only_itself() -> (
    None
):
    """The row keeps working over the targets it is still right about.

    Both in one operation, because that is where a filter goes wrong quietly:
    dropping the question along with the exempt path would lose the sibling
    worktree entirely, and keeping the question while still naming `/tmp`
    would send a reviewer to look at something nobody is asking about.
    """
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            contained=True,
            inside_placement=True,
            unleased=["/repo/tree/other/x", "/tmp/cs.txt"],
        )
    )

    assert settled.effect == "ask"
    assert settled.subject == "/repo/tree/other/x"


def test_the_temporary_root_is_read_after_normalizing_rather_than_by_its_prefix() -> (
    None
):
    """A word that climbs back out is not the temporary root, however it starts.

    The scratchpad's predicate is written this way for the same reason, and an
    exemption is exactly the wrong place to trust a prefix: `/tmp/../etc` is a
    write to the operator's machine wearing the one spelling this exempts.
    """
    assert is_temporary_root_target("/tmp/cs.txt")
    assert not is_temporary_root_target("/tmp/../etc/passwd")
    assert not is_temporary_root_target("$SOMEWHERE/cs.txt")
    assert not is_temporary_root_target("/repo/tree/other/x")
