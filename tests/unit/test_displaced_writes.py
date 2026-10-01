"""A write whose target resolves through a symlink to somewhere else.

Every grant in the kernel reads a path lexically, which is what makes a role
declarable: a root names a tree, and a spelling sits under it or does not. A
symlink breaks that step, and the machine's temporary root is where it costs
the most — world-writable, so the link can be planted by anybody, and scratch,
so the grant it inherits is the widest one there is.

The two halves are tested apart because they are separated on purpose: the
host resolves and holds no role table, the kernel classifies and reads no
filesystem, and neither can reach the other's answer.
"""

from pathlib import Path

from lup.policy.assets.host import resolved_write_targets
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.roles import displaced_targets
from lup.policy.kernel.rows import DisplacedTargetRow, PathRoleRow
from lup.policy.kernel.settlement import SettlementFacts, settle

ROLES = [
    PathRoleRow(root="tests", role="test"),
    PathRoleRow(root="tmp", role="scratch"),
]


def test_the_host_reports_a_target_that_does_not_land_where_it_spells(
    tmp_path: Path,
) -> None:
    """Resolution is filesystem work, so it happens on the host and crosses.

    The leaf need not exist: what a write would follow is the directories
    above it, and those are what the link sits in.
    """
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "tmp").symlink_to(tmp_path / "elsewhere")

    reported = resolved_write_targets(["tmp/note.md"], tmp_path)

    assert reported == {"tmp/note.md": "elsewhere/note.md"}


def test_the_host_reports_nothing_for_a_target_that_lands_where_it_reads(
    tmp_path: Path,
) -> None:
    """Only a difference is worth carrying, so an ordinary path stays silent."""
    (tmp_path / "tmp").mkdir()

    assert resolved_write_targets(["tmp/note.md"], tmp_path) == {}


def test_the_host_leaves_an_expansion_alone(tmp_path: Path) -> None:
    """A word carrying an expansion names a different path at run time."""
    assert resolved_write_targets(["$SOMEWHERE/note.md"], tmp_path) == {}


def test_a_landing_under_another_role_is_what_the_kernel_reports() -> None:
    """The kernel's half: whether the landing changes what the path is."""
    reported = displaced_targets(
        [DisplacedTargetRow(path="tmp/note.py", lands="src/note.py")], ROLES
    )

    assert [row["path"] for row in reported] == ["tmp/note.py"]


def test_a_landing_under_the_same_role_changed_nothing_this_table_can_see() -> None:
    """`tmp/a` pointing at `tmp/b` is scratch either way.

    The temporary root resolving to its own real name — `/private/tmp` on a
    system that spells it that way — is the same case: a link that moves a
    path within the root it already claimed takes no grant it did not have.
    """
    assert not displaced_targets(
        [DisplacedTargetRow(path="tmp/a.md", lands="tmp/b.md")], ROLES
    )


def test_a_production_spelling_claims_nothing_and_is_not_reported() -> None:
    """No relaxation is granted, so there is none to take back.

    Its write is judged where it reads either way — by the conventions, by
    the protected paths, by whether a capture holds what stands there.
    """
    assert not displaced_targets(
        [DisplacedTargetRow(path="src/app.py", lands="/etc/cron.d/job")], ROLES
    )


def test_the_settlement_row_asks_and_names_both_halves() -> None:
    """A reviewer shown only the typed path would be reading the wrong file."""
    settled = settle(
        SettlementFacts(
            KernelDecision("allow", "every shell segment is declared safe"),
            displaced=[
                DisplacedTargetRow(path="/tmp/link/job", lands="/etc/cron.d/job")
            ],
        )
    )

    assert settled.effect == "ask"
    assert settled.rule == "displaced-write"
    assert "/tmp/link/job" in settled.reason
    assert "/etc/cron.d/job" in settled.reason


def test_a_verdict_already_asking_keeps_its_own_reason() -> None:
    """The row reads against `allow` and `defer`: an ask has a reviewer."""
    settled = settle(
        SettlementFacts(
            KernelDecision("ask", "writing command output to a file"),
            displaced=[
                DisplacedTargetRow(path="/tmp/link/job", lands="/etc/cron.d/job")
            ],
        )
    )

    assert settled.effect == "ask"
    assert settled.rule != "displaced-write"


def test_a_caller_that_resolved_nothing_leaves_the_lexical_grants_standing() -> None:
    """Absent facts are absent, not an accusation: the row stays silent."""
    settled = settle(
        SettlementFacts(KernelDecision("allow", "every shell segment is declared safe"))
    )

    assert settled.effect == "allow"


def test_an_edit_through_a_link_is_asked_about_before_any_gate_reads_the_role() -> None:
    """The edit path has the same hole and does not reach the settlement rows.

    A scratch spelling skips the anti-pattern, marker and size gates. Landing
    in production, that is those gates read off one file and spent on another.
    """
    decided = decide_edit(
        "tmp/note.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=ROLES,
        python_source=True,
        displaced=DisplacedTargetRow(path="tmp/note.py", lands="src/note.py"),
    )

    assert decided.effect == "ask"
    assert decided.rule == "edit:displaced-path"
    assert "src/note.py" in decided.reason


def test_an_edit_that_lands_where_it_reads_is_judged_as_it_always_was() -> None:
    """Nothing displaced, nothing changed: scratch still relaxes the gates."""
    decided = decide_edit(
        "tmp/note.py",
        None,
        "value = 1\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=ROLES,
        python_source=True,
    )

    assert decided.effect == "allow"
